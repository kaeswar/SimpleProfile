"""TradingView Lightweight Charts candlestick widget via QWebEngineView.

Renders OHLCV candlestick chart with POC/VAH/VAL overlay lines and
Value Area shading. Communicates with the JS chart via page.runJavaScript().
"""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
from PySide6.QtCore import QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

from engine import ProfileResult


# CDN URL for lightweight-charts (v4)
LWC_CDN = "https://unpkg.com/lightweight-charts@4/dist/lightweight-charts.standalone.production.js"

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#131722; overflow:hidden; }
  #chart { width:100vw; height:100vh; }
</style>
</head><body>
<div id="chart"></div>
<script src="__LWC_CDN__"></script>
<script>
const chartDiv = document.getElementById('chart');
const chart = LightweightCharts.createChart(chartDiv, {
    layout: {
        background: { type: 'solid', color: '#131722' },
        textColor: '#d1d4dc',
    },
    grid: {
        vertLines: { color: '#1e222d' },
        horzLines: { color: '#1e222d' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#2B2B43' },
    timeScale: {
        borderColor: '#2B2B43',
        timeVisible: true,
        secondsVisible: false,
    },
});

const candleSeries = chart.addSeries(
    LightweightCharts.CandlestickSeries, {
    upColor: '#26a69a',
    downColor: '#ef5350',
    borderVisible: false,
    wickUpColor: '#26a69a',
    wickDownColor: '#ef5350',
});

const volumeSeries = chart.addSeries(
    LightweightCharts.HistogramSeries, {
    color: '#26a69a',
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
});
chart.priceScale('volume').applyOptions({
    scaleMargins: { top: 0.8, bottom: 0 },
});

// Store line refs for cleanup
let overlayLines = [];
let overlayPanes = [];

function clearOverlays() {
    overlayLines.forEach(l => candleSeries.removePriceLine(l));
    overlayLines = [];
}

function setData(candles, volumes) {
    candleSeries.setData(candles);
    volumeSeries.setData(volumes.map((v, i) => ({
        time: candles[i].time,
        value: v,
        color: candles[i].close >= candles[i].open ? '#26a69a80' : '#ef535080',
    })));
    chart.timeScale().fitContent();
}

function addKeyLevels(poc, vah, val) {
    clearOverlays();
    const pocLine = candleSeries.createPriceLine({
        price: poc, color: '#d62728', lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true, title: 'POC',
    });
    const vahLine = candleSeries.createPriceLine({
        price: vah, color: '#2ca02c', lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true, title: 'VAH',
    });
    const valLine = candleSeries.createPriceLine({
        price: val, color: '#2ca02c', lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true, title: 'VAL',
    });
    overlayLines.push(pocLine, vahLine, valLine);
}

// Expose to Python
window.setData = setData;
window.addKeyLevels = addKeyLevels;
window.clearOverlays = clearOverlays;
</script>
</body></html>"""


class CandlestickChart(QWebEngineView):
    """QWebEngineView that shows a TradingView Lightweight Charts candlestick."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ready = False
        self._pending_calls: list[str] = []
        html = _HTML_TEMPLATE.replace("__LWC_CDN__", LWC_CDN)
        self.loadFinished.connect(self._on_load)
        self.setHtml(html)

    def _on_load(self, ok: bool):
        self._ready = ok
        if ok:
            for js in self._pending_calls:
                self.page().runJavaScript(js)
            self._pending_calls.clear()

    def _run_js(self, js: str):
        if self._ready:
            self.page().runJavaScript(js)
        else:
            self._pending_calls.append(js)

    # ---------- public API ----------

    def set_candle_data(self, df: pd.DataFrame):
        """Feed a 1-min OHLCV dataframe (columns: timestamp, open, high, low, close, volume)."""
        if df.empty:
            return
        candles = []
        volumes = []
        for _, row in df.iterrows():
            ts = row["timestamp"]
            if isinstance(ts, str):
                ts = pd.Timestamp(ts)
            # lightweight-charts wants UTC unix seconds
            t = int(ts.timestamp())
            candles.append({
                "time": t,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })
            volumes.append(float(row.get("volume", 0)))
        js = f"setData({json.dumps(candles)}, {json.dumps(volumes)});"
        self._run_js(js)

    def set_key_levels(self, poc: float, vah: float, val: float):
        """Draw POC, VAH, VAL as horizontal price lines."""
        js = f"addKeyLevels({poc}, {vah}, {val});"
        self._run_js(js)

    def clear_chart(self):
        self._run_js("clearOverlays();")

    def update_from_result(self, r: ProfileResult, df: pd.DataFrame):
        """Convenience: set candle data + key levels from engine result."""
        self.set_candle_data(df)
        self.set_key_levels(r.poc, r.vah, r.val)
