"""
OpenF1 Lap Viewer
=================
A small Windows desktop app that pulls historical Formula 1 session data
from the OpenF1 API (https://openf1.org) and plots it lap-by-lap for
whichever drivers/cars you select.

Run with:  python main.py
"""

from __future__ import annotations

import colorsys
import statistics
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import matplotlib
matplotlib.use("QtAgg")  # works with both PyQt and PySide bindings

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from matplotlib import patheffects
from matplotlib.patches import Patch

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import circuit_info
import openf1_client as api
import team_order

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

# Most drivers that can be ticked at once, on every tab - beyond this the
# charts (especially Tyre Strategy's one-panel-per-driver stack) get too
# crowded to read.
MAX_SELECTED_DRIVERS = 6

# OpenF1 compound name -> (background shading colour, legend label): pale
# versions of the colours Pirelli paints on the tyre sidewalls, so the
# lap-time line stays readable on top. Hard is really white, but excluded
# laps are left white, so Hard is shaded light grey to tell them apart.
TYRE_COMPOUNDS: dict[str, tuple[str, str]] = {
    "SOFT": ("#f7a8b6", "Soft"),
    "MEDIUM": ("#ffe58f", "Medium"),
    "HARD": ("#d6d6d6", "Hard"),
    "INTERMEDIATE": ("#a9dcaf", "Intermediate"),
    "WET": ("#9fc3e3", "Wet"),
}
# Laps with no stint data are hatched rather than filled, so they don't
# look like an excluded (white) lap or any real compound.
UNKNOWN_COMPOUND = ("#ffffff", "Unknown")
UNKNOWN_COMPOUND_HATCH = "///"

# Default for the Tyre Strategy tab's outlier threshold: a lap this many
# percent or more slower or faster than the driver's median lap is treated
# as an outlier (safety car, red flag, pit in-lap, ...).
DEFAULT_TYRE_OUTLIER_PERCENT = 10


