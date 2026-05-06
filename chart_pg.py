"""PyQtGraph renderer for Market Profile.

Layout (classic expanded TPO):
  - Y-axis: Price
  - X-axis: Time (bracket start for single day; per-day columns for composite)
  - Letters placed at (bracket_x, price)
  - TPO count per price row drawn as a numeric column on the LEFT of the chart
  - POC / VAH / VAL as horizontal lines spanning the whole time range
  - Value Area as a horizontal shaded band
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QFont, QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from engine import (
    ProfileResult, bin_counts, bin_minute_counts, bin_letters_per_bracket,
    bracket_letter, composite_letter, minute_key_levels,
)

pg.setConfigOptions(antialias=True, background="w", foreground="k")

# 26 visually distinct colors — one per bracket letter (cycles beyond that)
TPO_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c5b0d5",
    "#c49c94", "#f7b6d2", "#c7c7c7", "#dbdb8d", "#9edae5",
    "#393b79", "#637939", "#8c6d31", "#843c39", "#7b4173",
    "#5254a3",
]


def _tpo_color(idx: int) -> str:
    return TPO_PALETTE[idx % len(TPO_PALETTE)]


class TimeAxis(pg.AxisItem):
    """X-axis that converts unix-seconds into HH:MM / date labels."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.mode = "time"  # or "date"

    def tickStrings(self, values, scale, spacing):
        if self.mode == "count":
            return [f"{int(v)}" if abs(v - int(v)) < 1e-6 else f"{v:g}" for v in values]
        out = []
        for v in values:
            try:
                dt = datetime.fromtimestamp(v)
            except (OSError, ValueError, OverflowError):
                out.append("")
                continue
            if self.mode == "date":
                out.append(dt.strftime("%Y-%m-%d"))
            else:
                out.append(dt.strftime("%H:%M"))
        return out


