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
     to compare cars' traces through that lap, aligned by distance
     travelled since the lap started (so you can compare drivers
     corner-for-corner) rather than by elapsed time. OpenF1 has no
     distance channel, so it's derived by integrating each car's speed
     over time. When corner data is available for the circuit (see
     below), the x-axis shows corner numbers instead of raw metres. Note:
     OpenF1's free tier has no steering-angle channel, so that's the one
     thing you can't compare here.
   - **Race Trace** — click **Plot Race Trace** to see each ticked
     driver's gap to the race leader, lap by lap (leader flat at zero,
     everyone else sinking below it in proportion to how far behind they
     are). The leader is whoever's actually ahead on track that lap, even
     if they're not one of the ticked drivers. Each driver's gap is their
     own cumulative race time (the running total of their lap times,
     including any pit stops - a stop is *supposed* to show up as a dip)
     minus the leader's. Most useful for Race/Sprint sessions, where lap
     numbers line up across the field; Practice/Qualifying laps aren't
     sequential in the same way, so it's not very meaningful there.
   - **Tyre Strategy** — pick up to four drivers from the tab's own
     driver pickers and click **Plot Lap Times** to see each driver's lap
     times on their own graph (stacked, sharing the same axes so they're
     easy to compare), with the graph's background shaded behind each
     lap in the colour of the tyre compound in use (red Soft, yellow
     Medium, grey Hard - really white, but grey to tell it apart from
     excluded laps - green Intermediate, blue Wet), so you can see stint
     lengths, pit stops and how pace fades as tyres wear. Compounds come
     from OpenF1's `/stints` endpoint; laps it has no stint for are
     hatched as "Unknown". "Exclude outlier laps" (on by default) drops
     any lap that's off that driver's median lap by at least the chosen
     percentage (default 10%, either slower or faster) - e.g. safety car,
     red flag or pit in-laps - so they don't squash the scale. Wherever a
     lap has been left out (including excluded pit out-laps), its
     background is left white and the line breaks rather than joining
     the laps either side.

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

- `main.py` — the PySide6 GUI: dropdowns, driver checklist, and each
  tab's matplotlib charts.
- `openf1_client.py` — a thin wrapper around the OpenF1 REST endpoints
  (`meetings`, `sessions`, `drivers`, `laps`, `stints`, `car_data`). Kept
  separate so you can reuse it or extend it without touching the UI code.
- `circuit_info.py` — looks up each circuit's corner numbers and their
  position along the lap, from `circuit_corners.json` (see below).
- `circuit_corners.json` — a bundled, pre-generated table of corner
  positions per circuit (as a fraction of the lap, 0-1), covering every
  circuit on the current F1 calendar. Regenerate it with
  `scripts/generate_circuit_corners.py` if a new circuit shows up or a
  layout changes and corner numbers stop appearing for it.
- `requirements.txt` — the packages the app needs.

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
- **Corner numbers**: sourced from a small bundled file
  (`circuit_corners.json`) rather than a live lookup, so this is instant
  and needs no network call or extra dependency - see
  `scripts/generate_circuit_corners.py`'s docstring for why (in short: the
  live source, MultiViewer, turned out to be unreliably reachable from
  some networks, and needed a slow full-session download to calibrate
  distances). If a circuit isn't in the bundled data (e.g. one that's
  brand new), the plot just falls back to a plain "Distance into lap (m)"
  axis.
