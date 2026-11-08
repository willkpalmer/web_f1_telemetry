# OpenF1 Lap Viewer

A static web app that loads historical Formula 1 session data from the
free [OpenF1 API](https://openf1.org/docs/) and plots it lap by lap for
whichever drivers you pick. Everything runs in the browser, with no server
and no build step, so it can be hosted on GitHub Pages.

Once Pages is enabled (see below), the app is at
**https://willkpalmer.github.io/web_f1_telemetry/**.

## What it does

1. Pick a **Year**, then a **Grand Prix**, then a **Session** (Practice,
   Qualifying, Sprint, Race, ...). On first load the app picks the most
   recent session that OpenF1 has data for.
2. Tick the drivers/cars you want to compare - up to 6 at once, so the
   charts stay readable. The list is grouped by team, with teams in the
   order they finished the previous season's Constructors' Championship,
   and drivers sorted alphabetically within each team; **Clear** unticks
   everyone.
3. Choose a tab:
   - **Lap Summary** — pick a **Metric** (Lap Time, Sector 1/2/3 time, or
     Speed Trap) and click **Plot** to see it graphed lap by lap, one
     coloured line per car (using each team's real colour, with
     team-mates shaded lighter/darker so they can be told apart).
   - **Car Telemetry** — pick a lap (the same one for everyone, or untick
     "Same lap for all drivers" to pick one per driver), tick the channels
     you want (Speed, Throttle, Brake, Gear, RPM, DRS), and click **Plot
     Telemetry** to compare cars' traces through that lap, aligned by
     distance travelled since the lap started (so you can compare drivers
     corner for corner) rather than by elapsed time. OpenF1 has no
     distance channel, so it's derived by integrating each car's speed
     over time. When corner data is available for the circuit, the x-axis
     shows corner numbers instead of raw metres. With per-driver laps,
     each driver's lap picker lists every lap's time (marking their
     fastest lap and any pit out-lap) to help you choose.
     The **Time delta** panel (on by default) is a race trace within the
     lap: the **Delta reference** driver (the first one you ticked, unless
     you pick another) sits flat at zero, and every other car is drawn
     above it where it's ahead at that point of the lap and below where
     it's behind, ending at the difference in their lap times. Each car's
     position is taken as a fraction of its own integrated lap distance,
     so small differences in the distance estimate between cars don't
     build up into a fake gap. OpenF1's free tier has no
     steering-angle channel, so that's the one thing you can't compare.
   - **Race Trace** — click **Plot Race Trace** to see each ticked
     driver's gap to the race leader, lap by lap (leader flat at zero,
     everyone else sinking below it in proportion to how far behind they
     are). The leader is whoever's actually ahead on track that lap, even
     if they're not ticked. Each driver's gap is their own cumulative race
     time (the running total of their lap times, including pit stops - a
     stop is *supposed* to show up as a dip) minus the leader's. Most
     useful for Race/Sprint sessions, where lap numbers line up across the
     field.
   - **Tyre Strategy** — click **Plot Lap Times** to see each ticked
     driver's lap times on their own graph (stacked, sharing the same axes
     so they're easy to compare), with the background shaded behind each
     lap in the colour of the tyre compound in use (red Soft, yellow
     Medium, grey Hard - really white, but grey to tell it apart from
     excluded laps - green Intermediate, blue Wet), so you can see stint
     lengths, pit stops and how pace fades as tyres wear. Compounds come
     from OpenF1's `/stints` endpoint; laps it has no stint for are
     hatched as "Unknown". "Exclude outlier laps" (on by default) drops
     any lap that's off that driver's median lap by at least the chosen
     percentage (default 10%, either slower or faster) - e.g. safety car,
     red flag or pit in-laps - so they don't squash the scale. Wherever a
     lap has been left out, its background is left blank and the line
     breaks rather than joining the laps either side.