class ProfileChart(pg.PlotWidget):
    def __init__(self, parent=None):
        self.time_axis = TimeAxis(orientation="bottom")
        super().__init__(parent, axisItems={"bottom": self.time_axis})
        self.showGrid(x=True, y=True, alpha=0.25)
        self.setLabel("left", "Price")
        self.setLabel("bottom", "Time")

        # Crosshair
        self._vline = pg.InfiniteLine(angle=90, movable=False,
                                      pen=pg.mkPen("#888", style=Qt.DashLine))
        self._hline = pg.InfiniteLine(angle=0, movable=False,
                                      pen=pg.mkPen("#888", style=Qt.DashLine))
        self.addItem(self._vline, ignoreBounds=True)
        self.addItem(self._hline, ignoreBounds=True)

        self._readout = pg.TextItem(anchor=(0, 1), color="#222",
                                    fill=pg.mkBrush(255, 255, 255, 220))
        self._readout.setFont(QFont("Consolas", 9))
        self.addItem(self._readout, ignoreBounds=True)

        self.scene().sigMouseMoved.connect(self._on_mouse)
        self._count_at_price: dict = {}

    # ---------- public ----------
    def render(self, r: ProfileResult, bin_size: float,
               show_letters: bool = True, view_mode: str = "merged",
               count_metric: str = "tpo"):
        """view_mode: 'merged' | 'expanded' | 'continuous'.
           count_metric: 'tpo' (bracket count) | 'minute' (1-min bar count)."""
        self._count_metric = count_metric
        # Override key levels if minute-mode
        if count_metric == "minute" and r.minute_counts:
            lvl = minute_key_levels(r)
            r = self._with_key_levels(r, lvl)
        self.clear()
        self.addItem(self._vline, ignoreBounds=True)
        self.addItem(self._hline, ignoreBounds=True)
        self.addItem(self._readout, ignoreBounds=True)

        if view_mode == "continuous" and r.components:
            self._render_continuous(r, bin_size, show_letters)
            # No global VA/POC lines — each day has its own.
            self.setTitle(
                f"<b>{r.title}</b>   "
                f"<span style='color:#555'>period={r.period_minutes}m  "
                f"tick={r.tick_size}  bin={bin_size}  VA={r.value_area_pct*100:.0f}%</span>"
            )
            return

        if view_mode == "merged":
            self._render_merged(r, bin_size, show_letters)
        elif r.components:
            self._render_composite(r, bin_size)
        else:
            self._render_single(r, bin_size, show_letters)

        # Common: value area + POC/VAH/VAL
        va_region = pg.LinearRegionItem(
            values=(r.val, r.vah), orientation="horizontal",
            brush=pg.mkBrush(255, 215, 0, 40), pen=pg.mkPen(None), movable=False,
        )
        va_region.setZValue(-20)
        self.addItem(va_region)

        self._add_hline(r.poc, "#d62728", f"\tPOC {r.poc:.2f}", width=2, dash=True)
        self._add_hline(r.vah, "#2ca02c", f"\tVAH {r.vah:.2f}")
        self._add_hline(r.val, "#2ca02c", f"\tVAL {r.val:.2f}")

        self.setTitle(
            f"<b>{r.title}</b>   "
            f"<span style='color:#555'>period={r.period_minutes}m  "
            f"tick={r.tick_size}  bin={bin_size}  VA={r.value_area_pct*100:.0f}%</span>"
        )

    # ---------- merged (true composite / single collapsed) ----------
    def _render_merged(self, r: ProfileResult, bin_size: float, show_letters: bool):
        self.time_axis.mode = "count"
        self.setLabel("bottom", "TPO Count")

        # Merge counts across self (or components if composite)
        sources = r.components if r.components else [r]

        # Global bracket index offset per source (day) = sum of prior days' brackets
        day_offsets = []
        running = 0
        for src in sources:
            day_offsets.append(running)
            running += max(len(src.bracket_starts), 1)
        is_composite = len(sources) > 1

        # bin_start -> set of global indices (dedup: each bracket counts once per bin)
        bin_idx_sets: dict = {}
        for di, src in enumerate(sources):
            off = day_offsets[di]
            for price, idxs in src.bracket_visits.items():
                bs = round(np.floor(price / bin_size) * bin_size, 8)
                bin_idx_sets.setdefault(bs, set()).update(off + i for i in idxs)

        bin_counts_map = {bs: len(s) for bs, s in bin_idx_sets.items()}
        bin_letters = {bs: sorted(s) for bs, s in bin_idx_sets.items()} if show_letters else {}

        if not bin_counts_map:
            return
        starts = sorted(bin_counts_map.keys())
        centers = [s + bin_size / 2 for s in starts]
        counts = [bin_counts_map[s] for s in starts]   # bracket counts (drive grid)
        max_count = max(counts)
        label_counts = self._label_counts(r, bin_size, starts)  # metric-driven
        self._count_at_price = dict(zip(centers, label_counts))

        # Horizontal extent — ensure a sane minimum so narrow dists don't balloon
        unit = max(max_count, 20)
        x_left = -unit * 0.20           # space for count column
        x_right = unit * 1.05
        count_x = -unit * 0.03

        self._draw_count_column(count_x, centers, label_counts, bin_size)

        total_cells = sum(counts)
        letter_fn = composite_letter if is_composite else bracket_letter
        font = QFont("Consolas", 8); font.setBold(True)

        # Threshold: above this, skip per-cell rects + letters (perf)
        CELL_LIMIT = 6000

        if show_letters and total_cells <= CELL_LIMIT:
            # Per-cell colored rectangles with centered letters
            pen = QPen(QColor("#555555")); pen.setWidthF(0.0)
            for bs, gidxs in bin_letters.items():
                y = bs + bin_size / 2
                h = bin_size * 0.9
                for pos, g_idx in enumerate(gidxs):
                    rect = QGraphicsRectItem(pos, bs + bin_size * 0.05, 1.0, h)
                    rect.setBrush(QBrush(QColor(_tpo_color(g_idx))))
                    rect.setPen(pen)
                    self.addItem(rect)
                    lbl = pg.TextItem(text=letter_fn(g_idx),
                                      color="#ffffff", anchor=(0.5, 0.5))
                    lbl.setFont(font)
                    lbl.setPos(pos + 0.5, y)
                    self.addItem(lbl)
        elif show_letters:
            # Fallback: one text item per row (fast)
            bar = pg.BarGraphItem(
                x0=[0] * len(centers), y=centers,
                height=bin_size * 0.9, width=counts,
                brush=pg.mkBrush(232, 241, 251, 200),
                pen=pg.mkPen(184, 212, 238, 220),
            )
            bar.setZValue(-5)
            self.addItem(bar)
            for bs, gidxs in bin_letters.items():
                y = bs + bin_size / 2
                text = "".join(letter_fn(i) for i in gidxs)
                if not text:
                    continue
                item = pg.TextItem(text=text, color="#1a3a5c", anchor=(0.0, 0.5))
                item.setFont(font)
                item.setPos(0, y)
                self.addItem(item)
        else:
            # No letters — solid histogram
            bar = pg.BarGraphItem(
                x0=[0] * len(centers), y=centers,
                height=bin_size * 0.9, width=counts,
                brush=pg.mkBrush("#6fb3e8"), pen=pg.mkPen("#2b6ca3"),
            )
            self.addItem(bar)

        # Reset ticks to numeric (clear any previous custom ticks)
        self.getAxis("bottom").setTicks(None)
        self.setXRange(x_left, x_right, padding=0.02)
        pad = bin_size * 1.5
        self.setYRange(min(centers) - pad, max(centers) + pad, padding=0)

    # ---------- single day (expanded) ----------
    def _render_single(self, r: ProfileResult, bin_size: float, show_letters: bool):
        self.time_axis.mode = "time"
        starts, counts = bin_counts(r, bin_size)
        if not starts:
            return
        centers = [s + bin_size / 2 for s in starts]
        label_counts = self._label_counts(r, bin_size, starts)
        self._count_at_price = dict(zip(centers, label_counts))

        # X positions — bracket start times as unix seconds
        bracket_x = [b.timestamp() for b in r.bracket_starts]
        if not bracket_x:
            return
        period_sec = r.period_minutes * 60
        x_left = bracket_x[0] - period_sec * 1.2   # space for count column
        x_right = bracket_x[-1] + period_sec

        # LEFT count column — numeric labels (metric-driven)
        count_x = bracket_x[0] - period_sec * 0.55
        self._draw_count_column(count_x, centers, label_counts, bin_size)

        bl = bin_letters_per_bracket(r, bin_size)
        font = QFont("Consolas", 8); font.setBold(True)
        pen = QPen(QColor("#555555")); pen.setWidthF(0.0)
        rect_w = period_sec * 0.9
        x_pad = period_sec * 0.05

        for (bin_start, idx), _ in bl.items():
            if idx >= len(bracket_x):
                continue
            y = bin_start + bin_size / 2
            rect = QGraphicsRectItem(
                bracket_x[idx] + x_pad, bin_start + bin_size * 0.05,
                rect_w, bin_size * 0.9,
            )
            rect.setBrush(QBrush(QColor(_tpo_color(idx))))
            rect.setPen(pen)
            self.addItem(rect)
            if show_letters:
                lbl = pg.TextItem(text=bracket_letter(idx), color="#ffffff",
                                  anchor=(0.5, 0.5))
                lbl.setFont(font)
                lbl.setPos(bracket_x[idx] + period_sec / 2, y)
                self.addItem(lbl)

        # Tick spacing for time axis — show every bracket
        self.getAxis("bottom").setTicks([[(x, datetime.fromtimestamp(x).strftime("%H:%M"))
                                          for x in bracket_x]])
        self.setXRange(x_left, x_right, padding=0.02)
        pad = bin_size * 1.5
        self.setYRange(min(centers) - pad, max(centers) + pad, padding=0)

    # ---------- continuous (N day strip) ----------
    def _render_continuous(self, r: ProfileResult, bin_size: float, show_letters: bool):
        """Side-by-side independent profiles: each day = own band with own
        letters (A..), own POC/VAH/VAL, own count column."""
        self.time_axis.mode = "count"
        self.setLabel("bottom", "Days (left → right)")

        comps = r.components
        if not comps:
            return

        font = QFont("Consolas", 8); font.setBold(True)
        cell_pen = QPen(QColor("#555555")); cell_pen.setWidthF(0.0)

        # Compute each day's bin counts/letters once
        per_day = []
        all_centers = []
        for day in comps:
            starts, counts = bin_counts(day, bin_size)
            centers = [s + bin_size / 2 for s in starts]
            bl = bin_letters_per_bracket(day, bin_size)
            per_bin: dict = {}
            for (bs, idx), _ in bl.items():
                per_bin.setdefault(bs, []).append(idx)
            for v in per_bin.values():
                v.sort()
            max_cnt = max(counts) if counts else 1
            label_counts = self._label_counts(day, bin_size, starts)
            # Per-day key levels override if metric=minute
            if getattr(self, "_count_metric", "tpo") == "minute" and day.minute_counts:
                lvl = minute_key_levels(day)
                day = self._with_key_levels(day, lvl)
            per_day.append({
                "day": day, "starts": starts, "counts": counts,
                "label_counts": label_counts,
                "centers": centers, "per_bin": per_bin, "max_cnt": max_cnt,
            })
            all_centers.extend(centers)

        # Layout: each band width = max(max_cnt, 6) + 2 (gap), preceded by a count column slot of ~3
        COUNT_W = 3.0
        GAP = 2.0
        x_cursor = 0.0
        band_bounds = []  # list of (x_start, x_end) in data units
        band_count_x = []
        band_cell_x0 = []  # where cells start (after count column)
        for d in per_day:
            x_count = x_cursor + COUNT_W * 0.85       # right edge of count column text
            x0 = x_cursor + COUNT_W                    # cells start here
            band_w = max(d["max_cnt"], 1) + 0.5
            x_end = x0 + band_w
            band_bounds.append((x_cursor, x_end))
            band_count_x.append(x_count)
            band_cell_x0.append(x0)
            x_cursor = x_end + GAP

        # Merged crosshair source (metric-driven)
        merged_starts, merged_counts = self._counts_for(r, bin_size)
        merged_centers = [s + bin_size / 2 for s in merged_starts]
        self._count_at_price = dict(zip(merged_centers, merged_counts))

        # Draw each band
        for bi, d in enumerate(per_day):
            day = d["day"]
            x0 = band_cell_x0[bi]

            # Count column (per-day, metric-driven)
            self._draw_count_column(band_count_x[bi], d["centers"], d["label_counts"], bin_size)

            # Per-day VA shading (only within band x range)
            x_band_start, x_band_end = band_bounds[bi]
            va_rect = QGraphicsRectItem(
                x_band_start, day.val,
                x_band_end - x_band_start, day.vah - day.val,
            )
            va_rect.setBrush(QBrush(QColor(255, 215, 0, 45)))
            va_rect.setPen(QPen(Qt.NoPen))
            va_rect.setZValue(-10)
            self.addItem(va_rect)

            # POC line (segment within band)
            self._add_segment(x_band_start, x_band_end, day.poc,
                              "#d62728", width=2, dash=True)
            # VAH / VAL (segments)
            self._add_segment(x_band_start, x_band_end, day.vah, "#2ca02c")
            self._add_segment(x_band_start, x_band_end, day.val, "#2ca02c")

            # Cells + letters
            for bs, idxs in d["per_bin"].items():
                y = bs + bin_size / 2
                for pos, idx in enumerate(idxs):
                    rect = QGraphicsRectItem(
                        x0 + pos, bs + bin_size * 0.05, 1.0, bin_size * 0.9,
                    )
                    rect.setBrush(QBrush(QColor(_tpo_color(idx))))
                    rect.setPen(cell_pen)
                    self.addItem(rect)
                    if show_letters:
                        lbl = pg.TextItem(text=bracket_letter(idx), color="#ffffff",
                                          anchor=(0.5, 0.5))
                        lbl.setFont(font)
                        lbl.setPos(x0 + pos + 0.5, y)
                        self.addItem(lbl)

        # Day-separator lines + date labels
        sep_pen = pg.mkPen("#bbb", style=Qt.DashLine)
        ticks = []
        for bi, d in enumerate(per_day):
            x_start, x_end = band_bounds[bi]
            if bi > 0:
                line = pg.InfiniteLine(pos=x_start, angle=90,
                                       pen=sep_pen, movable=False)
                line.setZValue(-2)
                self.addItem(line, ignoreBounds=True)
            mid = (x_start + x_end) / 2
            day = d["day"]
            label = day.session_date.strftime("%m-%d") if day.session_date else f"D{bi+1}"
            ticks.append((mid, label))
        self.getAxis("bottom").setTicks([ticks])

        if all_centers:
            pad = bin_size * 1.5
            self.setYRange(min(all_centers) - pad, max(all_centers) + pad, padding=0)
        self.setXRange(-1, x_cursor, padding=0.01)

    def _add_segment(self, x0, x1, y, color, width=1, dash=False):
        pen = pg.mkPen(color, width=width,
                       style=Qt.DashLine if dash else Qt.SolidLine)
        seg = pg.PlotCurveItem(x=[x0, x1], y=[y, y], pen=pen)
        seg.setZValue(5)
        self.addItem(seg)

    # ---------- composite expanded ----------
    def _render_composite(self, r: ProfileResult, bin_size: float):
        """Treat composite as one continuous timeline: all brackets from all
        days laid out sequentially left-to-right. Letters use the continuous
        composite sequence (A..Z, a..z, a1..). Day boundaries are marked."""
        self.time_axis.mode = "count"  # numeric x-axis (bracket position)
        self.setLabel("bottom", "Bracket (time)")

        comps = r.components
        if not comps:
            return

        # Flatten: (global_idx, day_idx, bin_start) for every cell
        # Each day's brackets occupy consecutive global indices.
        day_offsets = []
        running = 0
        for day in comps:
            day_offsets.append(running)
            running += max(len(day.bracket_starts), 1)
        total_brackets = running

        # Aggregate left count column across merged counts
        starts, counts = bin_counts(r, bin_size)
        centers = [s + bin_size / 2 for s in starts]
        self._count_at_price = dict(zip(centers, counts))

        x_left = -total_brackets * 0.15  # space for count column
        x_right = total_brackets + 0.5
        count_x = -total_brackets * 0.02
        self._draw_count_column(count_x, centers, counts, bin_size)

        font = QFont("Consolas", 8); font.setBold(True)
        pen = QPen(QColor("#555555")); pen.setWidthF(0.0)

        # Per-cell rects + letters
        CELL_LIMIT = 6000
        # Count total cells first
        total_cells = 0
        for day in comps:
            total_cells += len(bin_letters_per_bracket(day, bin_size))

        if total_cells <= CELL_LIMIT:
            for di, day in enumerate(comps):
                off = day_offsets[di]
                for (bin_start, bracket_idx), _ in bin_letters_per_bracket(day, bin_size).items():
                    g_idx = off + bracket_idx
                    y = bin_start + bin_size / 2
                    rect = QGraphicsRectItem(g_idx, bin_start + bin_size * 0.05,
                                             1.0, bin_size * 0.9)
                    rect.setBrush(QBrush(QColor(_tpo_color(g_idx))))
                    rect.setPen(pen)
                    self.addItem(rect)
                    lbl = pg.TextItem(text=composite_letter(g_idx),
                                      color="#ffffff", anchor=(0.5, 0.5))
                    lbl.setFont(font)
                    lbl.setPos(g_idx + 0.5, y)
                    self.addItem(lbl)
        else:
            # Perf fallback: faint bars per row, one text per row summarising letters
            for bs, cnt in zip(starts, counts):
                bar = QGraphicsRectItem(0, bs + bin_size * 0.05,
                                        cnt, bin_size * 0.9)
                bar.setBrush(QBrush(QColor(232, 241, 251, 200)))
                bar.setPen(pen)
                self.addItem(bar)

        # Vertical dashed separators + date label at each day boundary
        sep_pen = pg.mkPen("#999", style=Qt.DashLine)
        for di, day in enumerate(comps):
            x = day_offsets[di]
            if di > 0:
                line = pg.InfiniteLine(pos=x, angle=90, pen=sep_pen, movable=False)
                line.setZValue(-2)
                self.addItem(line, ignoreBounds=True)

        # X-axis ticks: date label at the midpoint of each day's span
        ticks = []
        for di, day in enumerate(comps):
            n = max(len(day.bracket_starts), 1)
            mid = day_offsets[di] + n / 2
            label = day.session_date.strftime("%m-%d") if day.session_date else f"D{di+1}"
            ticks.append((mid, label))
        self.getAxis("bottom").setTicks([ticks])

        self.setXRange(x_left, x_right, padding=0.02)
        pad = bin_size * 1.5
        if centers:
            self.setYRange(min(centers) - pad, max(centers) + pad, padding=0)

    # ---------- helpers ----------
    def _with_key_levels(self, r: ProfileResult, lvl: dict) -> ProfileResult:
        """Return a shallow copy of r with overridden POC/VAH/VAL/total_tpo."""
        from dataclasses import replace
        return replace(r, poc=lvl["poc"], vah=lvl["vah"], val=lvl["val"],
                       total_tpo=lvl["total_tpo"])

    def _counts_for(self, r: ProfileResult, bin_size: float):
        """Return (starts, counts) for the LEFT count column based on metric."""
        if getattr(self, "_count_metric", "tpo") == "minute" and r.minute_counts:
            return bin_minute_counts(r, bin_size)
        return bin_counts(r, bin_size)

    def _label_counts(self, r: ProfileResult, bin_size: float, starts) -> list[int]:
        """Counts aligned to given bin starts, using the chosen metric."""
        m_starts, m_counts = self._counts_for(r, bin_size)
        m_map = dict(zip(m_starts, m_counts))
        return [m_map.get(s, 0) for s in starts]

    def _draw_count_column(self, x: float, centers, counts, bin_size):
        font = QFont("Consolas", 8)
        for y, c in zip(centers, counts):
            item = pg.TextItem(text=str(c), color="#555", anchor=(1.0, 0.5))
            item.setFont(font)
            item.setPos(x, y)
            self.addItem(item)
        # Header
        if centers:
            hdr = pg.TextItem(text="TPO", color="#333", anchor=(1.0, 0.5))
            hdr_font = QFont("Consolas", 8); hdr_font.setBold(True)
            hdr.setFont(hdr_font)
            hdr.setPos(x, max(centers) + bin_size * 1.2)
            self.addItem(hdr)

    def _add_hline(self, y: float, color: str, label: str, width: int = 1, dash: bool = False):
        pen = pg.mkPen(color, width=width,
                       style=Qt.DashLine if dash else Qt.SolidLine)
        line = pg.InfiniteLine(
            pos=y, angle=0, pen=pen, movable=False, label=label,
            labelOpts={"position": 0.02, "color": color,
                       "fill": (255, 255, 255, 220), "movable": False},
        )
        self.addItem(line, ignoreBounds=True)

    def _on_mouse(self, pos):
        vb = self.getViewBox()
        if vb is None or not self.sceneBoundingRect().contains(pos):
            self._readout.setText("")
            return
        mp = vb.mapSceneToView(pos)
        x, y = mp.x(), mp.y()
        self._vline.setPos(x)
        self._hline.setPos(y)

        nearest_price, nearest_count = None, 0
        if self._count_at_price:
            nearest_price = min(self._count_at_price.keys(), key=lambda p: abs(p - y))
            nearest_count = self._count_at_price[nearest_price]

        if self.time_axis.mode == "count":
            text = f"Price: {y:.2f}"
        else:
            try:
                t_str = datetime.fromtimestamp(x).strftime("%Y-%m-%d %H:%M")
            except (OSError, ValueError, OverflowError):
                t_str = f"{x:.0f}"
            text = f"Time:  {t_str}\nPrice: {y:.2f}"
        if nearest_price is not None:
            text += f"\nBin:   {nearest_price:.2f}  (TPO {nearest_count})"
        self._readout.setText(text)
        self._readout.setPos(x, y)
