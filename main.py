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
from datetime import datetime
from typing import Any, Callable

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
        self.resize(1150, 700)

        self.thread_pool = QThreadPool.globalInstance()

        # Caches, keyed by id, so re-plotting after a checkbox change never
        # needs a fresh network call.
        self._drivers_by_session: dict[int, list[dict]] = {}
        self._laps_by_session: dict[int, list[dict]] = {}

        # Keeps in-flight Worker objects alive: QRunnable/QObject instances
        # with no Qt parent are only kept alive by Python's own refcounting,
        # so a Worker with no other reference gets garbage-collected while
        # its background thread is still running, and emitting its signal
        # afterwards raises "Signal source has been deleted".
        self._active_workers: list[Worker] = []

        self._build_ui()
        self._populate_years()

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter)

        splitter.addWidget(self._build_control_panel())
        splitter.addWidget(self._build_plot_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 850])

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _build_control_panel(self) -> QWidget:
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

        layout.addWidget(QLabel("<b>Metric</b>"))
        self.metric_combo = QComboBox()
        self.metric_combo.addItems(list(METRICS.keys()))
        layout.addWidget(self.metric_combo)

        self.exclude_pit_laps_check = QCheckBox("Exclude pit in/out laps")
        self.exclude_pit_laps_check.setChecked(True)
        layout.addWidget(self.exclude_pit_laps_check)

        self.invert_time_axis_check = QCheckBox("Faster laps plotted higher")
        layout.addWidget(self.invert_time_axis_check)

        self.plot_button = QPushButton("Plot")
        self.plot_button.setEnabled(False)
        self.plot_button.clicked.connect(self._on_plot_clicked)
        layout.addWidget(self.plot_button)

        layout.addStretch(1)
        return panel

    def _build_plot_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        self.figure = Figure(figsize=(7, 5))
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, panel)
        self.ax = self.figure.add_subplot(111)
        self._reset_plot_placeholder()

        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas, stretch=1)
        return panel

    def _reset_plot_placeholder(self) -> None:
        self.ax.clear()
        self.ax.text(
            0.5, 0.5, "Select a session and drivers, then click Plot",
            ha="center", va="center", transform=self.ax.transAxes, color="gray",
        )
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.canvas.draw_idle()

    # ---- Generic async helper --------------------------------------------

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

    # ---- Cascading selection: Year -> Meeting -> Session -> Drivers ------

    def _populate_years(self) -> None:
        current_year = datetime.now().year
        self.year_combo.blockSignals(True)
        self.year_combo.addItem("-- Select year --", None)
        for year in range(current_year, FIRST_HISTORICAL_YEAR - 1, -1):
            self.year_combo.addItem(str(year), year)
        self.year_combo.blockSignals(False)

    def _on_year_changed(self) -> None:
        year = self.year_combo.currentData()
        self._clear_combo(self.meeting_combo, "-- Select Grand Prix --")
        self._clear_combo(self.session_combo, "-- Select session --")
        self.driver_list.clear()
        self.plot_button.setEnabled(False)
        if year is None:
            return
        self._run_async(
            api.get_meetings, self._populate_meetings, f"Loading {year} calendar...", year=year
        )

    def _populate_meetings(self, meetings: list[dict]) -> None:
        meetings_sorted = sorted(meetings, key=lambda m: m.get("date_start") or "")
        self.meeting_combo.blockSignals(True)
        for meeting in meetings_sorted:
            self.meeting_combo.addItem(meeting["meeting_name"], meeting["meeting_key"])
        self.meeting_combo.blockSignals(False)
        if meetings_sorted:
            self.meeting_combo.setCurrentIndex(1)  # trigger _on_meeting_changed

    def _on_meeting_changed(self) -> None:
        meeting_key = self.meeting_combo.currentData()
        self._clear_combo(self.session_combo, "-- Select session --")
        self.driver_list.clear()
        self.plot_button.setEnabled(False)
        if meeting_key is None:
            return
        self._run_async(
            api.get_sessions, self._populate_sessions, "Loading sessions...", meeting_key=meeting_key
        )

    def _populate_sessions(self, sessions: list[dict]) -> None:
        sessions_sorted = sorted(sessions, key=lambda s: s.get("date_start") or "")
        self.session_combo.blockSignals(True)
        for session in sessions_sorted:
            self.session_combo.addItem(session["session_name"], session["session_key"])
        self.session_combo.blockSignals(False)
        if sessions_sorted:
            self.session_combo.setCurrentIndex(1)  # trigger _on_session_changed

    def _on_session_changed(self) -> None:
        session_key = self.session_combo.currentData()
        self.driver_list.clear()
        self.plot_button.setEnabled(False)
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

        # Warm the lap-data cache in the background so Plot feels instant.
        if session_key not in self._laps_by_session:
            worker = Worker(api.get_laps, session_key=session_key)
            worker.signals.finished.connect(
                lambda laps, sk=session_key: self._laps_by_session.__setitem__(sk, laps)
            )
            self._start_worker(worker)

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

    # ---- Plotting ----------------------------------------------------------

    def _on_plot_clicked(self) -> None:
        session_key = self.session_combo.currentData()
        if session_key is None:
            return

        checked_drivers = [
            self.driver_list.item(i).data(Qt.UserRole)
            for i in range(self.driver_list.count())
            if self.driver_list.item(i).checkState() == Qt.Checked
        ]
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


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
