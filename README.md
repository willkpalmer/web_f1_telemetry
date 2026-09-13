# OpenF1 Lap Viewer

A small Windows desktop app that downloads historical Formula 1 session
data from the free [OpenF1 API](https://openf1.org/docs/) and plots it
lap-by-lap for whichever drivers you pick.

## What it does

1. Pick a **Year**, then a **Grand Prix**, then a **Session** (Practice,
   Qualifying, Sprint, Race, ...).
2. Tick the drivers/cars you want to compare.
3. Choose a tab:
   - **Lap Summary** — pick a **Metric** (Lap Time, Sector 1/2/3 time, or
     Speed Trap) and click **Plot** to see it graphed lap-by-lap, one
     coloured line per car (using each team's real colour).
   - **Car Telemetry** — pick a single **Lap**, tick the channels you want
     (Speed, Throttle, Brake, Gear, RPM, DRS), and click **Plot Telemetry**
     to compare cars' traces through that lap, aligned on elapsed time
     since the lap started. Note: OpenF1's free tier has no steering-angle
     channel, so that's the one thing you can't compare here.

Data only goes back to the 2023 season (that's what OpenF1 provides for
free without authentication) — the year list starts there.

## Setup (Windows)

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/)
   if you don't already have it (tick "Add python.exe to PATH" during
   install).
2. Open a terminal (PowerShell or Command Prompt) in this folder and
   create a virtual environment:

   ```
   python -m venv venv
   venv\Scripts\activate
   ```

3. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

4. Run the app:

   ```
   python main.py
   ```

Each time you come back to work on it, just re-run `venv\Scripts\activate`
before `python main.py`.

## Project layout

- `main.py` — the PySide6 GUI: dropdowns, driver checklist, and the two
  tabs' matplotlib charts.
- `openf1_client.py` — a thin wrapper around the OpenF1 REST endpoints
  (`meetings`, `sessions`, `drivers`, `laps`, `car_data`). Kept separate
  so you can reuse it or extend it (e.g. add `stints`) without touching
  the UI code.
- `requirements.txt` — the three packages the app needs.

## Notes and things worth knowing

- **Pit laps**: the "Exclude pit in/out laps" checkbox is on by default,
  because an out-lap or in-lap's time is inflated by the pit lane and
  would otherwise dwarf the racing laps on the graph.
- **"Faster laps plotted higher"**: inverts the y-axis for time-based
  metrics, so a driver improving pace draws an upward line instead of a
  downward one. Doesn't apply to Speed Trap (higher is already better
  there).
- **Caching**: once a session's lap data has been downloaded, switching
  drivers or metrics and clicking Plot again re-draws instantly — no new
  network request. Telemetry is cached per (session, driver, lap) too, so
  ticking a different channel for a lap you've already plotted doesn't
  re-fetch anything.
- **Telemetry lap selector**: only laps with a recorded start time and
  duration show up, since those two fields are what's used to carve a
  time window out of `/car_data` for that lap.
- **Rate limits**: OpenF1's free tier will occasionally respond with "too
  many requests" if you plot telemetry for a lot of drivers in quick
  succession — just wait a few seconds and click Plot again.
- **Extending it**: the `/laps` endpoint also has `i1_speed`, `i2_speed`,
  and the sector "mini-sector" colour segments if you want to add more
  Lap Summary metrics.
