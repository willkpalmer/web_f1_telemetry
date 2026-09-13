"""
OpenF1 Lap Viewer
=================
A small Windows desktop app that pulls historical Formula 1 session data
from the OpenF1 API (https://openf1.org) and plots it lap-by-lap for
whichever drivers/cars you select.

Run with:  python main.py
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import matplotlib
matplotlib.use("QtAgg")  # works with both PyQt and PySide bindings

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import openf1_client as api

# Display name -> (JSON field on the /laps object, y-axis label)
METRICS: dict[str, tuple[str, str]] = {
    "Lap Time": ("lap_duration", "Lap time (s)"),
    "Sector 1": ("duration_sector_1", "Sector 1 time (s)"),
    "Sector 2": ("duration_sector_2", "Sector 2 time (s)"),
    "Sector 3": ("duration_sector_3", "Sector 3 time (s)"),
    "Speed Trap": ("st_speed", "Speed (km/h)"),
}

# Display name -> (JSON field on the /car_data object, y-axis label)
# OpenF1's free telemetry has no steering-angle channel - this is everything
# it exposes.
TELEMETRY_CHANNELS: dict[str, tuple[str, str]] = {
    "Speed": ("speed", "Speed (km/h)"),
    "Throttle": ("throttle", "Throttle (%)"),
    "Brake": ("brake", "Brake (%)"),
    "Gear": ("n_gear", "Gear"),
    "RPM": ("rpm", "Engine RPM"),
    "DRS": ("drs", "DRS (raw status code)"),
}

FIRST_HISTORICAL_YEAR = 2023


# --------------------------------------------------------------------------
# Background worker plumbing (keeps network calls off the UI thread)
# --------------------------------------------------------------------------

class WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    """Runs `fn(*args, **kwargs)` on a thread-pool thread."""

    def __init__(self, fn: Callable, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception:  # noqa: BLE001 - surface any failure to the UI
            self.signals.error.emit(traceback.format_exc(limit=2))
        else:
            self.signals.finished.emit(result)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OpenF1 Lap Viewer")
        self.resize(1250, 750)

        self.thread_pool = QThreadPool.globalInstance()

        # Caches, keyed by id, so re-plotting after a checkbox change never
        # needs a fresh network call.
        self._drivers_by_session: dict[int, list[dict]] = {}
        self._sessions_by_meeting: dict[int, list[dict]] = {}
        self._laps_by_session: dict[int, list[dict]] = {}
        self._telemetry_cache: dict[tuple[int, int, int], list[dict]] = {}

        # Set just before auto-selecting a meeting at startup so the
        # session cascade picks the specific session already confirmed to
        # have data, instead of defaulting to the earliest session.
        self._pending_startup_session_key: Optional[int] = None

        # Bumped on every user-driven year change, so a slow startup
        # auto-detection probe that's still running can tell it's stale
        # and avoid clobbering a selection the user has since made.
        self._year_selection_generation = 0

        # Keeps in-flight Worker objects alive: QRunnable/QObject instances
        # with no Qt parent are only kept alive by Python's own refcounting,
        # so a Worker with no other reference gets garbage-collected while
        # its background thread is still running, and emitting its signal
        # afterwards raises "Signal source has been deleted".
        self._active_workers: list[Worker] = []

        # State for the telemetry fetch-then-draw join (see _plot_telemetry).
        self._pending_telemetry: Optional[dict[str, Any]] = None
        self._telemetry_fetch_remaining = 0

        self._build_ui()
        self._populate_years()
        self._autoselect_startup_defaults()

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter)

        splitter.addWidget(self._build_selection_panel())

        tabs = QTabWidget()
        tabs.addTab(self._build_lap_summary_tab(), "Lap Summary")
        tabs.addTab(self._build_telemetry_tab(), "Car Telemetry")
        splitter.addWidget(tabs)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 950])

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _build_selection_panel(self) -> QWidget:
        """Year / Grand Prix / Session / Drivers - shared by both tabs."""
        panel = QWidget()
        layout = QVBoxLayout(panel)

        layout.addWidget(QLabel("<b>Year</b>"))
        self.year_combo = QComboBox()
        self.year_combo.currentIndexChanged.connect(self._on_year_changed)
        layout.addWidget(self.year_combo)

        layout.addWidget(QLabel("<b>Grand Prix</b>"))
        self.meeting_combo = QComboBox()
        self.meeting_combo.currentIndexChanged.connect(self._on_meeting_changed)
        layout.addWidget(self.meeting_combo)

        layout.addWidget(QLabel("<b>Session</b>"))
        self.session_combo = QComboBox()
        self.session_combo.currentIndexChanged.connect(self._on_session_changed)
        layout.addWidget(self.session_combo)

        layout.addWidget(QLabel("<b>Drivers</b>"))
        self.driver_list = QListWidget()
        self.driver_list.setSelectionMode(QListWidget.NoSelection)
        layout.addWidget(self.driver_list, stretch=1)

        select_row = QHBoxLayout()
        select_all_btn = QPushButton("All")
        select_all_btn.clicked.connect(lambda: self._set_all_drivers_checked(True))
        select_none_btn = QPushButton("None")
        select_none_btn.clicked.connect(lambda: self._set_all_drivers_checked(False))
        select_row.addWidget(select_all_btn)
        select_row.addWidget(select_none_btn)
        layout.addLayout(select_row)

        return panel

    def _build_lap_summary_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        controls_layout.addWidget(QLabel("<b>Metric</b>"))
        self.metric_combo = QComboBox()
        self.metric_combo.addItems(list(METRICS.keys()))
        controls_layout.addWidget(self.metric_combo)

        self.exclude_pit_laps_check = QCheckBox("Exclude pit in/out laps")
        self.exclude_pit_laps_check.setChecked(True)
        controls_layout.addWidget(self.exclude_pit_laps_check)

        self.invert_time_axis_check = QCheckBox("Faster laps plotted higher")
        controls_layout.addWidget(self.invert_time_axis_check)

        self.plot_button = QPushButton("Plot")
        self.plot_button.setEnabled(False)
        self.plot_button.clicked.connect(self._on_plot_clicked)
        controls_layout.addWidget(self.plot_button)

        controls_layout.addStretch(1)
        controls.setFixedWidth(180)
        layout.addWidget(controls)

        plot_panel = QWidget()
        plot_layout = QVBoxLayout(plot_panel)
        self.figure = Figure(figsize=(7, 5))
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, plot_panel)
        self.ax = self.figure.add_subplot(111)
        self._reset_plot_placeholder()
        plot_layout.addWidget(self.toolbar)
        plot_layout.addWidget(self.canvas, stretch=1)
        layout.addWidget(plot_panel, stretch=1)

        return tab

    def _build_telemetry_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        controls_layout.addWidget(QLabel("<b>Lap</b>"))
        self.lap_combo = QComboBox()
        self.lap_combo.addItem("-- Select session first --", None)
        controls_layout.addWidget(self.lap_combo)

        controls_layout.addWidget(QLabel("<b>Channels</b>"))
        self.channel_checks: dict[str, QCheckBox] = {}
        for name in TELEMETRY_CHANNELS:
            check = QCheckBox(name)
            check.setChecked(name in ("Speed", "Throttle", "Brake", "Gear"))
            controls_layout.addWidget(check)
            self.channel_checks[name] = check

        self.telemetry_plot_button = QPushButton("Plot Telemetry")
        self.telemetry_plot_button.setEnabled(False)
        self.telemetry_plot_button.clicked.connect(self._on_telemetry_plot_clicked)
        controls_layout.addWidget(self.telemetry_plot_button)

        controls_layout.addStretch(1)
        controls.setFixedWidth(180)
        layout.addWidget(controls)

        plot_panel = QWidget()
        plot_layout = QVBoxLayout(plot_panel)
        self.telemetry_figure = Figure(figsize=(7, 5))
        self.telemetry_canvas = FigureCanvas(self.telemetry_figure)
        self.telemetry_toolbar = NavigationToolbar(self.telemetry_canvas, plot_panel)
        self._reset_telemetry_placeholder()
        plot_layout.addWidget(self.telemetry_toolbar)
        plot_layout.addWidget(self.telemetry_canvas, stretch=1)
        layout.addWidget(plot_panel, stretch=1)

        return tab

    def _reset_plot_placeholder(self) -> None:
        self.ax.clear()
        self.ax.text(
            0.5, 0.5, "Select a session and drivers, then click Plot",
            ha="center", va="center", transform=self.ax.transAxes, color="gray",
        )
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.canvas.draw_idle()

    def _reset_telemetry_placeholder(self) -> None:
        self.telemetry_figure.clear()
        ax = self.telemetry_figure.add_subplot(111)
        ax.text(
            0.5, 0.5, "Select a session, lap, drivers and channels,\nthen click Plot Telemetry",
            ha="center", va="center", transform=ax.transAxes, color="gray",
        )
        ax.set_xticks([])
        ax.set_yticks([])
        self.telemetry_canvas.draw_idle()

    # ---- Generic async helpers --------------------------------------------

    def _start_worker(self, worker: Worker) -> None:
        """Starts a Worker while keeping a Python reference to it alive
        until it finishes, so it can't be garbage-collected mid-flight."""
        self._active_workers.append(worker)
        worker.signals.finished.connect(lambda _r, w=worker: self._active_workers.remove(w))
        worker.signals.error.connect(lambda _e, w=worker: self._active_workers.remove(w))
        self.thread_pool.start(worker)

    def _run_async(
        self,
        fn: Callable,
        on_success: Callable[[Any], None],
        status_message: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.status_bar.showMessage(status_message)
        worker = Worker(fn, *args, **kwargs)
        worker.signals.finished.connect(lambda result: self._on_async_success(result, on_success))
        worker.signals.error.connect(self._on_async_error)
        self._start_worker(worker)

    def _on_async_success(self, result: Any, callback: Callable[[Any], None]) -> None:
        self.status_bar.clearMessage()
        callback(result)

    def _on_async_error(self, message: str) -> None:
        self.status_bar.clearMessage()
        QMessageBox.critical(self, "OpenF1 request failed", message.strip().splitlines()[-1])

    def _checked_drivers(self) -> list[dict]:
        return [
            self.driver_list.item(i).data(Qt.UserRole)
            for i in range(self.driver_list.count())
            if self.driver_list.item(i).checkState() == Qt.Checked
        ]

    # ---- Cascading selection: Year -> Meeting -> Session -> Drivers ------

    def _populate_years(self) -> None:
        current_year = datetime.now().year
        self.year_combo.blockSignals(True)
        self.year_combo.addItem("-- Select year --", None)
        for year in range(current_year, FIRST_HISTORICAL_YEAR - 1, -1):
            self.year_combo.addItem(str(year), year)
        self.year_combo.blockSignals(False)

    def _on_year_changed(self) -> None:
        self._year_selection_generation += 1
        year = self.year_combo.currentData()
        self._clear_combo(self.meeting_combo, "-- Select Grand Prix --")
        self._clear_combo(self.session_combo, "-- Select session --")
        self._clear_combo(self.lap_combo, "-- Select session first --")
        self.driver_list.clear()
        self.plot_button.setEnabled(False)
        self.telemetry_plot_button.setEnabled(False)
        if year is None:
            return
        self._run_async(
            api.get_meetings, self._populate_meetings, f"Loading {year} calendar...", year=year
        )

    def _populate_meetings(self, meetings: list[dict], auto_select: bool = True) -> None:
        meetings_sorted = sorted(meetings, key=lambda m: m.get("date_start") or "")
        self.meeting_combo.blockSignals(True)
        for meeting in meetings_sorted:
            self.meeting_combo.addItem(meeting["meeting_name"], meeting["meeting_key"])
        self.meeting_combo.blockSignals(False)
        if meetings_sorted and auto_select:
            self.meeting_combo.setCurrentIndex(1)  # trigger _on_meeting_changed

    def _on_meeting_changed(self) -> None:
        meeting_key = self.meeting_combo.currentData()
        self._clear_combo(self.session_combo, "-- Select session --")
        self._clear_combo(self.lap_combo, "-- Select session first --")
        self.driver_list.clear()
        self.plot_button.setEnabled(False)
        self.telemetry_plot_button.setEnabled(False)
        if meeting_key is None:
            return

        if meeting_key in self._sessions_by_meeting:
            self._populate_sessions(
                self._sessions_by_meeting[meeting_key], self._consume_pending_startup_session()
            )
        else:
            self._run_async(
                api.get_sessions,
                lambda sessions, mk=meeting_key: self._on_sessions_cached(mk, sessions),
                "Loading sessions...",
                meeting_key=meeting_key,
            )

    def _on_sessions_cached(self, meeting_key: int, sessions: list[dict]) -> None:
        self._sessions_by_meeting[meeting_key] = sessions
        if self.meeting_combo.currentData() == meeting_key:
            self._populate_sessions(sessions, self._consume_pending_startup_session())

    def _consume_pending_startup_session(self) -> Optional[int]:
        session_key = self._pending_startup_session_key
        self._pending_startup_session_key = None
        return session_key

    def _populate_sessions(self, sessions: list[dict], select_session_key: Optional[int] = None) -> None:
        sessions_sorted = sorted(sessions, key=lambda s: s.get("date_start") or "")
        self.session_combo.blockSignals(True)
        for session in sessions_sorted:
            self.session_combo.addItem(session["session_name"], session["session_key"])
        self.session_combo.blockSignals(False)
        if not sessions_sorted:
            return
        if select_session_key is not None:
            index = self.session_combo.findData(select_session_key)
            self.session_combo.setCurrentIndex(index if index != -1 else 1)
        else:
            self.session_combo.setCurrentIndex(1)  # trigger _on_session_changed

    def _on_session_changed(self) -> None:
        session_key = self.session_combo.currentData()
        self.driver_list.clear()
        self._clear_combo(self.lap_combo, "-- Select lap --")
        self.plot_button.setEnabled(False)
        self.telemetry_plot_button.setEnabled(False)
        if session_key is None:
            return

        if session_key in self._drivers_by_session:
            self._populate_drivers(session_key, self._drivers_by_session[session_key])
        else:
            self._run_async(
                api.get_drivers,
                lambda drivers, sk=session_key: self._populate_drivers(sk, drivers),
                "Loading drivers...",
                session_key=session_key,
            )

        # Warm the lap-data cache in the background so Plot feels instant,
        # and populate the telemetry tab's Lap dropdown once it's in.
        if session_key in self._laps_by_session:
            self._populate_lap_combo(self._laps_by_session[session_key])
        else:
            worker = Worker(api.get_laps, session_key=session_key)
            worker.signals.finished.connect(
                lambda laps, sk=session_key: self._on_laps_cached(sk, laps)
            )
            self._start_worker(worker)

    def _on_laps_cached(self, session_key: int, laps: list[dict]) -> None:
        self._laps_by_session[session_key] = laps
        if self.session_combo.currentData() == session_key:
            self._populate_lap_combo(laps)

    def _populate_lap_combo(self, laps: list[dict]) -> None:
        lap_numbers = sorted(
            {
                lap["lap_number"]
                for lap in laps
                if lap.get("date_start") and lap.get("lap_duration")
            }
        )
        self.lap_combo.blockSignals(True)
        self.lap_combo.clear()
        self.lap_combo.addItem("-- Select lap --", None)
        for number in lap_numbers:
            self.lap_combo.addItem(f"Lap {number}", number)
        self.lap_combo.blockSignals(False)
        if lap_numbers:
            self.lap_combo.setCurrentIndex(1)
        self.telemetry_plot_button.setEnabled(bool(lap_numbers))

    # ---- Startup default: current year + most recent GP with data --------

    def _autoselect_startup_defaults(self) -> None:
        current_year = datetime.now().year
        generation = self._year_selection_generation
        self._clear_combo(self.meeting_combo, "-- Select Grand Prix --")
        self._clear_combo(self.session_combo, "-- Select session --")
        self._clear_combo(self.lap_combo, "-- Select session first --")
        self._set_year_combo_silently(current_year)
        self._run_async(
            api.get_meetings,
            lambda meetings, y=current_year, g=generation: self._on_startup_meetings(y, meetings, g),
            f"Loading {current_year} calendar...",
            year=current_year,
        )

    def _set_year_combo_silently(self, year: int) -> None:
        index = self.year_combo.findData(year)
        if index == -1:
            return
        self.year_combo.blockSignals(True)
        self.year_combo.setCurrentIndex(index)
        self.year_combo.blockSignals(False)

    def _on_startup_meetings(self, year: int, meetings: list[dict], generation: int) -> None:
        if generation != self._year_selection_generation:
            return  # the user picked a year themselves before this arrived

        now = datetime.now(timezone.utc)
        past_meetings = [
            m for m in meetings
            if not m.get("is_cancelled")
            and m.get("date_start")
            and datetime.fromisoformat(m["date_start"]) <= now
        ]

        if not past_meetings and year > FIRST_HISTORICAL_YEAR:
            # Nothing has happened yet this year (e.g. before the season
            # starts) - fall back to last year's most recent meeting.
            self._set_year_combo_silently(year - 1)
            self._run_async(
                api.get_meetings,
                lambda meetings, y=year - 1, g=generation: self._on_startup_meetings(y, meetings, g),
                f"Loading {year - 1} calendar...",
                year=year - 1,
            )
            return

        self._populate_meetings(meetings, auto_select=False)

        if not past_meetings:
            self.status_bar.clearMessage()
            return

        past_meetings.sort(key=lambda m: m["date_start"], reverse=True)
        self._probe_meetings_for_data(past_meetings, generation)

    def _probe_meetings_for_data(self, candidates: list[dict], generation: int, index: int = 0) -> None:
        if generation != self._year_selection_generation:
            return

        if index >= len(candidates):
            # None of this year's past meetings have queryable session
            # data yet; leave the year/meeting pickers on their placeholders.
            self.status_bar.clearMessage()
            return

        meeting = candidates[index]
        self._run_async(
            api.get_sessions,
            lambda sessions, m=meeting, c=candidates, i=index, g=generation: self._probe_sessions_for_data(
                c, i, m, sorted(sessions, key=lambda s: s.get("date_start") or "", reverse=True), g
            ),
            f"Checking {meeting['meeting_name']}...",
            meeting_key=meeting["meeting_key"],
        )

    def _probe_sessions_for_data(
        self, candidates: list[dict], meeting_index: int, meeting: dict, sessions_desc: list[dict], generation: int
    ) -> None:
        if generation != self._year_selection_generation:
            return

        if not sessions_desc:
            self._probe_meetings_for_data(candidates, generation, meeting_index + 1)
            return

        session, remaining = sessions_desc[0], sessions_desc[1:]
        self._run_async(
            api.get_drivers,
            lambda drivers, m=meeting, s=session, c=candidates, i=meeting_index, r=remaining, full=sessions_desc, g=generation:
                self._on_session_probe_result(c, i, m, full, s, r, drivers, g),
            f"Checking {meeting['meeting_name']} - {session['session_name']}...",
            session_key=session["session_key"],
        )

    def _on_session_probe_result(
        self,
        candidates: list[dict],
        meeting_index: int,
        meeting: dict,
        all_sessions: list[dict],
        session: dict,
        remaining_sessions: list[dict],
        drivers: list[dict],
        generation: int,
    ) -> None:
        if generation != self._year_selection_generation:
            return

        if drivers:
            self._drivers_by_session[session["session_key"]] = drivers
            self._apply_startup_meeting_selection(meeting, all_sessions, session["session_key"], generation)
        elif remaining_sessions:
            self._probe_sessions_for_data(candidates, meeting_index, meeting, remaining_sessions, generation)
        else:
            self._probe_meetings_for_data(candidates, generation, meeting_index + 1)

    def _apply_startup_meeting_selection(
        self, meeting: dict, sessions: list[dict], session_key: int, generation: int
    ) -> None:
        if generation != self._year_selection_generation:
            return

        self._sessions_by_meeting[meeting["meeting_key"]] = sessions
        self._pending_startup_session_key = session_key
        self.status_bar.clearMessage()
        index = self.meeting_combo.findData(meeting["meeting_key"])
        if index != -1:
            self.meeting_combo.setCurrentIndex(index)  # triggers _on_meeting_changed

    def _populate_drivers(self, session_key: int, drivers: list[dict]) -> None:
        self._drivers_by_session[session_key] = drivers

        # If the user has already moved on to a different session by the
        # time this (possibly slow) response arrives, don't repaint the list.
        if self.session_combo.currentData() != session_key:
            return

        drivers_sorted = sorted(drivers, key=lambda d: d.get("driver_number", 0))
        self.driver_list.clear()
        for driver in drivers_sorted:
            label = f"{driver.get('name_acronym', '?')} - {driver.get('team_name', 'Unknown')}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, driver)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            colour_hex = driver.get("team_colour") or "888888"
            item.setIcon(self._colour_swatch(f"#{colour_hex}"))
            self.driver_list.addItem(item)

        self.plot_button.setEnabled(self.driver_list.count() > 0)

    @staticmethod
    def _colour_swatch(colour: str) -> QIcon:
        pixmap = QPixmap(14, 14)
        pixmap.fill(QColor(colour))
        return QIcon(pixmap)

    def _set_all_drivers_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.driver_list.count()):
            self.driver_list.item(i).setCheckState(state)

    @staticmethod
    def _clear_combo(combo: QComboBox, placeholder: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(placeholder, None)
        combo.blockSignals(False)

    # ---- Lap Summary plotting ---------------------------------------------

    def _on_plot_clicked(self) -> None:
        session_key = self.session_combo.currentData()
        if session_key is None:
            return

        checked_drivers = self._checked_drivers()
        if not checked_drivers:
            QMessageBox.information(self, "No drivers selected", "Tick at least one driver to plot.")
            return

        if session_key in self._laps_by_session:
            self._draw_plot(self._laps_by_session[session_key], checked_drivers)
        else:
            self._run_async(
                api.get_laps,
                lambda laps: self._on_laps_ready(session_key, laps, checked_drivers),
                "Loading lap data...",
                session_key=session_key,
            )

    def _on_laps_ready(self, session_key: int, laps: list[dict], checked_drivers: list[dict]) -> None:
        self._laps_by_session[session_key] = laps
        self._draw_plot(laps, checked_drivers)

    def _draw_plot(self, all_laps: list[dict], drivers: list[dict]) -> None:
        metric_label = self.metric_combo.currentText()
        field_name, y_label = METRICS[metric_label]
        exclude_pit_laps = self.exclude_pit_laps_check.isChecked()
        invert_axis = self.invert_time_axis_check.isChecked() and field_name != "st_speed"

        self.ax.clear()
        plotted_any = False

        for driver in drivers:
            driver_number = driver["driver_number"]
            colour = f"#{driver.get('team_colour') or '888888'}"

            driver_laps = [lap for lap in all_laps if lap.get("driver_number") == driver_number]
            if exclude_pit_laps:
                driver_laps = [lap for lap in driver_laps if not lap.get("is_pit_out_lap")]
            driver_laps.sort(key=lambda lap: lap.get("lap_number", 0))

            lap_numbers, values = [], []
            for lap in driver_laps:
                value = lap.get(field_name)
                if value is None:
                    continue
                lap_numbers.append(lap["lap_number"])
                values.append(value)

            if not lap_numbers:
                continue

            plotted_any = True
            self.ax.plot(
                lap_numbers, values,
                marker="o", markersize=3, linewidth=1.5,
                color=colour, label=driver.get("name_acronym", str(driver_number)),
            )

        if not plotted_any:
            self.ax.text(
                0.5, 0.5, "No data for this selection",
                ha="center", va="center", transform=self.ax.transAxes, color="gray",
            )
            self.ax.set_xticks([])
            self.ax.set_yticks([])
            self.canvas.draw_idle()
            return

        self.ax.set_xlabel("Lap")
        self.ax.set_ylabel(y_label)
        self.ax.set_title(
            f"{self.meeting_combo.currentText()} - {self.session_combo.currentText()} - {metric_label}"
        )
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="best", fontsize=8, ncol=2)
        if invert_axis:
            self.ax.invert_yaxis()

        self.figure.tight_layout()
        self.canvas.draw_idle()

    # ---- Car Telemetry plotting --------------------------------------------

    def _on_telemetry_plot_clicked(self) -> None:
        session_key = self.session_combo.currentData()
        lap_number = self.lap_combo.currentData()
        if session_key is None or lap_number is None:
            return

        checked_drivers = self._checked_drivers()
        if not checked_drivers:
            QMessageBox.information(self, "No drivers selected", "Tick at least one driver to plot.")
            return

        channels = [name for name, check in self.channel_checks.items() if check.isChecked()]
        if not channels:
            QMessageBox.information(self, "No channels selected", "Tick at least one channel to plot.")
            return

        all_laps = self._laps_by_session.get(session_key)
        if all_laps is None:
            self._run_async(
                api.get_laps,
                lambda laps: self._fetch_telemetry(session_key, laps, lap_number, checked_drivers, channels),
                "Loading lap data...",
                session_key=session_key,
            )
        else:
            self._fetch_telemetry(session_key, all_laps, lap_number, checked_drivers, channels)

    def _fetch_telemetry(
        self,
        session_key: int,
        all_laps: list[dict],
        lap_number: int,
        drivers: list[dict],
        channels: list[str],
    ) -> None:
        self._laps_by_session[session_key] = all_laps

        lap_by_driver = {
            lap["driver_number"]: lap
            for lap in all_laps
            if lap.get("lap_number") == lap_number and lap.get("date_start") and lap.get("lap_duration")
        }

        self._pending_telemetry = {
            "session_key": session_key,
            "lap_number": lap_number,
            "drivers": drivers,
            "channels": channels,
            "data": {},
        }

        to_fetch: list[tuple[int, dict]] = []
        for driver in drivers:
            driver_number = driver["driver_number"]
            lap = lap_by_driver.get(driver_number)
            if lap is None:
                continue  # this driver has no timed lap with this number

            cache_key = (session_key, driver_number, lap_number)
            cached = self._telemetry_cache.get(cache_key)
            if cached is not None:
                self._pending_telemetry["data"][driver_number] = cached
            else:
                to_fetch.append((driver_number, lap))

        if not to_fetch:
            self._draw_telemetry()
            return

        self.telemetry_plot_button.setEnabled(False)
        self.status_bar.showMessage("Loading telemetry...")
        self._telemetry_fetch_remaining = len(to_fetch)

        for driver_number, lap in to_fetch:
            date_from = lap["date_start"]
            start_dt = datetime.fromisoformat(date_from)
            date_to = (start_dt + timedelta(seconds=lap["lap_duration"])).isoformat()

            worker = Worker(
                api.get_car_data,
                session_key=session_key,
                driver_number=driver_number,
                date_from=date_from,
                date_to=date_to,
            )
            worker.signals.finished.connect(
                lambda data, dn=driver_number: self._on_telemetry_fetched(session_key, lap_number, dn, data)
            )
            worker.signals.error.connect(self._on_telemetry_fetch_error)
            self._start_worker(worker)

    def _on_telemetry_fetched(self, session_key: int, lap_number: int, driver_number: int, data: list[dict]) -> None:
        self._telemetry_cache[(session_key, driver_number, lap_number)] = data
        if self._pending_telemetry is not None:
            self._pending_telemetry["data"][driver_number] = data
        self._telemetry_fetch_remaining -= 1
        if self._telemetry_fetch_remaining <= 0:
            self.status_bar.clearMessage()
            self._draw_telemetry()

    def _on_telemetry_fetch_error(self, message: str) -> None:
        self._telemetry_fetch_remaining -= 1
        if self._telemetry_fetch_remaining <= 0:
            self.status_bar.clearMessage()
            self._draw_telemetry()
        QMessageBox.critical(self, "OpenF1 request failed", message.strip().splitlines()[-1])

    def _draw_telemetry(self) -> None:
        self.telemetry_plot_button.setEnabled(True)
        info = self._pending_telemetry
        if info is None:
            return

        channels: list[str] = info["channels"]
        drivers: list[dict] = info["drivers"]
        data_by_driver: dict[int, list[dict]] = info["data"]

        self.telemetry_figure.clear()
        axes = self.telemetry_figure.subplots(len(channels), 1, sharex=True)
        if len(channels) == 1:
            axes = [axes]

        plotted_any = False
        for ax, channel_name in zip(axes, channels):
            field_name, y_label = TELEMETRY_CHANNELS[channel_name]
            for driver in drivers:
                driver_number = driver["driver_number"]
                rows = data_by_driver.get(driver_number)
                if not rows:
                    continue

                rows_sorted = sorted(rows, key=lambda r: r["date"])
                t0 = datetime.fromisoformat(rows_sorted[0]["date"])
                elapsed = [(datetime.fromisoformat(r["date"]) - t0).total_seconds() for r in rows_sorted]
                values = [r.get(field_name) for r in rows_sorted]

                colour = f"#{driver.get('team_colour') or '888888'}"
                ax.plot(
                    elapsed, values,
                    linewidth=1.2, color=colour,
                    label=driver.get("name_acronym", str(driver_number)),
                )
                plotted_any = True

            ax.set_ylabel(y_label, fontsize=9)
            ax.grid(True, alpha=0.3)

        if not plotted_any:
            axes[0].text(
                0.5, 0.5, "No telemetry data for this selection",
                ha="center", va="center", transform=axes[0].transAxes, color="gray",
            )
            for ax in axes:
                ax.set_xticks([])
                ax.set_yticks([])
            self.telemetry_canvas.draw_idle()
            return

        axes[-1].set_xlabel("Time into lap (s)")
        axes[0].legend(loc="upper right", fontsize=7, ncol=min(len(drivers), 4))
        self.telemetry_figure.suptitle(
            f"{self.meeting_combo.currentText()} - {self.session_combo.currentText()} - Lap {info['lap_number']}"
        )
        self.telemetry_figure.tight_layout()
        self.telemetry_canvas.draw_idle()


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
