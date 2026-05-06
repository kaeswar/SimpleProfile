"""Desktop Market Profile Creator (PySide6).

Run: python app.pyw
"""
from __future__ import annotations

import sys
import os
from dataclasses import replace
from datetime import datetime

import pandas as pd
from PySide6.QtCore import Qt, QDate, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QLineEdit, QFileDialog, QComboBox, QDoubleSpinBox, QSpinBox,
    QListWidget, QListWidgetItem, QRadioButton, QButtonGroup, QDateEdit,
    QCheckBox, QGroupBox, QMessageBox, QSplitter, QStatusBar,
    QDialog, QDialogButtonBox,
)

from engine import (
    PERIOD_OPTIONS, ProfileResult, compute_composite,
    compute_profile, list_csv_files, load_csv, minute_key_levels,
)
from chart_unified import UnifiedChart
from dhan_live import DhanLiveFetcher, DEFAULT_SECURITY_ID

DEFAULT_FOLDER = r"D:\Learning\Python\KD_Fetcher\data\NIFTY-Apr2026-FUT"


class ProfileApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Once a Wanderer's Contribution - Simple Profile - எளியவர்களின் முயற்சி")

        self.files: list[tuple] = []  # [(datetime, path)]

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_controls())
        splitter.addWidget(self._build_chart())
        splitter.setStretchFactor(0, 0)  # controls panel: fixed
        splitter.setStretchFactor(1, 1)  # chart: takes all remaining space
        self.setCentralWidget(splitter)
        self._splitter = splitter

        self.setStatusBar(QStatusBar())
        self._load_folder(DEFAULT_FOLDER)

    def showEvent(self, event):
        super().showEvent(event)
        # Set splitter sizes after window geometry is known
        self._splitter.setSizes([320, self.width() - 320])

    def show(self):
        super().showMaximized()

    # ---------- UI ----------
    def _build_controls(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # Folder
        fbox = QGroupBox("Data Folder")
        fl = QHBoxLayout(fbox)
        self.folder_edit = QLineEdit(DEFAULT_FOLDER)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_folder)
        fl.addWidget(self.folder_edit); fl.addWidget(btn_browse)
        lay.addWidget(fbox)

        # Mode
        mbox = QGroupBox("Mode")
        ml = QVBoxLayout(mbox)
        self.mode_single = QRadioButton("Single day")
        self.mode_range = QRadioButton("Composite — date range")
        self.mode_multi = QRadioButton("Composite — multi-select")
        self.mode_continuous = QRadioButton("Continuous (last N days)")
        self.mode_weekly = QRadioButton("Continuous Weekly (last N weeks)")
        self.mode_live = QRadioButton("Live (Dhan API)")
        self.mode_single.setChecked(True)
        grp = QButtonGroup(self)
        for r in (self.mode_single, self.mode_range, self.mode_multi,
                  self.mode_continuous, self.mode_weekly, self.mode_live):
            grp.addButton(r); ml.addWidget(r)
            r.toggled.connect(self._sync_mode)
        lay.addWidget(mbox)

        # Continuous controls
        self.cont_box = QGroupBox("Continuous Settings")
        cont_lay = QVBoxLayout(self.cont_box)
        cont_row1 = QHBoxLayout()
        cont_row1.addWidget(QLabel("N days"))
        self.cont_n = QSpinBox(); self.cont_n.setRange(2, 60); self.cont_n.setValue(10)
        cont_row1.addWidget(self.cont_n)
        cont_lay.addLayout(cont_row1)
        cont_row2 = QHBoxLayout()
        self.cont_anchor_cb = QCheckBox("Anchor end date")
        cont_row2.addWidget(self.cont_anchor_cb)
        self.cont_anchor_date = QDateEdit(); self.cont_anchor_date.setCalendarPopup(True)
        cont_row2.addWidget(self.cont_anchor_date)
        cont_lay.addLayout(cont_row2)
        lay.addWidget(self.cont_box)

        # Weekly controls
        self.weekly_box = QGroupBox("Weekly Settings")
        weekly_lay = QVBoxLayout(self.weekly_box)
        weekly_row1 = QHBoxLayout()
        weekly_row1.addWidget(QLabel("N weeks"))
        self.weekly_n = QSpinBox(); self.weekly_n.setRange(1, 52); self.weekly_n.setValue(4)
        weekly_row1.addWidget(self.weekly_n)
        weekly_row1.addWidget(QLabel("Week starts"))
        self.week_start_combo = QComboBox()
        self.week_start_combo.addItems(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        self.week_start_combo.setCurrentText("Wednesday")
        weekly_row1.addWidget(self.week_start_combo)
        weekly_lay.addLayout(weekly_row1)
        weekly_row2 = QHBoxLayout()
        self.weekly_anchor_cb = QCheckBox("Anchor end date")
        weekly_row2.addWidget(self.weekly_anchor_cb)
        self.weekly_anchor_date = QDateEdit(); self.weekly_anchor_date.setCalendarPopup(True)
        weekly_row2.addWidget(self.weekly_anchor_date)
        weekly_lay.addLayout(weekly_row2)
        lay.addWidget(self.weekly_box)

        # Live controls
        self.live_box = QGroupBox("Live Settings")
        live_lay = QVBoxLayout(self.live_box)
        live_row1 = QHBoxLayout()
        live_row1.addWidget(QLabel("Security ID"))
        self.live_sec_id = QLineEdit(DEFAULT_SECURITY_ID)
        self.live_sec_id.setFixedWidth(80)
        live_row1.addWidget(self.live_sec_id)
        live_row1.addWidget(QLabel("Days"))
        self.live_n_days = QSpinBox()
        self.live_n_days.setRange(1, 30)
        self.live_n_days.setValue(10)
        live_row1.addWidget(self.live_n_days)
        live_row1.addStretch()
        live_lay.addLayout(live_row1)
        live_row2 = QHBoxLayout()
        self.live_auto_cb = QCheckBox("Auto-refresh (3 min)")
        self.live_auto_cb.setChecked(True)
        self.live_auto_cb.toggled.connect(self._toggle_live_timer)
        live_row2.addWidget(self.live_auto_cb)
        self.live_fetch_btn = QPushButton("Fetch Now")
        self.live_fetch_btn.clicked.connect(self._on_live_fetch)
        live_row2.addWidget(self.live_fetch_btn)
        live_lay.addLayout(live_row2)
        self.live_status = QLabel("Status: Idle")
        self.live_status.setStyleSheet("color: #888; font-size: 10px;")
        live_lay.addWidget(self.live_status)
        lay.addWidget(self.live_box)

        # Live timer
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(3 * 60 * 1000)  # 3 minutes
        self._live_timer.timeout.connect(self._on_live_fetch)

        # Date range
        self.range_box = QGroupBox("Date Range")
        rl = QHBoxLayout(self.range_box)
        self.date_from = QDateEdit(); self.date_from.setCalendarPopup(True)
        self.date_to = QDateEdit(); self.date_to.setCalendarPopup(True)
        rl.addWidget(QLabel("From")); rl.addWidget(self.date_from)
        rl.addWidget(QLabel("To")); rl.addWidget(self.date_to)
        lay.addWidget(self.range_box)

        # Day list (used by single + multi-select)
        self.list_box = QGroupBox("Days")
        ll = QVBoxLayout(self.list_box)
        self.day_list = QListWidget()
        ll.addWidget(self.day_list)
        lay.addWidget(self.list_box)

        # Action buttons
        btn_row = QHBoxLayout()
        self.draw_btn = QPushButton("Draw Profile")
        self.draw_btn.clicked.connect(self._on_draw)
        btn_row.addWidget(self.draw_btn)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self._on_settings)
        btn_row.addWidget(self.settings_btn)
        lay.addLayout(btn_row)

        # Initialize parameters with defaults (edited via Settings dialog)
        self._init_params()

        lay.addStretch(1)

        about_btn = QPushButton("About")
        about_btn.setFixedWidth(60)
        about_btn.clicked.connect(self._on_about)
        lay.addWidget(about_btn, alignment=Qt.AlignLeft)

        self._sync_mode()
        return w

    def _init_params(self):
        """Initialize all parameter values (no widgets on main panel)."""
        self._tick_size = 0.05
        self._bin_size = 10.0
        self._period_text = "30 min"
        self._va_pct = 0.68
        self._show_letters = True
        self._style_text = "Merged"
        self._metric_text = "TPO (brackets)"
        self._show_candle = False
        self._candle_tf = "5 min"
        self._visibility = {
            "poc": True, "vah": True, "val": True,
            "open": True, "close": True, "mid": True,
            "ib_high": True, "ib_low": True,
        }
        self._ib_minutes = 60

    def _build_chart(self) -> QWidget:
        self.chart = UnifiedChart()
        return self.chart

    # ---------- Helpers ----------
    def _sync_mode(self):
        is_cont = self.mode_continuous.isChecked()
        is_weekly = self.mode_weekly.isChecked()
        is_range = self.mode_range.isChecked()
        is_live = self.mode_live.isChecked()
        self.range_box.setVisible(is_range)
        self.cont_box.setVisible(is_cont)
        self.weekly_box.setVisible(is_weekly)
        self.live_box.setVisible(is_live)
        self.list_box.setVisible(not is_range and not is_cont and not is_weekly and not is_live)
        self.day_list.setSelectionMode(
            QListWidget.MultiSelection if self.mode_multi.isChecked() else QListWidget.SingleSelection
        )
        # Stop live timer if switching away from live mode
        if not is_live and self._live_timer.isActive():
            self._live_timer.stop()
            self.live_status.setText("Status: Stopped")

    def _resample_candles(self, df: pd.DataFrame) -> pd.DataFrame:
        """Resample 1-min OHLCV data to the selected candlestick timeframe."""
        tf_map = {"1 min": "1min", "5 min": "5min", "15 min": "15min",
                  "30 min": "30min", "1 hour": "1h", "4 hours": "4h", "1 day": "1D"}
        tf = tf_map.get(self._candle_tf, "5min")
        if tf == "1min":
            return df
        df = df.set_index("timestamp")
        resampled = df.resample(tf).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna(subset=["open"]).reset_index()
        return resampled

    def _browse_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Select data folder", self.folder_edit.text())
        if d:
            self._load_folder(d)

    def _load_folder(self, folder: str):
        self.folder_edit.setText(folder)
        self.files = list_csv_files(folder)
        self.day_list.clear()
        for dt, path in self.files:
            item = QListWidgetItem(dt.strftime("%Y-%m-%d  ") + os.path.basename(path))
            item.setData(Qt.UserRole, path)
            self.day_list.addItem(item)
        if self.files:
            first, last = self.files[0][0], self.files[-1][0]
            self.date_from.setDate(QDate(first.year, first.month, first.day))
            self.date_to.setDate(QDate(last.year, last.month, last.day))
            self.cont_anchor_date.setDate(QDate(last.year, last.month, last.day))
            self.weekly_anchor_date.setDate(QDate(last.year, last.month, last.day))
            self.day_list.setCurrentRow(len(self.files) - 1)
        self.statusBar().showMessage(f"Loaded {len(self.files)} files from {folder}")

    def _selected_paths(self) -> list[str]:
        if self.mode_range.isChecked():
            d1 = self.date_from.date().toPython()
            d2 = self.date_to.date().toPython()
            if d2 < d1:
                d1, d2 = d2, d1
            return [p for dt, p in self.files if d1 <= dt.date() <= d2]
        if self.mode_continuous.isChecked():
            n = self.cont_n.value()
            pool = self.files
            if self.cont_anchor_cb.isChecked():
                anchor = self.cont_anchor_date.date().toPython()
                pool = [(dt, p) for dt, p in self.files if dt.date() <= anchor]
            return [p for _, p in pool[-n:]]
        if self.mode_multi.isChecked():
            return [it.data(Qt.UserRole) for it in self.day_list.selectedItems()]
        cur = self.day_list.currentItem()
        return [cur.data(Qt.UserRole)] if cur else []

    def _group_into_weeks(self, file_list: list[tuple]) -> list[list[tuple]]:
        """Group (datetime, path) tuples into weeks based on configured start day.
        Week starts on the configured weekday. If that day has no data, the week
        still begins on that calendar day — files are assigned to the week whose
        start day is on or before their date.
        """
        day_map = {"Monday": 0, "Tuesday": 1, "Wednesday": 2,
                   "Thursday": 3, "Friday": 4}
        start_dow = day_map[self.week_start_combo.currentText()]

        if not file_list:
            return []

        from datetime import timedelta

        weeks: list[list[tuple]] = []
        current_week: list[tuple] = []
        # Find the week-start on or before the first file's date
        first_date = file_list[0][0].date()
        days_since_start = (first_date.weekday() - start_dow) % 7
        current_week_start = first_date - timedelta(days=days_since_start)

        for dt, path in file_list:
            d = dt.date()
            # Check if this date belongs to the current week
            days_since = (d - current_week_start).days
            if days_since >= 7:
                # Start new week(s) — advance week_start
                if current_week:
                    weeks.append(current_week)
                    current_week = []
                # Advance to the correct week
                while (d - current_week_start).days >= 7:
                    current_week_start += timedelta(days=7)
            current_week.append((dt, path))

        if current_week:
            weeks.append(current_week)
        return weeks

    # ---------- Draw ----------
    def _toggle_live_timer(self, checked: bool):
        if checked and self.mode_live.isChecked():
            self._live_timer.start()
            self.live_status.setText("Status: Auto-refresh ON (every 3 min)")
        else:
            self._live_timer.stop()
            self.live_status.setText("Status: Auto-refresh OFF")

    def _on_live_fetch(self):
        """Fetch live data from Dhan API and draw profile."""
        sec_id = self.live_sec_id.text().strip() or DEFAULT_SECURITY_ID
        n_days = self.live_n_days.value()
        self.live_status.setText(f"Status: Fetching {n_days} day(s)...")
        self.live_status.repaint()
        QApplication.processEvents()

        try:
            fetcher = DhanLiveFetcher.from_credentials_file(security_id=sec_id)
            if not fetcher.is_configured():
                QMessageBox.warning(self, "Credentials Missing",
                    "credentials.txt not found or incomplete.\n"
                    "Create a file 'credentials.txt' in the app folder with:\n"
                    "  client_id=YOUR_CLIENT_ID\n"
                    "  access_token=YOUR_ACCESS_TOKEN")
                self.live_status.setText("Status: No credentials")
                return

            tick = self._tick_size
            bin_sz = self._bin_size
            period = PERIOD_OPTIONS[self._period_text]
            va = self._va_pct
            ib_min = self._ib_minutes
            from engine import SESSION_START, SESSION_END

            if n_days == 1:
                # Single day — today only
                df = fetcher.fetch_today()
                if df.empty:
                    self.live_status.setText("Status: No data (market closed?)")
                    return
                t = df["timestamp"].dt.time
                df = df[(t >= SESSION_START) & (t <= SESSION_END)].copy()
                if df.empty:
                    self.live_status.setText("Status: No data in session hours")
                    return

                result = compute_profile(df, tick_size=tick, period_minutes=period,
                                         value_area_pct=va, title="LIVE — Nifty Futures",
                                         ib_minutes=ib_min)
                all_dfs = [df]
                style_merged = self._style_text == "Merged"
                view_mode = "merged" if style_merged else "expanded"
                total_bars = len(df)
            else:
                # Multi-day continuous
                day_data = fetcher.fetch_last_n_days(n_days)
                if not day_data:
                    self.live_status.setText("Status: No data found")
                    return

                daily_profiles = []
                all_dfs = []
                for d, df in day_data:
                    t = df["timestamp"].dt.time
                    df = df[(t >= SESSION_START) & (t <= SESSION_END)].copy()
                    if df.empty:
                        continue
                    all_dfs.append(df)
                    daily_profiles.append(compute_profile(
                        df, tick_size=tick, period_minutes=period,
                        value_area_pct=va, title=d.strftime("%d-%b"),
                        ib_minutes=ib_min))

                if not daily_profiles:
                    self.live_status.setText("Status: No data in session hours")
                    return

                first_d = day_data[0][0].strftime("%d-%b")
                last_d = day_data[-1][0].strftime("%d-%b")
                title = f"LIVE — {len(daily_profiles)} days ({first_d} → {last_d})"
                result = compute_composite(daily_profiles, value_area_pct=va,
                                           tick_size=tick, title=title)
                view_mode = "continuous"
                total_bars = sum(len(df) for df in all_dfs)

            metric = "minute" if self._metric_text.startswith("Minute") else "tpo"
            style_merged = self._style_text == "Merged"

            candle_df = None
            if self._show_candle and all_dfs:
                candle_df = pd.concat(all_dfs, ignore_index=True).sort_values("timestamp")
                candle_df = self._resample_candles(candle_df)

            self.chart.render(result, bin_sz, candle_df=candle_df,
                              show_letters=self._show_letters,
                              view_mode=view_mode, count_metric=metric,
                              style="merged" if style_merged else "expanded",
                              visibility=self._visibility,
                              ib_minutes=ib_min)

            now = datetime.now().strftime("%H:%M:%S")
            self.live_status.setText(f"Status: OK — {total_bars} bars @ {now}")
            self.statusBar().showMessage(f"Live: {result.title} | POC={result.poc:.2f}")

            # Start auto-refresh if enabled
            if self.live_auto_cb.isChecked() and not self._live_timer.isActive():
                self._live_timer.start()

        except Exception as e:
            self.live_status.setText(f"Status: Error — {str(e)[:60]}")
            QMessageBox.critical(self, "Live Fetch Error", str(e))

    def _on_about(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("About — Simple Profile")
        dlg.setFixedWidth(420)
        vl = QVBoxLayout(dlg)

        info = QLabel(
            "<h3>Once a Wanderer's Contribution - Simple Profile</h3>"
            "<p style='font-size:13px; color:#555;'>எளியவர்களின் முயற்சி</p>"
            "<p>A desktop application for generating and visualizing Market Profile charts "
            "for Nifty Futures (and other instruments). Supports single-day, multi-day composite, "
            "continuous, and weekly profile modes with TPO and minute-density metrics. "
            "Includes TradingView-style candlestick overlay, Initial Balance, Value Area, "
            "and interactive pan/zoom/stretch.</p>"
            "<p><b>Developed with Claude Code</b> (Anthropic AI) — the entire codebase was "
            "written and iterated through AI-assisted development.</p>"
            "<p>This software is <b>free to use and free to enhance</b>. It is provided as-is "
            "and may contain errors — it is not perfect. Use at your own discretion.</p>"
            "<p>This phase of development was driven by the need and desire of "
            "<b>Kaeswar</b> — <a href='mailto:kaeswar@gmail.com'>kaeswar@gmail.com</a></p>"
            "<p>If you find this useful, please consider contributing a little — "
            "it will be encouraging and help keep this project alive.</p>"
            "<hr>"
            "<p><b>Donate via UPI:</b> <code>kaeswar@oksbi</code></p>"
        )
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        vl.addWidget(info)

        # QR Code
        qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "donate_qr.png")
        if os.path.exists(qr_path):
            from PySide6.QtGui import QPixmap
            qr_label = QLabel()
            pixmap = QPixmap(qr_path).scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            qr_label.setPixmap(pixmap)
            qr_label.setAlignment(Qt.AlignCenter)
            vl.addWidget(qr_label)
            scan_lbl = QLabel("<p style='text-align:center; color:#555;'>Scan with GPay / PhonePe / Paytm</p>")
            scan_lbl.setAlignment(Qt.AlignCenter)
            vl.addWidget(scan_lbl)

        footer = QLabel("<p style='color:#888; font-size:10px;'>Built with Python, PySide6, "
                        "TradingView Lightweight Charts &amp; HTML5 Canvas.</p>")
        footer.setWordWrap(True)
        vl.addWidget(footer)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        vl.addWidget(close_btn)
        dlg.exec()

    def _on_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        dlg.setMinimumWidth(420)
        vl = QVBoxLayout(dlg)

        # --- Parameters ---
        pbox = QGroupBox("Parameters")
        pl = QVBoxLayout(pbox)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Tick size"))
        tick_spin = QDoubleSpinBox(); tick_spin.setDecimals(4)
        tick_spin.setRange(0.0001, 100.0); tick_spin.setSingleStep(0.05)
        tick_spin.setValue(self._tick_size)
        row1.addWidget(tick_spin)
        row1.addWidget(QLabel("Bin size"))
        bin_spin = QDoubleSpinBox(); bin_spin.setDecimals(2)
        bin_spin.setRange(0.05, 1000.0); bin_spin.setSingleStep(1.0)
        bin_spin.setValue(self._bin_size)
        row1.addWidget(bin_spin)
        pl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("TPO period"))
        period_combo = QComboBox()
        for k in PERIOD_OPTIONS:
            period_combo.addItem(k)
        period_combo.setCurrentText(self._period_text)
        row2.addWidget(period_combo)
        row2.addWidget(QLabel("Value Area %"))
        va_spin = QDoubleSpinBox(); va_spin.setRange(0.50, 0.95)
        va_spin.setSingleStep(0.01); va_spin.setValue(self._va_pct)
        row2.addWidget(va_spin)
        pl.addLayout(row2)

        row3 = QHBoxLayout()
        letters_cb = QCheckBox("Show letters (A, B, C…)")
        letters_cb.setChecked(self._show_letters)
        row3.addWidget(letters_cb)
        row3.addWidget(QLabel("Style"))
        style_combo = QComboBox()
        style_combo.addItems(["Merged", "Expanded (time x-axis)"])
        style_combo.setCurrentText(self._style_text)
        row3.addWidget(style_combo)
        pl.addLayout(row3)

        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Count metric"))
        metric_combo = QComboBox()
        metric_combo.addItems(["TPO (brackets)", "Minute (1-min bars)"])
        metric_combo.setCurrentText(self._metric_text)
        row4.addWidget(metric_combo)
        pl.addLayout(row4)

        row5 = QHBoxLayout()
        candle_cb = QCheckBox("Show Candlestick Chart")
        candle_cb.setChecked(self._show_candle)
        row5.addWidget(candle_cb)
        row5.addWidget(QLabel("TF"))
        candle_tf_combo = QComboBox()
        candle_tf_combo.addItems(["1 min", "5 min", "15 min", "30 min", "1 hour", "4 hours", "1 day"])
        candle_tf_combo.setCurrentText(self._candle_tf)
        row5.addWidget(candle_tf_combo)
        pl.addLayout(row5)
        vl.addWidget(pbox)

        # --- Show / Hide Overlays ---
        grp = QGroupBox("Show / Hide Overlays")
        gl = QVBoxLayout(grp)
        vis_cbs = {}
        for key, label in [
            ("poc", "POC (Point of Control)"),
            ("vah", "VAH (Value Area High)"),
            ("val", "VAL (Value Area Low)"),
            ("open", "Open"),
            ("close", "Close"),
            ("mid", "Mid Point"),
            ("ib_high", "IB High (Initial Balance High)"),
            ("ib_low", "IB Low (Initial Balance Low)"),
        ]:
            cb = QCheckBox(label)
            cb.setChecked(self._visibility[key])
            vis_cbs[key] = cb
            gl.addWidget(cb)
        vl.addWidget(grp)

        # IB duration
        ib_row = QHBoxLayout()
        ib_row.addWidget(QLabel("IB Duration (minutes):"))
        ib_spin = QSpinBox()
        ib_spin.setRange(15, 240)
        ib_spin.setValue(self._ib_minutes)
        ib_spin.setSingleStep(15)
        ib_row.addWidget(ib_spin)
        vl.addLayout(ib_row)

        # Buttons
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        vl.addWidget(btns)

        if dlg.exec() == QDialog.Accepted:
            self._tick_size = tick_spin.value()
            self._bin_size = bin_spin.value()
            self._period_text = period_combo.currentText()
            self._va_pct = va_spin.value()
            self._show_letters = letters_cb.isChecked()
            self._style_text = style_combo.currentText()
            self._metric_text = metric_combo.currentText()
            self._show_candle = candle_cb.isChecked()
            self._candle_tf = candle_tf_combo.currentText()
            self._ib_minutes = ib_spin.value()
            for key, cb in vis_cbs.items():
                self._visibility[key] = cb.isChecked()

    def _on_draw(self):
        if self.mode_live.isChecked():
            self._on_live_fetch()
            return
        is_weekly = self.mode_weekly.isChecked()
        is_continuous = self.mode_continuous.isChecked()
        is_single = self.mode_single.isChecked()

        # For weekly mode, handle path selection differently
        if is_weekly:
            n_weeks = self.weekly_n.value()
            pool = self.files
            if self.weekly_anchor_cb.isChecked():
                anchor = self.weekly_anchor_date.date().toPython()
                pool = [(dt, p) for dt, p in self.files if dt.date() <= anchor]
            weeks = self._group_into_weeks(pool)
            weeks = weeks[-n_weeks:]  # last N weeks
            if not weeks:
                QMessageBox.warning(self, "No data", "No weeks found in data.")
                return
            paths = [p for week in weeks for _, p in week]
        else:
            paths = self._selected_paths()

        if not paths:
            QMessageBox.warning(self, "No data", "Select at least one day.")
            return

        tick = self._tick_size
        bin_sz = self._bin_size
        period = PERIOD_OPTIONS[self._period_text]
        va = self._va_pct
        show_letters = self._show_letters
        ib_min = self._ib_minutes

        try:
            all_dfs = []
            if is_weekly:
                # Build one composite profile per week
                weekly_profiles = []
                for week in weeks:
                    week_daily = []
                    for dt, p in week:
                        df = load_csv(p)
                        if df.empty:
                            continue
                        all_dfs.append(df)
                        week_daily.append(compute_profile(
                            df, tick_size=tick, period_minutes=period,
                            value_area_pct=va, title=os.path.basename(p), ib_minutes=ib_min))
                    if week_daily:
                        first_d = week[0][0].strftime("%d-%b")
                        last_d = week[-1][0].strftime("%d-%b")
                        wp = compute_composite(week_daily, value_area_pct=va,
                                               tick_size=tick,
                                               title=f"{first_d}→{last_d}")
                        weekly_profiles.append(wp)
                if not weekly_profiles:
                    QMessageBox.warning(self, "Empty", "No data after session filter.")
                    return
                # Build a meta-composite whose components are the weekly composites
                title = f"Weekly ({len(weekly_profiles)} weeks)"
                result = compute_composite(weekly_profiles, value_area_pct=va,
                                           tick_size=tick, title=title)
                # Override components to be the weekly profiles (for continuous rendering)
                result = replace(result, components=weekly_profiles)

            elif is_single:
                df = load_csv(paths[0])
                all_dfs.append(df)
                result = compute_profile(df, tick_size=tick, period_minutes=period,
                                         value_area_pct=va,
                                         title=os.path.basename(paths[0]),
                                         ib_minutes=ib_min)
            else:
                daily = []
                for p in paths:
                    df = load_csv(p)
                    if df.empty:
                        continue
                    all_dfs.append(df)
                    daily.append(compute_profile(df, tick_size=tick, period_minutes=period,
                                                 value_area_pct=va,
                                                 title=os.path.basename(p),
                                                 ib_minutes=ib_min))
                if not daily:
                    QMessageBox.warning(self, "Empty", "No rows after session filter.")
                    return
                if is_continuous:
                    title = f"Continuous ({len(daily)} days: {daily[0].session_date.strftime('%d-%b')} → {daily[-1].session_date.strftime('%d-%b')})"
                else:
                    title = f"Composite ({len(daily)} days)"
                result = compute_composite(daily, value_area_pct=va, tick_size=tick,
                                           title=title)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        style_merged = self._style_text == "Merged"
        if is_continuous or is_weekly:
            view_mode = "continuous"
        else:
            view_mode = "merged" if style_merged else "expanded"
        metric = "minute" if self._metric_text.startswith("Minute") else "tpo"

        # Combine all dataframes for candlestick (only if enabled)
        candle_df = None
        if self._show_candle:
            candle_df = pd.concat(all_dfs, ignore_index=True).sort_values("timestamp")
            candle_df = self._resample_candles(candle_df)

        # Render unified chart (candlestick + profile)
        cur_style = "merged" if style_merged else "expanded"
        self.chart.render(result, bin_sz, candle_df=candle_df,
                          show_letters=show_letters,
                          view_mode=view_mode, count_metric=metric,
                          style=cur_style,
                          visibility=self._visibility,
                          ib_minutes=self._ib_minutes)

        self.statusBar().showMessage(f"Rendered: {result.title}")


def main():
    app = QApplication(sys.argv)
    w = ProfileApp()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
