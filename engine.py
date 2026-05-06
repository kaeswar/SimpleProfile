"""Market Profile engine — pure logic, no UI.

Data model keeps per-bracket info so the chart can place letters along a
time x-axis (classic expanded TPO view).
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

SESSION_START = time(9, 15)
SESSION_END = time(15, 30)

PERIOD_OPTIONS = {
    "30 min": 30,
    "1 hour": 60,
    "2 hours": 120,
    "4 hours": 240,
    "1 day": 24 * 60,
}

_DATE_RE = re.compile(r"(\d{4})[_-](\d{2})[_-](\d{2})")


def bracket_letter(i: int) -> str:
    if i < 26:
        return chr(ord("A") + i)
    return f"{chr(ord('A') + (i // 26) - 1)}{chr(ord('A') + (i % 26))}"


def composite_letter(i: int) -> str:
    """Continuous label for merged composite:
       0-25   -> A..Z
       26-51  -> a..z
       52+    -> a1, b1, ... z1, a2, b2, ... (lowercase + numeric suffix)
    """
    if i < 26:
        return chr(ord("A") + i)
    if i < 52:
        return chr(ord("a") + (i - 26))
    j = i - 52
    suffix = j // 26 + 1
    return f"{chr(ord('a') + (j % 26))}{suffix}"


@dataclass
class ProfileResult:
    # Key levels
    poc: float
    vah: float
    val: float
    total_tpo: int
    value_area_pct: float

    # Per-bracket info (for time x-axis)
    bracket_starts: list = field(default_factory=list)        # list[datetime]
    # price -> list of bracket indices that touched it (with repetition = count)
    bracket_visits: dict = field(default_factory=dict)

    # Session open / close / mid
    open_price: float = 0.0
    close_price: float = 0.0
    mid_price: float = 0.0      # (session_high + session_low) / 2

    # Initial Balance (first N minutes high/low)
    ib_high: float = 0.0
    ib_low: float = 0.0

    # Params / metadata
    period_minutes: int = 30
    tick_size: float = 0.05
    title: str = ""
    session_date: datetime | None = None

    # Composite components (empty for single day)
    components: list = field(default_factory=list)  # list[ProfileResult]

    # Minute-density counts (price tick -> # of 1-min bars that touched it)
    minute_counts: dict = field(default_factory=dict)
    # Per-minute (low, high) ranges — used for bin-level minute counts (dedup)
    minute_ranges: list = field(default_factory=list)

    @property
    def count_dict(self) -> dict[float, int]:
        return {p: len(v) for p, v in self.bracket_visits.items()}

    def letters_at(self, price: float) -> list[str]:
        idxs = self.bracket_visits.get(price, [])
        return [bracket_letter(i) for i in idxs]


# ---------- Data loading ----------

def parse_date_from_filename(path: str) -> datetime | None:
    m = _DATE_RE.search(os.path.basename(path))
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        return datetime(y, mo, d)
    except ValueError:
        return None


def list_csv_files(folder: str) -> list[tuple[datetime, str]]:
    out = []
    if not os.path.isdir(folder):
        return out
    for name in os.listdir(folder):
        if not name.lower().endswith(".csv"):
            continue
        full = os.path.join(folder, name)
        dt = parse_date_from_filename(name)
        if dt is None:
            continue
        out.append((dt, full))
    out.sort(key=lambda x: x[0])
    return out


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    t = df["timestamp"].dt.time
    df = df[(t >= SESSION_START) & (t <= SESSION_END)].copy()
    return df.sort_values("timestamp").reset_index(drop=True)


# ---------- Core math ----------

def get_key_levels(count_dict: dict, value_area_pct: float = 0.68) -> dict:
    if not count_dict:
        raise ValueError("No TPO data")
    poc_price = max(count_dict, key=count_dict.get)
    total_tpo = sum(count_dict.values())
    prices_sorted = sorted(count_dict.keys())
    poc_idx = prices_sorted.index(poc_price)

    left = right = poc_idx
    value_tpo_count = count_dict[poc_price]
    target = total_tpo * value_area_pct

    while value_tpo_count < target and (left > 0 or right < len(prices_sorted) - 1):
        l_val = count_dict[prices_sorted[left - 1]] if left > 0 else -1
        r_val = count_dict[prices_sorted[right + 1]] if right < len(prices_sorted) - 1 else -1
        if l_val >= r_val and left > 0:
            left -= 1
            value_tpo_count += l_val
        elif right < len(prices_sorted) - 1:
            right += 1
            value_tpo_count += r_val
        else:
            break

    return {"poc": poc_price, "vah": prices_sorted[right],
            "val": prices_sorted[left], "total_tpo": int(total_tpo)}


def compute_profile(df: pd.DataFrame, tick_size: float = 0.05,
                    period_minutes: int = 30, value_area_pct: float = 0.68,
                    title: str = "", ib_minutes: int = 60) -> ProfileResult:
    if df.empty:
        raise ValueError("Empty dataframe after session filter")

    start_time = df["timestamp"].iloc[0].floor("min")
    end_time = df["timestamp"].iloc[-1] + timedelta(minutes=1)

    bracket_starts: list[datetime] = []
    cur = start_time
    while cur < end_time:
        bracket_starts.append(cur.to_pydatetime() if hasattr(cur, "to_pydatetime") else cur)
        cur += timedelta(minutes=period_minutes)

    visits: dict = defaultdict(list)
    for i, s in enumerate(bracket_starts):
        e = s + timedelta(minutes=period_minutes)
        mask = (df["timestamp"] >= s) & (df["timestamp"] < e)
        if not mask.any():
            continue
        bdf = df[mask]
        low = bdf["low"].min()
        high = bdf["high"].max()
        p_start = np.ceil(low / tick_size) * tick_size
        p_end = np.floor(high / tick_size) * tick_size
        if p_start > p_end:
            continue
        for p in np.arange(p_start, p_end + tick_size, tick_size):
            pc = round(round(p / tick_size) * tick_size, 8)
            visits[pc].append(i)

    # Minute-density: store per-minute (low, high) ranges + tick-level counts.
    # Tick count = # minutes whose [low,high] touched that tick (max ≈ session minutes).
    # Bin count is computed at display time from ranges (dedups minutes per bin).
    minute_counts: dict = defaultdict(int)
    minute_ranges: list = []
    lows = df["low"].to_numpy()
    highs = df["high"].to_numpy()
    for lo, hi in zip(lows, highs):
        minute_ranges.append((float(lo), float(hi)))
        p_start = np.ceil(lo / tick_size) * tick_size
        p_end = np.floor(hi / tick_size) * tick_size
        if p_start > p_end:
            continue
        for p in np.arange(p_start, p_end + tick_size, tick_size):
            pc = round(round(p / tick_size) * tick_size, 8)
            minute_counts[pc] += 1

    count_dict = {p: len(v) for p, v in visits.items()}
    lvl = get_key_levels(count_dict, value_area_pct)

    session_date = df["timestamp"].iloc[0].to_pydatetime().replace(
        hour=0, minute=0, second=0, microsecond=0)

    # Open / Close / Mid
    open_price = float(df["open"].iloc[0])
    close_price = float(df["close"].iloc[-1])
    session_high = float(df["high"].max())
    session_low = float(df["low"].min())
    mid_price = (session_high + session_low) / 2.0

    # Initial Balance (first N minutes)
    ib_end = df["timestamp"].iloc[0] + timedelta(minutes=ib_minutes)
    ib_df = df[df["timestamp"] < ib_end]
    ib_high = float(ib_df["high"].max()) if not ib_df.empty else session_high
    ib_low = float(ib_df["low"].min()) if not ib_df.empty else session_low

    return ProfileResult(
        poc=lvl["poc"], vah=lvl["vah"], val=lvl["val"],
        total_tpo=lvl["total_tpo"], value_area_pct=value_area_pct,
        open_price=open_price, close_price=close_price, mid_price=mid_price,
        ib_high=ib_high, ib_low=ib_low,
        bracket_starts=bracket_starts, bracket_visits=dict(visits),
        minute_counts=dict(minute_counts), minute_ranges=minute_ranges,
        period_minutes=period_minutes, tick_size=tick_size, title=title,
        session_date=session_date,
    )


def compute_composite(results: list[ProfileResult], value_area_pct: float = 0.68,
                      tick_size: float = 0.05, title: str = "Composite") -> ProfileResult:
    """Merge daily profiles. Keeps components for per-day rendering along x.
    Preserves real bracket indices with day offsets so bin_letters_per_bracket works.
    """
    merged: dict = defaultdict(int)
    merged_min: dict = defaultdict(int)
    merged_ranges: list = []
    for r in results:
        for p, v in r.count_dict.items():
            merged[p] += v
        for p, v in r.minute_counts.items():
            merged_min[p] += v
        merged_ranges.extend(r.minute_ranges)
    if not merged:
        raise ValueError("No data to composite")
    lvl = get_key_levels(merged, value_area_pct)
    period = results[0].period_minutes if results else 30

    # Build composite_visits with real bracket indices (offset per day)
    # so that bin_letters_per_bracket correctly counts distinct brackets.
    composite_visits: dict = defaultdict(list)
    all_bracket_starts: list = []
    offset = 0
    for r in results:
        for p, idxs in r.bracket_visits.items():
            composite_visits[p].extend(offset + i for i in idxs)
        all_bracket_starts.extend(r.bracket_starts)
        offset += max(len(r.bracket_starts), 1)

    # Open / Close / Mid for the composite
    open_price = results[0].open_price if results else 0.0
    close_price = results[-1].close_price if results else 0.0
    all_prices = list(merged.keys())
    mid_price = (max(all_prices) + min(all_prices)) / 2.0 if all_prices else 0.0
    # IB from first component
    ib_high = results[0].ib_high if results else 0.0
    ib_low = results[0].ib_low if results else 0.0

    return ProfileResult(
        poc=lvl["poc"], vah=lvl["vah"], val=lvl["val"],
        total_tpo=lvl["total_tpo"], value_area_pct=value_area_pct,
        open_price=open_price, close_price=close_price, mid_price=mid_price,
        ib_high=ib_high, ib_low=ib_low,
        bracket_starts=all_bracket_starts, bracket_visits=dict(composite_visits),
        minute_counts=dict(merged_min), minute_ranges=merged_ranges,
        period_minutes=period, tick_size=tick_size, title=title,
        session_date=results[0].session_date if results else None,
        components=list(results),
    )


# ---------- Binning ----------

def _bin_bracket_sets(result: ProfileResult, bin_size: float) -> dict[float, set]:
    """bin_start -> set of bracket indices that touched ANY tick in the bin.
    This is the correct semantic: a bracket counts at most once per bin.
    """
    out: dict = defaultdict(set)
    for price, idxs in result.bracket_visits.items():
        bs = round(np.floor(price / bin_size) * bin_size, 8)
        out[bs].update(idxs)
    return out


def bin_minute_counts(result: ProfileResult, bin_size: float) -> tuple[list[float], list[int]]:
    """For each bin, count distinct 1-min bars whose [low, high] overlaps the bin.
    A minute is counted ONCE per bin even if its range spans many ticks of that bin.
    """
    if not result.minute_ranges:
        # Fallback for older data: best-effort using minute_counts max within bin.
        bins: dict = defaultdict(int)
        for price, cnt in result.minute_counts.items():
            bs = round(np.floor(price / bin_size) * bin_size, 8)
            bins[bs] = max(bins[bs], cnt)
        starts = sorted(bins.keys())
        return starts, [bins[s] for s in starts]

    # Determine bin range from data
    if not result.minute_counts:
        return [], []
    prices = list(result.minute_counts.keys())
    p_min = min(prices); p_max = max(prices)
    first_bin = np.floor(p_min / bin_size) * bin_size
    last_bin = np.floor(p_max / bin_size) * bin_size

    bins: dict = defaultdict(int)
    for lo, hi in result.minute_ranges:
        b_lo = max(np.floor(lo / bin_size) * bin_size, first_bin)
        b_hi = min(np.floor(hi / bin_size) * bin_size, last_bin)
        b = b_lo
        while b <= b_hi + 1e-9:
            bs = round(b, 8)
            bins[bs] += 1
            b += bin_size
    starts = sorted(bins.keys())
    return starts, [bins[s] for s in starts]


def minute_key_levels(result: ProfileResult, value_area_pct: float | None = None
                      ) -> dict:
    """Recompute POC/VAH/VAL from minute_counts at tick resolution."""
    if not result.minute_counts:
        return {"poc": result.poc, "vah": result.vah, "val": result.val,
                "total_tpo": result.total_tpo}
    va = value_area_pct if value_area_pct is not None else result.value_area_pct
    return get_key_levels(result.minute_counts, va)


def bin_counts(result: ProfileResult, bin_size: float) -> tuple[list[float], list[int]]:
    """Return (bin_start_prices, counts). Count = # distinct brackets that touched the bin."""
    sets = _bin_bracket_sets(result, bin_size)
    starts = sorted(sets.keys())
    return starts, [len(sets[s]) for s in starts]


def bin_letters_per_bracket(result: ProfileResult, bin_size: float
                            ) -> dict[tuple[float, int], int]:
    """(bin_start_price, bracket_idx) -> 1 if that bracket touched the bin, else absent."""
    out: dict = {}
    for bs, idxs in _bin_bracket_sets(result, bin_size).items():
        for i in idxs:
            out[(bs, i)] = 1
    return out
