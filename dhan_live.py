"""Dhan API live data fetcher for Market Profile.

Fetches 1-minute OHLCV intraday candle data for Nifty Futures.
Reuses patterns from KD_Fetcher and OptionAnalyzer_Codex projects.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, date

import pandas as pd
import requests

API_URL = "https://api.dhan.co/v2/charts/intraday"

# Default: Nifty current month future (update monthly or use continuous)
DEFAULT_SECURITY_ID = "66071"
DEFAULT_EXCHANGE_SEGMENT = "NSE_FNO"
DEFAULT_INSTRUMENT = "FUTIDX"

# Credentials file path (same as OptionAnalyzer_Codex)
CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.txt")


def load_credentials(path: str = CREDENTIALS_PATH) -> dict[str, str]:
    """Load client_id and access_token from credentials.txt."""
    creds = {}
    if not os.path.exists(path):
        return creds
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                creds[key.strip()] = val.strip()
    return creds


class DhanLiveFetcher:
    """Fetches live intraday 1-min OHLCV data from Dhan API."""

    def __init__(self, client_id: str = "", access_token: str = "",
                 security_id: str = DEFAULT_SECURITY_ID,
                 exchange_segment: str = DEFAULT_EXCHANGE_SEGMENT,
                 instrument: str = DEFAULT_INSTRUMENT):
        self.client_id = client_id
        self.access_token = access_token
        self.security_id = security_id
        self.exchange_segment = exchange_segment
        self.instrument = instrument
        self._session = requests.Session()

    @classmethod
    def from_credentials_file(cls, path: str = CREDENTIALS_PATH,
                              security_id: str = DEFAULT_SECURITY_ID) -> "DhanLiveFetcher":
        """Create fetcher from credentials.txt file."""
        creds = load_credentials(path)
        return cls(
            client_id=creds.get("client_id", ""),
            access_token=creds.get("access_token", ""),
            security_id=security_id,
        )

    def is_configured(self) -> bool:
        """Check if credentials are available."""
        return bool(self.client_id and self.access_token)

    def fetch_today(self) -> pd.DataFrame:
        """Fetch today's 1-min intraday data. Returns DataFrame with
        columns: timestamp, open, high, low, close, volume."""
        today = date.today().strftime("%Y-%m-%d")
        return self._fetch(from_date=today, to_date=today)

    def fetch_date(self, d: date) -> pd.DataFrame:
        """Fetch 1-min intraday data for a specific date."""
        ds = d.strftime("%Y-%m-%d")
        return self._fetch(from_date=ds, to_date=ds)

    def fetch_last_n_days(self, n: int = 10) -> list[tuple[date, pd.DataFrame]]:
        """Fetch 1-min data for the last N trading days (including today).
        Returns list of (date, DataFrame) sorted oldest-first."""
        from datetime import timedelta
        results = []
        today = date.today()
        # Start from today, go backwards until we have N days with data
        max_lookback = n * 2 + 10
        for offset in range(0, max_lookback):
            d = today - timedelta(days=offset)
            if d.weekday() >= 5:  # skip Saturday/Sunday
                continue
            try:
                df = self.fetch_date(d)
                if not df.empty:
                    results.append((d, df))
            except Exception:
                continue
            if len(results) >= n:
                break
            time.sleep(0.4)  # rate limit
        # Reverse to chronological order (oldest first)
        results.reverse()
        return results

    def _fetch(self, from_date: str, to_date: str) -> pd.DataFrame:
        """Internal: call Dhan intraday chart API and return DataFrame."""
        payload = {
            "securityId": str(self.security_id),
            "exchangeSegment": self.exchange_segment,
            "instrument": self.instrument,
            "interval": "1",
            "oi": False,
            "fromDate": from_date,
            "toDate": to_date,
        }
        headers = {
            "Content-Type": "application/json",
            "access-token": self.access_token,
            "client-id": self.client_id,
        }

        try:
            resp = self._session.post(API_URL, json=payload, headers=headers, timeout=30)
            if resp.status_code != 200:
                raise ConnectionError(f"Dhan API error {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Network error: {e}")

        # Parse response
        if isinstance(data, dict) and "open" in data:
            payload_data = data
        elif isinstance(data, dict) and "data" in data:
            payload_data = data["data"]
        else:
            raise ValueError(f"Unexpected API response format: {str(data)[:200]}")

        opens = payload_data.get("open") or []
        highs = payload_data.get("high") or []
        lows = payload_data.get("low") or []
        closes = payload_data.get("close") or []
        volumes = payload_data.get("volume") or [0] * len(closes)
        timestamps = payload_data.get("start_Time") or payload_data.get("timestamp") or []

        if not closes:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        rows = []
        for i in range(len(closes)):
            if not closes[i]:
                continue
            ts = timestamps[i] if i < len(timestamps) else None
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts)
            elif isinstance(ts, str):
                ts = pd.Timestamp(ts)
            rows.append({
                "timestamp": ts,
                "open": float(opens[i]) if i < len(opens) and opens[i] else 0.0,
                "high": float(highs[i]) if i < len(highs) and highs[i] else 0.0,
                "low": float(lows[i]) if i < len(lows) and lows[i] else 0.0,
                "close": float(closes[i]),
                "volume": float(volumes[i]) if i < len(volumes) and volumes[i] else 0.0,
            })

        df = pd.DataFrame(rows)
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
        return df