def _shift_lightness(hex_colour: str, index: int) -> str:
    """Nudges a hex colour's lightness apart for the `index`-th driver
    sharing it, alternating lighter/darker and growing the shift for each
    further one; `index == 0` returns the colour unchanged."""
    if index == 0:
        return hex_colour

    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)

    step = (index + 1) // 2
    sign = 1 if index % 2 else -1
    l = min(0.92, max(0.08, l + sign * step * 0.22))

    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


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
        self._meetings_by_key: dict[int, dict] = {}
        self._laps_by_session: dict[int, list[dict]] = {}
        self._telemetry_cache: dict[tuple[int, int, int], list[dict]] = {}
        self._stints_by_session: dict[int, list[dict]] = {}

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
        tabs.addTab(self._build_race_trace_tab(), "Race Trace")
        tabs.addTab(self._build_tyre_strategy_tab(), "Tyre Strategy")
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
        self.driver_list.itemChanged.connect(self._on_driver_checklist_changed)
        layout.addWidget(self.driver_list, stretch=1)

        select_row = QHBoxLayout()
        # No "All" button: at most MAX_SELECTED_DRIVERS can be ticked.
        self.driver_count_label = QLabel()
        select_row.addWidget(self.driver_count_label, stretch=1)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_driver_checks)
        select_row.addWidget(clear_btn)
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

        self.link_laps_check = QCheckBox("Same lap for all drivers")
        self.link_laps_check.setChecked(True)
        self.link_laps_check.toggled.connect(self._on_link_laps_toggled)
        controls_layout.addWidget(self.link_laps_check)

        self.lap_combo = QComboBox()
        self.lap_combo.addItem("-- Select session first --", None)
        controls_layout.addWidget(self.lap_combo)

        # Per-driver lap pickers, shown instead of the combo above when
        # "Same lap for all drivers" is unticked - one row per checked
        # driver, kept in sync with the driver list (see
        # _rebuild_per_driver_lap_rows).
        self.per_driver_lap_widget = QWidget()
        self.per_driver_lap_layout = QFormLayout(self.per_driver_lap_widget)
        self.per_driver_lap_layout.setContentsMargins(0, 0, 0, 0)
        self.per_driver_lap_widget.setVisible(False)
        controls_layout.addWidget(self.per_driver_lap_widget)
        self.per_driver_lap_combos: dict[int, QComboBox] = {}

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

    def _build_race_trace_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        blurb = QLabel(
            "Each driver's gap to the race leader, lap by lap. The leader "
            "is whoever's actually ahead on track, even if they're not "
            "ticked."
        )
        blurb.setWordWrap(True)
        controls_layout.addWidget(blurb)

        self.race_trace_plot_button = QPushButton("Plot Race Trace")
        self.race_trace_plot_button.setEnabled(False)
        self.race_trace_plot_button.clicked.connect(self._on_race_trace_plot_clicked)
        controls_layout.addWidget(self.race_trace_plot_button)

        controls_layout.addStretch(1)
        controls.setFixedWidth(180)
        layout.addWidget(controls)

        plot_panel = QWidget()
        plot_layout = QVBoxLayout(plot_panel)
        self.race_trace_figure = Figure(figsize=(7, 5))
        self.race_trace_canvas = FigureCanvas(self.race_trace_figure)
        self.race_trace_toolbar = NavigationToolbar(self.race_trace_canvas, plot_panel)
        self.race_trace_ax = self.race_trace_figure.add_subplot(111)
        self._reset_race_trace_placeholder()
        plot_layout.addWidget(self.race_trace_toolbar)
        plot_layout.addWidget(self.race_trace_canvas, stretch=1)
        layout.addWidget(plot_panel, stretch=1)

        return tab

    def _build_tyre_strategy_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        blurb = QLabel(
            "Each ticked driver's lap times on their own graph, with the "
            "background shaded by the tyre compound in use."
        )
        blurb.setWordWrap(True)
        controls_layout.addWidget(blurb)

        self.tyre_exclude_pit_laps_check = QCheckBox("Exclude pit out laps")
        self.tyre_exclude_pit_laps_check.setChecked(True)
        controls_layout.addWidget(self.tyre_exclude_pit_laps_check)

        self.tyre_exclude_outliers_check = QCheckBox("Exclude outlier laps")
        self.tyre_exclude_outliers_check.setChecked(True)
        controls_layout.addWidget(self.tyre_exclude_outliers_check)

        outlier_row = QHBoxLayout()
        outlier_row.addWidget(QLabel("Off median by ≥"))
        self.tyre_outlier_percent_spin = QSpinBox()
        self.tyre_outlier_percent_spin.setRange(1, 100)
        self.tyre_outlier_percent_spin.setSuffix("%")
        self.tyre_outlier_percent_spin.setValue(DEFAULT_TYRE_OUTLIER_PERCENT)
        outlier_row.addWidget(self.tyre_outlier_percent_spin)
        controls_layout.addLayout(outlier_row)
        self.tyre_exclude_outliers_check.toggled.connect(self.tyre_outlier_percent_spin.setEnabled)

        self.tyre_invert_time_axis_check = QCheckBox("Faster laps plotted higher")
        controls_layout.addWidget(self.tyre_invert_time_axis_check)

        self.tyre_plot_button = QPushButton("Plot Lap Times")
        self.tyre_plot_button.setEnabled(False)
        self.tyre_plot_button.clicked.connect(self._on_tyre_plot_clicked)
        controls_layout.addWidget(self.tyre_plot_button)

        controls_layout.addStretch(1)
        controls.setFixedWidth(180)
        layout.addWidget(controls)

        plot_panel = QWidget()
        plot_layout = QVBoxLayout(plot_panel)
        self.tyre_figure = Figure(figsize=(7, 5))
        self.tyre_canvas = FigureCanvas(self.tyre_figure)
        self.tyre_toolbar = NavigationToolbar(self.tyre_canvas, plot_panel)
        self._reset_tyre_placeholder()
        plot_layout.addWidget(self.tyre_toolbar)
        plot_layout.addWidget(self.tyre_canvas, stretch=1)
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
            0.5, 0.5, "Select a session, lap(s), drivers and channels,\nthen click Plot Telemetry",
            ha="center", va="center", transform=ax.transAxes, color="gray",
        )
        ax.set_xticks([])
        ax.set_yticks([])
        self.telemetry_canvas.draw_idle()

    def _reset_race_trace_placeholder(self) -> None:
        self.race_trace_ax.clear()
        self.race_trace_ax.text(
            0.5, 0.5, "Select a session and drivers, then click Plot Race Trace",
            ha="center", va="center", transform=self.race_trace_ax.transAxes, color="gray",
        )
        self.race_trace_ax.set_xticks([])
        self.race_trace_ax.set_yticks([])
        self.race_trace_canvas.draw_idle()

    def _reset_tyre_placeholder(self) -> None:
        self.tyre_figure.clear()
        ax = self.tyre_figure.add_subplot(111)
        ax.text(
            0.5, 0.5, "Select a session and up to four drivers, then click Plot Lap Times",
            ha="center", va="center", transform=ax.transAxes, color="gray",
        )
        ax.set_xticks([])
        ax.set_yticks([])
        self.tyre_canvas.draw_idle()

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
        self._clear_per_driver_lap_rows()
        self.driver_list.clear()
        self._update_driver_count_label()
        self.plot_button.setEnabled(False)
        self.telemetry_plot_button.setEnabled(False)
        self.race_trace_plot_button.setEnabled(False)
        self.tyre_plot_button.setEnabled(False)
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
            self._meetings_by_key[meeting["meeting_key"]] = meeting
        self.meeting_combo.blockSignals(False)
        if meetings_sorted and auto_select:
            self.meeting_combo.setCurrentIndex(1)  # trigger _on_meeting_changed

    def _on_meeting_changed(self) -> None:
        meeting_key = self.meeting_combo.currentData()
        self._clear_combo(self.session_combo, "-- Select session --")
        self._clear_combo(self.lap_combo, "-- Select session first --")
        self._clear_per_driver_lap_rows()
        self.driver_list.clear()
        self._update_driver_count_label()
        self.plot_button.setEnabled(False)
        self.telemetry_plot_button.setEnabled(False)
        self.race_trace_plot_button.setEnabled(False)
        self.tyre_plot_button.setEnabled(False)
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
        self._update_driver_count_label()
        self._clear_combo(self.lap_combo, "-- Select lap --")
        self._clear_per_driver_lap_rows()
        self.plot_button.setEnabled(False)
        self.telemetry_plot_button.setEnabled(False)
        self.race_trace_plot_button.setEnabled(False)
        self.tyre_plot_button.setEnabled(False)
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
        if not self.link_laps_check.isChecked():
            self._rebuild_per_driver_lap_rows()

    # ---- Per-driver lap selection (Car Telemetry tab) ---------------------

    def _on_link_laps_toggled(self, linked: bool) -> None:
        self.lap_combo.setVisible(linked)
        self.per_driver_lap_widget.setVisible(not linked)
        if not linked:
            self._rebuild_per_driver_lap_rows()

    def _on_driver_checklist_changed(self, item: QListWidgetItem) -> None:
        if item.checkState() == Qt.Checked and len(self._checked_drivers()) > MAX_SELECTED_DRIVERS:
            # Undo the tick that went over the limit. Signals are blocked so
            # this doesn't re-enter the handler.
            self.driver_list.blockSignals(True)
            item.setCheckState(Qt.Unchecked)
            self.driver_list.blockSignals(False)
            self.status_bar.showMessage(
                f"At most {MAX_SELECTED_DRIVERS} drivers can be selected - untick one first.", 5000
            )
            return
        self._update_driver_count_label()
        if not self.link_laps_check.isChecked():
            self._rebuild_per_driver_lap_rows()

    def _clear_per_driver_lap_rows(self) -> None:
        while self.per_driver_lap_layout.rowCount():
            self.per_driver_lap_layout.removeRow(0)
        self.per_driver_lap_combos.clear()

    def _rebuild_per_driver_lap_rows(self) -> None:
        """Rebuilds the per-driver lap combos to match the currently
        checked drivers, preserving each driver's existing selection where
        possible and defaulting new rows to the linked lap combo's
        current lap."""
        checked_drivers = self._checked_drivers()
        checked_numbers = {d["driver_number"] for d in checked_drivers}

        previous_selection = {
            driver_number: combo.currentData()
            for driver_number, combo in self.per_driver_lap_combos.items()
            if driver_number in checked_numbers
        }

        self._clear_per_driver_lap_rows()

        session_key = self.session_combo.currentData()
        all_laps = self._laps_by_session.get(session_key, []) if session_key is not None else []
        default_lap = self.lap_combo.currentData()

        for driver in checked_drivers:
            driver_number = driver["driver_number"]
            lap_numbers = sorted(
                {
                    lap["lap_number"]
                    for lap in all_laps
                    if lap.get("driver_number") == driver_number
                    and lap.get("date_start") and lap.get("lap_duration")
                }
            )

            combo = QComboBox()
            combo.addItem("-- Select lap --", None)
            for number in lap_numbers:
                combo.addItem(f"Lap {number}", number)

            restore_lap = previous_selection.get(driver_number, default_lap)
            index = combo.findData(restore_lap)
            combo.setCurrentIndex(index if index != -1 else (1 if lap_numbers else 0))

            self.per_driver_lap_combos[driver_number] = combo
            self.per_driver_lap_layout.addRow(driver.get("name_acronym", str(driver_number)), combo)

    def _lap_selection_by_driver(self, drivers: list[dict]) -> Optional[dict[int, int]]:
        """Resolves the lap to plot for each driver, from either the
        single linked combo or the per-driver rows. Returns None (after
        telling the user why) if the selection is incomplete."""
        if self.link_laps_check.isChecked():
            lap_number = self.lap_combo.currentData()
            if lap_number is None:
                return None
            return {driver["driver_number"]: lap_number for driver in drivers}

        selection: dict[int, int] = {}
        missing = []
        for driver in drivers:
            combo = self.per_driver_lap_combos.get(driver["driver_number"])
            lap_number = combo.currentData() if combo else None
            if lap_number is None:
                missing.append(driver.get("name_acronym", str(driver["driver_number"])))
            else:
                selection[driver["driver_number"]] = lap_number

        if missing:
            QMessageBox.information(
                self, "Lap not selected", "Select a lap for: " + ", ".join(missing),
            )
            return None
        return selection

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

        # Grouped by team, in the order the teams finished last season's
        # Constructors' Championship, then alphabetically within each team.
        meeting = self._meetings_by_key.get(self.meeting_combo.currentData()) or {}
        season = meeting.get("year") or self.year_combo.currentData()
        drivers_sorted = sorted(
            drivers,
            key=lambda d: (
                team_order.team_sort_key(d.get("team_name"), season),
                (d.get("name_acronym") or "").casefold(),
            ),
        )
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
        self.race_trace_plot_button.setEnabled(self.driver_list.count() > 0)
        self.tyre_plot_button.setEnabled(self.driver_list.count() > 0)
        self._update_driver_count_label()

    @staticmethod
    def _colour_swatch(colour: str) -> QIcon:
        pixmap = QPixmap(14, 14)
        pixmap.fill(QColor(colour))
        return QIcon(pixmap)

    @staticmethod
    def _driver_plot_colours(drivers: list[dict]) -> dict[int, str]:
        """
        Maps driver_number -> hex colour to plot that driver in.

        Team-mates share an identical `team_colour` from OpenF1, so two of
        them plotted together would otherwise be indistinguishable - each
        driver beyond the first with a given team colour gets that colour's
        lightness nudged apart (alternating lighter/darker, growing with
        each further team-mate) so every line on the chart stays legible
        while still reading as "that team's colour".
        """
        colours: dict[int, str] = {}
        seen_per_colour: dict[str, int] = {}
        for driver in drivers:
            base = f"#{driver.get('team_colour') or '888888'}"
            index = seen_per_colour.get(base, 0)
            seen_per_colour[base] = index + 1
            colours[driver["driver_number"]] = _shift_lightness(base, index)
        return colours

    def _clear_driver_checks(self) -> None:
        for i in range(self.driver_list.count()):
            self.driver_list.item(i).setCheckState(Qt.Unchecked)

    def _update_driver_count_label(self) -> None:
        self.driver_count_label.setText(f"{len(self._checked_drivers())} / {MAX_SELECTED_DRIVERS} selected")

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
        driver_colours = self._driver_plot_colours(drivers)

        for driver in drivers:
            driver_number = driver["driver_number"]
            colour = driver_colours[driver_number]

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
        if session_key is None:
            return

        checked_drivers = self._checked_drivers()
        if not checked_drivers:
            QMessageBox.information(self, "No drivers selected", "Tick at least one driver to plot.")
            return

        lap_number_by_driver = self._lap_selection_by_driver(checked_drivers)
        if lap_number_by_driver is None:
            return

        channels = [name for name, check in self.channel_checks.items() if check.isChecked()]
        if not channels:
            QMessageBox.information(self, "No channels selected", "Tick at least one channel to plot.")
            return

        all_laps = self._laps_by_session.get(session_key)
        if all_laps is None:
            self._run_async(
                api.get_laps,
                lambda laps: self._fetch_telemetry(session_key, laps, lap_number_by_driver, checked_drivers, channels),
                "Loading lap data...",
                session_key=session_key,
            )
        else:
            self._fetch_telemetry(session_key, all_laps, lap_number_by_driver, checked_drivers, channels)

    def _fetch_telemetry(
        self,
        session_key: int,
        all_laps: list[dict],
        lap_number_by_driver: dict[int, int],
        drivers: list[dict],
        channels: list[str],
    ) -> None:
        self._laps_by_session[session_key] = all_laps

        laps_by_driver_and_number = {
            (lap["driver_number"], lap["lap_number"]): lap
            for lap in all_laps
            if lap.get("date_start") and lap.get("lap_duration")
        }

        self._pending_telemetry = {
            "session_key": session_key,
            "lap_number_by_driver": lap_number_by_driver,
            "drivers": drivers,
            "channels": channels,
            "data": {},
            "corners": self._get_corners_for_current_meeting(),
        }

        to_fetch: list[tuple[int, dict, int]] = []
        for driver in drivers:
            driver_number = driver["driver_number"]
            lap_number = lap_number_by_driver.get(driver_number)
            if lap_number is None:
                continue

            lap = laps_by_driver_and_number.get((driver_number, lap_number))
            if lap is None:
                continue  # this driver has no timed lap with this number

            cache_key = (session_key, driver_number, lap_number)
            cached = self._telemetry_cache.get(cache_key)
            if cached is not None:
                self._pending_telemetry["data"][driver_number] = cached
            else:
                to_fetch.append((driver_number, lap, lap_number))

        if not to_fetch:
            self._draw_telemetry()
            return

        self.telemetry_plot_button.setEnabled(False)
        self.status_bar.showMessage("Loading telemetry...")
        self._telemetry_fetch_remaining = len(to_fetch)

        for driver_number, lap, lap_number in to_fetch:
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
                lambda data, sk=session_key, dn=driver_number, ln=lap_number: self._on_telemetry_fetched(sk, ln, dn, data)
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

    def _get_corners_for_current_meeting(self) -> list[dict]:
        """
        Corner markers (as a fraction of the lap, 0-1) for the currently
        selected Grand Prix's circuit - a local, bundled lookup (see
        circuit_info.py), so this is instant and needs no network call.
        Returns [] if this circuit isn't in the bundled data (e.g. a
        brand-new track), in which case the plot just falls back to a
        plain distance axis.
        """
        meeting = self._meetings_by_key.get(self.meeting_combo.currentData())
        circuit_key = meeting.get("circuit_key") if meeting else None
        if circuit_key is None:
            return []
        try:
            return circuit_info.get_corners(circuit_key)
        except circuit_info.CircuitInfoError:
            return []

    @staticmethod
    def _cumulative_distance(rows_sorted: list[dict]) -> list[float]:
        """
        OpenF1's car_data has no distance channel, so this integrates speed
        over time (trapezoidal rule) to get metres travelled since the
        first sample - close enough to align drivers by track position
        (e.g. corner-for-corner) rather than by elapsed time.
        """
        distance = [0.0]
        for previous, current in zip(rows_sorted, rows_sorted[1:]):
            dt = (
                datetime.fromisoformat(current["date"]) - datetime.fromisoformat(previous["date"])
            ).total_seconds()
            avg_speed_ms = ((previous.get("speed") or 0) + (current.get("speed") or 0)) / 2 / 3.6
            distance.append(distance[-1] + avg_speed_ms * dt)
        return distance

    def _draw_telemetry(self) -> None:
        self.telemetry_plot_button.setEnabled(True)
        info = self._pending_telemetry
        if info is None:
            return

        channels: list[str] = info["channels"]
        drivers: list[dict] = info["drivers"]
        data_by_driver: dict[int, list[dict]] = info["data"]
        corners: list[dict] = info.get("corners") or []

        self.telemetry_figure.clear()
        axes = self.telemetry_figure.subplots(len(channels), 1, sharex=True)
        if len(channels) == 1:
            axes = [axes]

        # Sort once and reuse across channels rather than per (channel,
        # driver) pair - the distance integration is the same either way.
        distance_by_driver: dict[int, list[float]] = {}
        for driver_number, rows in list(data_by_driver.items()):
            if not rows:
                continue
            rows_sorted = sorted(rows, key=lambda r: r["date"])
            data_by_driver[driver_number] = rows_sorted
            distance_by_driver[driver_number] = self._cumulative_distance(rows_sorted)

        plotted_any = False
        driver_colours = self._driver_plot_colours(drivers)
        for ax, channel_name in zip(axes, channels):
            field_name, y_label = TELEMETRY_CHANNELS[channel_name]
            for driver in drivers:
                driver_number = driver["driver_number"]
                rows_sorted = data_by_driver.get(driver_number)
                distance = distance_by_driver.get(driver_number)
                if not rows_sorted or not distance:
                    continue

                values = [r.get(field_name) for r in rows_sorted]

                colour = driver_colours[driver_number]
                ax.plot(
                    distance, values,
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

        lap_distance = max(
            (distances[-1] for distances in distance_by_driver.values() if distances),
            default=None,
        )

        if lap_distance:
            axes[-1].set_xlim(0, lap_distance)

        # Corner positions are stored as a fraction of the lap (0-1) rather
        # than an absolute distance (see circuit_info.py), so they scale to
        # this specific lap's own integrated distance automatically.
        if corners and lap_distance:
            corner_distances = [c["fraction"] * lap_distance for c in corners]
            for ax in axes:
                ax.vlines(
                    corner_distances, *ax.get_ylim(),
                    linestyles="dotted", colors="grey", linewidth=0.8, alpha=0.6,
                )
            axes[-1].set_xticks(corner_distances)
            axes[-1].set_xticklabels(
                [f"{c['number']}{c['letter']}" for c in corners], fontsize=7
            )
            axes[-1].set_xlabel("Corner")
        else:
            axes[-1].set_xlabel("Distance into lap (m)")

        axes[0].legend(loc="upper right", fontsize=7, ncol=min(len(drivers), 4))
        lap_numbers_used = set(info["lap_number_by_driver"].values())
        lap_label = f"Lap {lap_numbers_used.pop()}" if len(lap_numbers_used) == 1 else "Various laps"
        self.telemetry_figure.suptitle(
            f"{self.meeting_combo.currentText()} - {self.session_combo.currentText()} - {lap_label}"
        )
        self.telemetry_figure.tight_layout()
        self.telemetry_canvas.draw_idle()

    # ---- Race Trace plotting ------------------------------------------------

    def _on_race_trace_plot_clicked(self) -> None:
        session_key = self.session_combo.currentData()
        if session_key is None:
            return

        checked_drivers = self._checked_drivers()
        if not checked_drivers:
            QMessageBox.information(self, "No drivers selected", "Tick at least one driver to plot.")
            return

        if session_key in self._laps_by_session:
            self._draw_race_trace(self._laps_by_session[session_key], checked_drivers)
        else:
            self._run_async(
                api.get_laps,
                lambda laps: self._on_race_trace_laps_ready(session_key, laps, checked_drivers),
                "Loading lap data...",
                session_key=session_key,
            )

    def _on_race_trace_laps_ready(self, session_key: int, laps: list[dict], checked_drivers: list[dict]) -> None:
        self._laps_by_session[session_key] = laps
        self._draw_race_trace(laps, checked_drivers)

    @staticmethod
    def _gaps_to_leader(all_laps: list[dict]) -> dict[int, dict[int, float]]:
        """
        driver_number -> {lap_number: seconds behind the leader}, computed
        from the *whole field's* laps (not just the drivers being plotted),
        so the actual race leader is used even if they're not ticked.

        Each driver's cumulative race time through a lap is the running
        total of their own lap_duration values (pit stops included - a
        stop should show up as losing time, not get smoothed away); the
        leader at a given lap number is whichever driver has the smallest
        cumulative time there. A driver's series stops at the first lap
        missing timing data (retirement, no time set, etc.) rather than
        guessing.
        """
        laps_by_driver: dict[int, list[dict]] = {}
        for lap in all_laps:
            driver_number = lap.get("driver_number")
            if driver_number is not None:
                laps_by_driver.setdefault(driver_number, []).append(lap)

        cumulative_by_driver: dict[int, dict[int, float]] = {}
        for driver_number, laps in laps_by_driver.items():
            laps_sorted = sorted(laps, key=lambda lap: lap.get("lap_number", 0))
            cumulative: dict[int, float] = {}
            total = 0.0
            for lap in laps_sorted:
                duration = lap.get("lap_duration")
                lap_number = lap.get("lap_number")
                if duration is None or lap_number is None:
                    break
                total += duration
                cumulative[lap_number] = total
            cumulative_by_driver[driver_number] = cumulative

        leader_time_by_lap: dict[int, float] = {}
        for cumulative in cumulative_by_driver.values():
            for lap_number, total in cumulative.items():
                if lap_number not in leader_time_by_lap or total < leader_time_by_lap[lap_number]:
                    leader_time_by_lap[lap_number] = total

        return {
            driver_number: {
                lap_number: total - leader_time_by_lap[lap_number]
                for lap_number, total in cumulative.items()
            }
            for driver_number, cumulative in cumulative_by_driver.items()
        }

    def _draw_race_trace(self, all_laps: list[dict], drivers: list[dict]) -> None:
        gaps_by_driver = self._gaps_to_leader(all_laps)

        self.race_trace_ax.clear()
        plotted_any = False
        driver_colours = self._driver_plot_colours(drivers)

        for driver in drivers:
            driver_number = driver["driver_number"]
            gaps = gaps_by_driver.get(driver_number)
            if not gaps:
                continue

            lap_numbers = sorted(gaps.keys())
            # Negated so the leader sits flat at zero and everyone else
            # sinks below it in proportion to how far behind they are.
            values = [-gaps[lap_number] for lap_number in lap_numbers]

            plotted_any = True
            self.race_trace_ax.plot(
                lap_numbers, values,
                marker="o", markersize=3, linewidth=1.5,
                color=driver_colours[driver_number], label=driver.get("name_acronym", str(driver_number)),
            )

        if not plotted_any:
            self.race_trace_ax.text(
                0.5, 0.5, "No data for this selection",
                ha="center", va="center", transform=self.race_trace_ax.transAxes, color="gray",
            )
            self.race_trace_ax.set_xticks([])
            self.race_trace_ax.set_yticks([])
            self.race_trace_canvas.draw_idle()
            return

        self.race_trace_ax.set_xlabel("Lap")
        self.race_trace_ax.set_ylabel("Gap to leader (s)")
        self.race_trace_ax.set_title(
            f"{self.meeting_combo.currentText()} - {self.session_combo.currentText()} - Race Trace"
        )
        self.race_trace_ax.grid(True, alpha=0.3)
        self.race_trace_ax.legend(loc="best", fontsize=8, ncol=2)

        self.race_trace_figure.tight_layout()
        self.race_trace_canvas.draw_idle()

    # ---- Tyre Strategy plotting ---------------------------------------------

    def _on_tyre_plot_clicked(self) -> None:
        session_key = self.session_combo.currentData()
        if session_key is None:
            return

        drivers = self._checked_drivers()
        if not drivers:
            QMessageBox.information(self, "No drivers selected", "Tick at least one driver to plot.")
            return

        laps = self._laps_by_session.get(session_key)
        stints = self._stints_by_session.get(session_key)
        if laps is not None and stints is not None:
            self._draw_tyre_strategy(laps, stints, drivers)
            return

        def fetch() -> tuple[list[dict], list[dict]]:
            return (
                laps if laps is not None else api.get_laps(session_key),
                stints if stints is not None else api.get_stints(session_key),
            )

        self._run_async(
            fetch,
            lambda result: self._on_tyre_data_ready(session_key, result, drivers),
            "Loading lap and tyre data...",
        )

    def _on_tyre_data_ready(
        self, session_key: int, result: tuple[list[dict], list[dict]], drivers: list[dict]
    ) -> None:
        laps, stints = result
        self._laps_by_session[session_key] = laps
        self._stints_by_session[session_key] = stints
        if self.session_combo.currentData() == session_key:
            self._draw_tyre_strategy(laps, stints, drivers)

    @staticmethod
    def _compound_for_lap(driver_stints: list[dict], lap_number: int) -> str:
        """The compound (e.g. "SOFT") a driver was on for a lap, from their
        stints' lap ranges, or "UNKNOWN". A stint still running when the
        data was captured may have no lap_end, so it's treated as
        open-ended."""
        for stint in driver_stints:
            lap_start = stint.get("lap_start")
            lap_end = stint.get("lap_end")
            if lap_start is None or lap_number < lap_start:
                continue
            if lap_end is None or lap_number <= lap_end:
                compound = (stint.get("compound") or "").upper()
                return compound if compound in TYRE_COMPOUNDS else "UNKNOWN"
        return "UNKNOWN"

    @staticmethod
    def _without_outliers(laps: list[dict], threshold: float) -> list[dict]:
        """Drops laps whose time is `threshold` (a fraction, e.g. 0.1) or
        more away from the median of `laps` (one driver's laps), in either
        direction."""
        if not laps:
            return laps
        median = statistics.median(lap["lap_duration"] for lap in laps)
        return [
            lap for lap in laps
            if abs(lap["lap_duration"] - median) < threshold * median
        ]

    @staticmethod
    def _compound_patch_style(compound: str) -> dict[str, Any]:
        """facecolor (and hatch, for unknown laps) to shade a compound with."""
        style: dict[str, Any] = {"facecolor": TYRE_COMPOUNDS.get(compound, UNKNOWN_COMPOUND)[0]}
        if compound not in TYRE_COMPOUNDS:
            style["hatch"] = UNKNOWN_COMPOUND_HATCH
            style["edgecolor"] = "#b0b0b0"  # hatch lines take the edge colour
        return style

    def _draw_tyre_strategy(self, all_laps: list[dict], stints: list[dict], drivers: list[dict]) -> None:
        exclude_pit_laps = self.tyre_exclude_pit_laps_check.isChecked()
        exclude_outliers = self.tyre_exclude_outliers_check.isChecked()
        outlier_threshold = self.tyre_outlier_percent_spin.value() / 100

        # Work out each driver's laps first, so drivers with nothing to
        # show don't get an empty panel.
        laps_by_driver: list[tuple[dict, list[dict]]] = []
        for driver in drivers:
            driver_number = driver["driver_number"]
            driver_laps = [
                lap for lap in all_laps
                if lap.get("driver_number") == driver_number
                and lap.get("lap_duration") is not None
                and lap.get("lap_number") is not None
            ]
            if exclude_pit_laps:
                driver_laps = [lap for lap in driver_laps if not lap.get("is_pit_out_lap")]
            if exclude_outliers:
                driver_laps = self._without_outliers(driver_laps, outlier_threshold)
            if driver_laps:
                driver_laps.sort(key=lambda lap: lap["lap_number"])
                laps_by_driver.append((driver, driver_laps))

        self.tyre_figure.clear()
        if not laps_by_driver:
            ax = self.tyre_figure.add_subplot(111)
            ax.text(
                0.5, 0.5, "No data for this selection",
                ha="center", va="center", transform=ax.transAxes, color="gray",
            )
            ax.set_xticks([])
            ax.set_yticks([])
            self.tyre_canvas.draw_idle()
            return

        # One panel per driver, sharing both axes so pace and lap numbers
        # line up when reading down the stack.
        axes = self.tyre_figure.subplots(len(laps_by_driver), 1, sharex=True, sharey=True, squeeze=False)[:, 0]
        driver_colours = self._driver_plot_colours(drivers)
        compounds_used: set[str] = set()

        for ax, (driver, driver_laps) in zip(axes, laps_by_driver):
            driver_number = driver["driver_number"]
            colour = driver_colours[driver_number]
            driver_stints = [s for s in stints if s.get("driver_number") == driver_number]

            lap_numbers = [lap["lap_number"] for lap in driver_laps]
            values = [lap["lap_duration"] for lap in driver_laps]
            # Break the line wherever lap numbers skip (an excluded pit
            # out-lap or outlier, or a lap with no time), so the laps either
            # side of the gap aren't joined up as if they were consecutive.
            line_x: list[float] = []
            line_y: list[float] = []
            for i, (lap_number, value) in enumerate(zip(lap_numbers, values)):
                if i and lap_number != lap_numbers[i - 1] + 1:
                    line_x.append(float("nan"))
                    line_y.append(float("nan"))
                line_x.append(lap_number)
                line_y.append(value)
            # A white halo keeps the driver-coloured line readable on any
            # compound's shading (e.g. a red Ferrari line on Soft).
            ax.plot(
                line_x, line_y, linewidth=1.8, color=colour, zorder=3,
                path_effects=[patheffects.withStroke(linewidth=3.5, foreground="white")],
            )

            # Shade each plotted lap's slot (lap - 0.5 to lap + 0.5) in its
            # compound's colour, merging consecutive laps on the same
            # compound into one band. Excluded or missing laps get no band,
            # so they're left white.
            bands: list[list[Any]] = []  # [compound, first_lap, last_lap]
            for lap_number in lap_numbers:
                compound = self._compound_for_lap(driver_stints, lap_number)
                compounds_used.add(compound)
                if bands and bands[-1][0] == compound and bands[-1][2] == lap_number - 1:
                    bands[-1][2] = lap_number
                else:
                    bands.append([compound, lap_number, lap_number])

            for compound, first_lap, last_lap in bands:
                ax.axvspan(first_lap - 0.5, last_lap + 0.5, linewidth=0, zorder=0, **self._compound_patch_style(compound))

            ax.set_ylabel(f"{driver.get('name_acronym', driver_number)}\nLap time (s)", fontsize=9)
            ax.grid(True, alpha=0.3, color="#555555", zorder=1)
            ax.set_axisbelow(False)

        # Shared y-axis, so inverting one inverts them all.
        if self.tyre_invert_time_axis_check.isChecked():
            axes[0].invert_yaxis()

        axes[-1].set_xlabel("Lap")

        compound_order = list(TYRE_COMPOUNDS) + ["UNKNOWN"]
        compound_handles = [
            Patch(
                linewidth=0.5, label=TYRE_COMPOUNDS.get(c, UNKNOWN_COMPOUND)[1],
                **{"edgecolor": "#999999", **self._compound_patch_style(c)},
            )
            for c in compound_order if c in compounds_used
        ]
        compound_handles.append(
            Patch(facecolor="white", edgecolor="#999999", linewidth=0.5, label="Excluded")
        )
        # A figure-level legend in a strip reserved on the right, so it
        # never covers laps and isn't tied to the (possibly short) top panel.
        legend = self.tyre_figure.legend(
            handles=compound_handles, title="Tyre", fontsize=8, loc="upper right",
        )

        self.tyre_figure.suptitle(
            f"{self.meeting_combo.currentText()} - {self.session_combo.currentText()} - Lap Times by Tyre"
        )
        self.tyre_canvas.draw()  # needed to measure the legend's width
        legend_width = legend.get_window_extent().width / self.tyre_figure.bbox.width
        self.tyre_figure.tight_layout(rect=(0, 0, 1 - legend_width - 0.01, 1))
        self.tyre_canvas.draw_idle()

def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