Every chart can be zoomed (drag a box), panned, reset (double-click) and
saved as a PNG from the toolbar in its top-right corner.

Data only goes back to the 2023 season (that's what OpenF1 provides for
free without authentication), so the year list starts there.

## Publishing on GitHub Pages

The repository root *is* the site, so no build or workflow is needed:

1. On GitHub, open the repo's **Settings → Pages**.
2. Under **Build and deployment**, set **Source** to **Deploy from a
   branch**, then pick the **master** branch and the **/ (root)** folder,
   and click **Save**.
3. After a minute or so the site is live at the URL above. Every push to
   `master` re-publishes it automatically.

(The empty `.nojekyll` file tells Pages to serve the files as they are
rather than running them through Jekyll.)

## Running it locally

Browsers won't load JavaScript modules from a `file://` page, so serve
the folder with any static web server instead of double-clicking
`index.html`. For example, from this folder:

```bash
python3 -m http.server 8000
```

then open http://localhost:8000/. (`npx serve` works just as well.)

## Project layout

- `index.html` — the page: pickers, driver list, tabs and their controls.
- `css/styles.css` — all styling, including the dark theme (it follows
  your system's light/dark setting).
- `js/app.js` — UI wiring: the Year → Grand Prix → Session → Drivers
  cascade, startup auto-selection, the lap pickers and the Plot buttons.
- `js/charts.js` — builds each tab's [Plotly](https://plotly.com/javascript/)
  figure from already-fetched data (lap filtering, distance integration,
  gap-to-leader, tyre stint bands, outlier removal).
- `js/api.js` — a thin wrapper around the OpenF1 REST endpoints
  (`meetings`, `sessions`, `drivers`, `laps`, `stints`, `car_data`).
- `js/teamOrder.js` — a bundled table of each season's final
  Constructors' Championship order, used to order teams in the driver
  list. Add a line for each season once it ends (until then the most
  recent season in the table is used). It also maps team renames, e.g.
  AlphaTauri / RB / Racing Bulls, onto the same team, so add any new
  team name OpenF1 starts using there.
- `js/circuitInfo.js` + `data/circuit_corners.json` — a bundled,
  pre-generated table of corner positions per circuit (as a fraction of
  the lap, 0-1), keyed by OpenF1's `circuit_key`. If a circuit isn't in
  the table (e.g. a brand-new track), the telemetry plot just falls back
  to a plain "Distance into lap (m)" axis.
- `js/util.js` — timestamp parsing, lap-time formatting and the driver
  colour logic.

Plotly is loaded from the jsDelivr CDN at a pinned version with an
integrity hash; nothing else is fetched except the OpenF1 API.

## Notes and things worth knowing

- **Caching**: every OpenF1 response is kept in memory for as long as the
  tab is open, so switching drivers, metrics or options and clicking Plot
  again re-draws instantly with no new network request. Telemetry is
  cached per driver and lap too. Reloading the page starts afresh.
- **Rate limits**: OpenF1's free tier rate-limits bursts of requests. The
  app spaces its requests out slightly and automatically retries a
  "too many requests" response a few times before giving up; if you
  still see the error, wait a few seconds and click Plot again.
- **Pit laps**: "Exclude pit out laps" is on by default, because an
  out-lap's time is inflated by the pit lane and would otherwise dwarf
  the racing laps on the graph.
- **"Faster laps plotted higher"**: inverts the y-axis for time-based
  metrics, so a driver improving pace draws an upward line instead of a
  downward one. Doesn't apply to Speed Trap (higher is already better).
- **Telemetry lap picker**: only laps with a recorded start time and
  duration are listed, since those two fields are what's used to carve a
  time window out of `/car_data` for that lap.
- **Extending it**: the `/laps` endpoint also has `i1_speed`, `i2_speed`,
  and the "mini-sector" segments if you want to add more Lap Summary
  metrics - add them to `METRICS` in `js/charts.js`.
