/**
 * Builds Plotly figures for each tab. Every builder is a pure function of
 * already-fetched data and the tab's options, returning
 * {data, layout, ...extras}, or null when there's nothing to plot. Colours
 * come from the page's CSS tokens at build time, so re-running a builder
 * after a light/dark switch re-themes the chart.
 */

import { driverLabel, driverPlotColours, formatSeconds, median, parseIsoMs } from "./util.js";

// Display name -> field on the /laps object and y-axis label.
export const METRICS = {
  "Lap Time": { field: "lap_duration", label: "Lap time (s)" },
  "Sector 1": { field: "duration_sector_1", label: "Sector 1 time (s)" },
  "Sector 2": { field: "duration_sector_2", label: "Sector 2 time (s)" },
  "Sector 3": { field: "duration_sector_3", label: "Sector 3 time (s)" },
  "Speed Trap": { field: "st_speed", label: "Speed (km/h)" },
};

// Display name -> field on the /car_data object and y-axis label. OpenF1's
// free telemetry has no steering-angle channel - this is everything it has.
export const TELEMETRY_CHANNELS = {
  Speed: { field: "speed", label: "Speed (km/h)" },
  Throttle: { field: "throttle", label: "Throttle (%)" },
  Brake: { field: "brake", label: "Brake (%)" },
  Gear: { field: "n_gear", label: "Gear" },
  RPM: { field: "rpm", label: "Engine RPM" },
  DRS: { field: "drs", label: "DRS (raw status code)" },
};

// OpenF1 compound -> pale versions of the colours Pirelli paints on the
// sidewalls, so the lap-time line stays readable on top. Hard is really
// white, but excluded laps are left blank, so Hard is light grey instead.
export const TYRE_COMPOUNDS = {
  SOFT: { colour: "#f7a8b6", label: "Soft" },
  MEDIUM: { colour: "#ffe58f", label: "Medium" },
  HARD: { colour: "#d6d6d6", label: "Hard" },
  INTERMEDIATE: { colour: "#a9dcaf", label: "Intermediate" },
  WET: { colour: "#9fc3e3", label: "Wet" },
};
export const UNKNOWN_HATCH_COLOUR = "#b0b0b0";

// ---- Shared styling --------------------------------------------------------

function theme() {
  const styles = getComputedStyle(document.documentElement);
  const token = (name) => styles.getPropertyValue(name).trim();
  return {
    text: token("--text"),
    muted: token("--text-muted"),
    grid: token("--chart-grid"),
    plotBg: token("--chart-bg"),
    paperBg: token("--surface"),
    font: token("--font-sans"),
  };
}

function baseLayout(t, overrides = {}) {
  return {
    paper_bgcolor: t.paperBg,
    plot_bgcolor: t.plotBg,
    font: { family: t.font, color: t.text, size: 12 },
    margin: { l: 64, r: 16, t: 36, b: 48 },
    showlegend: true,
    legend: { orientation: "h", x: 0, y: 1, yanchor: "bottom", font: { size: 12 } },
    hovermode: "closest",
    hoverlabel: { font: { family: t.font } },
    ...overrides,
  };
}

function axis(t, overrides = {}) {
  return {
    gridcolor: t.grid,
    zerolinecolor: t.grid,
    linecolor: t.grid,
    tickcolor: t.grid,
    automargin: true,
    ...overrides,
  };
}

/** Stacked subplot domains, top panel first. */
function stackedDomains(count, gap = 0.035) {
  const effectiveGap = count > 1 ? gap : 0;
  const height = (1 - effectiveGap * (count - 1)) / count;
  return Array.from({ length: count }, (_, i) => {
    const top = 1 - i * (height + effectiveGap);
    return [Math.max(0, top - height), top];
  });
}

const axisNames = (i) => ({ trace: i ? `y${i + 1}` : "y", layout: i ? `yaxis${i + 1}` : "yaxis" });

const byLapNumber = (a, b) => (a.lap_number ?? 0) - (b.lap_number ?? 0);

// ---- Lap Summary -----------------------------------------------------------

export function lapSummary({ laps, drivers, metric, excludePitLaps, invert }) {
  const { field, label } = METRICS[metric];
  const isTime = field !== "st_speed";
  const t = theme();
  const colours = driverPlotColours(drivers);

  const data = [];
  for (const driver of drivers) {
    const points = laps
      .filter((lap) => lap.driver_number === driver.driver_number)
      .filter((lap) => !(excludePitLaps && lap.is_pit_out_lap))
      .filter((lap) => lap[field] !== null && lap[field] !== undefined && lap.lap_number != null)
      .sort(byLapNumber);
    if (!points.length) continue;

    const name = driverLabel(driver);
    data.push({
      type: "scatter",
      mode: "lines+markers",
      name,
      x: points.map((lap) => lap.lap_number),
      y: points.map((lap) => lap[field]),
      customdata: points.map((lap) => (isTime ? formatSeconds(lap[field]) : `${lap[field]} km/h`)),
      line: { color: colours.get(driver.driver_number), width: 2 },
      marker: { size: 5 },
      hovertemplate: `<b>${name}</b> · Lap %{x}<br>%{customdata}<extra></extra>`,
    });
  }
  if (!data.length) return null;

  return {
    data,
    layout: baseLayout(t, {
      xaxis: axis(t, { title: { text: "Lap" } }),
      yaxis: axis(t, {
        title: { text: label },
        autorange: invert && isTime ? "reversed" : true,
      }),
    }),
  };
}

// ---- Car Telemetry ---------------------------------------------------------

/**
 * OpenF1's car_data has no distance channel, so this integrates speed
 * over time (trapezoidal rule) to get metres travelled since the first
 * sample - close enough to align drivers by track position (e.g.
 * corner-for-corner) rather than by elapsed time.
 */
function cumulativeDistance(rowsSorted) {
  const distance = [0];
  for (let i = 1; i < rowsSorted.length; i++) {
    const previous = rowsSorted[i - 1];
    const current = rowsSorted[i];
    const dt = (parseIsoMs(current.date) - parseIsoMs(previous.date)) / 1000;
    const avgSpeedMs = ((previous.speed || 0) + (current.speed || 0)) / 2 / 3.6;
    distance.push(distance[i - 1] + avgSpeedMs * dt);
  }
  return distance;
}

export function telemetry({ drivers, dataByDriver, channels, corners }) {
  const t = theme();
  const colours = driverPlotColours(drivers);

  const series = [];
  for (const driver of drivers) {
    const rows = dataByDriver.get(driver.driver_number);
    if (!rows || !rows.length) continue;
    const sorted = [...rows].sort((a, b) => parseIsoMs(a.date) - parseIsoMs(b.date));
    series.push({ driver, rows: sorted, distance: cumulativeDistance(sorted) });
  }
  if (!series.length) return null;

  const lapDistance = Math.max(...series.map((s) => s.distance[s.distance.length - 1]));
  const domains = stackedDomains(channels.length);
  const layout = baseLayout(t, { hovermode: "x unified", margin: { l: 72, r: 16, t: 36, b: 48 } });
  const data = [];

  channels.forEach((channel, i) => {
    const { field, label } = TELEMETRY_CHANNELS[channel];
    const names = axisNames(i);
    layout[names.layout] = axis(t, {
      domain: domains[i],
      title: { text: label, font: { size: 11 } },
      ...(field === "n_gear" ? { dtick: 1 } : {}),
    });
    for (const { driver, rows, distance } of series) {
      const name = driverLabel(driver);
      data.push({
        type: "scatter",
        mode: "lines",
        name,
        legendgroup: String(driver.driver_number),
        showlegend: i === 0,
        x: distance,
        y: rows.map((row) => row[field] ?? null),
        xaxis: "x",
        yaxis: names.trace,
        line: { color: colours.get(driver.driver_number), width: 1.5 },
        hovertemplate: `${name}: %{y}<extra></extra>`,
      });
    }
  });

  // Corner positions are a fraction of the lap, so they scale to this
  // specific lap's own integrated distance automatically.
  const hasCorners = corners.length > 0 && lapDistance > 0;
  const cornerDistances = hasCorners ? corners.map((c) => c.fraction * lapDistance) : [];
  layout.xaxis = axis(t, {
    anchor: axisNames(channels.length - 1).trace,
    range: [0, lapDistance],
    hoverformat: ",.0f",
    title: { text: hasCorners ? "Corner (hover shows metres into lap)" : "Distance into lap (m)" },
    ...(hasCorners
      ? {
          tickmode: "array",
          tickvals: cornerDistances,
          ticktext: corners.map((c) => `${c.number}${c.letter || ""}`),
          tickfont: { size: 10 },
          showgrid: false,
        }
      : {}),
  });
  layout.shapes = cornerDistances.map((x) => ({
    type: "line",
    xref: "x",
    yref: "paper",
    x0: x,
    x1: x,
    y0: 0,
    y1: 1,
    layer: "below",
    line: { color: t.muted, width: 1, dash: "dot" },
    opacity: 0.45,
  }));
  layout.height = 90 + channels.length * 170;

  return { data, layout };
}

// ---- Race Trace ------------------------------------------------------------

/**
 * Map driver_number -> Map(lap_number -> seconds behind the leader),
 * computed from the *whole field's* laps (not just the drivers being
 * plotted), so the actual leader is used even if they're not ticked.
 *
 * Each driver's cumulative race time is the running total of their own
 * lap_duration values (pit stops included - a stop should show up as
 * losing time); the leader at a lap is whoever has the smallest cumulative
 * time there. A driver's series stops at the first lap missing timing
 * data (retirement, no time set, ...) rather than guessing.
 */
function gapsToLeader(allLaps) {
  const lapsByDriver = new Map();
  for (const lap of allLaps) {
    if (lap.driver_number == null) continue;
    if (!lapsByDriver.has(lap.driver_number)) lapsByDriver.set(lap.driver_number, []);
    lapsByDriver.get(lap.driver_number).push(lap);
  }

  const cumulativeByDriver = new Map();
  for (const [driverNumber, laps] of lapsByDriver) {
    const cumulative = new Map();
    let total = 0;
    for (const lap of [...laps].sort(byLapNumber)) {
      if (lap.lap_duration == null || lap.lap_number == null) break;
      total += lap.lap_duration;
      cumulative.set(lap.lap_number, total);
    }
    cumulativeByDriver.set(driverNumber, cumulative);
  }

  const leaderTime = new Map();
  for (const cumulative of cumulativeByDriver.values()) {
    for (const [lapNumber, total] of cumulative) {
      if (!leaderTime.has(lapNumber) || total < leaderTime.get(lapNumber)) leaderTime.set(lapNumber, total);
    }
  }

  const gaps = new Map();
  for (const [driverNumber, cumulative] of cumulativeByDriver) {
    gaps.set(driverNumber, new Map([...cumulative].map(([lap, total]) => [lap, total - leaderTime.get(lap)])));
  }
  return gaps;
}

export function raceTrace({ laps, drivers }) {
  const t = theme();
  const gapsByDriver = gapsToLeader(laps);
  const colours = driverPlotColours(drivers);

  const data = [];
  for (const driver of drivers) {
    const gaps = gapsByDriver.get(driver.driver_number);
    if (!gaps || !gaps.size) continue;
    const lapNumbers = [...gaps.keys()].sort((a, b) => a - b);
    const name = driverLabel(driver);
    data.push({
      type: "scatter",
      mode: "lines+markers",
      name,
      x: lapNumbers,
      // Negated so the leader sits flat at zero and everyone else sinks
      // below it in proportion to how far behind they are.
      y: lapNumbers.map((lap) => -gaps.get(lap)),
      customdata: lapNumbers.map((lap) => {
        const gap = gaps.get(lap);
        return gap < 0.0005 ? "Leader" : `+${gap.toFixed(3)} s`;
      }),
      line: { color: colours.get(driver.driver_number), width: 2 },
      marker: { size: 4 },
      hovertemplate: `<b>${name}</b> · Lap %{x}<br>%{customdata}<extra></extra>`,
    });
  }
  if (!data.length) return null;

  return {
    data,
    layout: baseLayout(t, {
      xaxis: axis(t, { title: { text: "Lap" } }),
      yaxis: axis(t, { title: { text: "Gap to leader (s)" } }),
    }),
  };
}

// ---- Tyre Strategy ---------------------------------------------------------

/** The compound (e.g. "SOFT") a driver was on for a lap, or "UNKNOWN". A
 * stint still running when the data was captured may have no lap_end, so
 * it's treated as open-ended. */
function compoundForLap(driverStints, lapNumber) {
  for (const stint of driverStints) {
    if (stint.lap_start == null || lapNumber < stint.lap_start) continue;
    if (stint.lap_end == null || lapNumber <= stint.lap_end) {
      const compound = (stint.compound || "").toUpperCase();
      return compound in TYRE_COMPOUNDS ? compound : "UNKNOWN";
    }
  }
  return "UNKNOWN";
}

/** Drops laps `threshold` (a fraction, e.g. 0.1) or more away from the
 * median of one driver's laps, in either direction. */
function withoutOutliers(laps, threshold) {
  if (!laps.length) return laps;
  const mid = median(laps.map((lap) => lap.lap_duration));
  return laps.filter((lap) => Math.abs(lap.lap_duration - mid) < threshold * mid);
}

export function tyreStrategy({ laps, stints, drivers, excludePitLaps, excludeOutliers, outlierThreshold, invert }) {
  const t = theme();

  // Work out each driver's laps first, so drivers with nothing to show
  // don't get an empty panel.
  const panels = [];
  for (const driver of drivers) {
    let driverLaps = laps.filter(
      (lap) => lap.driver_number === driver.driver_number && lap.lap_duration != null && lap.lap_number != null
    );
    if (excludePitLaps) driverLaps = driverLaps.filter((lap) => !lap.is_pit_out_lap);
    if (excludeOutliers) driverLaps = withoutOutliers(driverLaps, outlierThreshold);
    if (driverLaps.length) panels.push({ driver, laps: driverLaps.sort(byLapNumber) });
  }
  if (!panels.length) return null;

  // One shared, explicit y-range for every panel, so pace lines up reading
  // down the stack (and the hatched "unknown" bands can span it exactly).
  const allTimes = panels.flatMap((p) => p.laps.map((lap) => lap.lap_duration));
  const lo = Math.min(...allTimes);
  const hi = Math.max(...allTimes);
  const pad = (hi - lo) * 0.06 || 1;
  const yMin = lo - pad;
  const yMax = hi + pad;

  const colours = driverPlotColours(drivers);
  const domains = stackedDomains(panels.length, 0.05);
  const compoundsUsed = new Set();
  const data = [];
  const shapes = [];
  const layout = baseLayout(t, { showlegend: false, margin: { l: 72, r: 16, t: 12, b: 48 } });

  panels.forEach(({ driver, laps: driverLaps }, i) => {
    const names = axisNames(i);
    const driverStints = stints.filter((s) => s.driver_number === driver.driver_number);
    const name = driverLabel(driver);

    // Shade each plotted lap's slot (lap - 0.5 to lap + 0.5) in its
    // compound's colour, merging consecutive laps on the same compound
    // into one band. Excluded or missing laps get no band.
    const bands = [];
    for (const lap of driverLaps) {
      const compound = compoundForLap(driverStints, lap.lap_number);
      compoundsUsed.add(compound);
      const last = bands[bands.length - 1];
      if (last && last.compound === compound && last.to === lap.lap_number - 1) last.to = lap.lap_number;
      else bands.push({ compound, from: lap.lap_number, to: lap.lap_number });
    }
    for (const band of bands) {
      const x0 = band.from - 0.5;
      const x1 = band.to + 0.5;
      if (band.compound in TYRE_COMPOUNDS) {
        shapes.push({
          type: "rect", xref: "x", yref: `${names.trace} domain`,
          x0, x1, y0: 0, y1: 1, layer: "below",
          fillcolor: TYRE_COMPOUNDS[band.compound].colour, line: { width: 0 },
        });
      } else {
        // Shapes can't be hatched, so unknown-compound laps are a filled,
        // patterned polygon trace spanning the panel's fixed y-range.
        data.push({
          type: "scatter", mode: "lines", xaxis: "x", yaxis: names.trace,
          x: [x0, x1, x1, x0, x0], y: [yMin, yMin, yMax, yMax, yMin],
          fill: "toself", fillcolor: "rgba(0,0,0,0)",
          fillpattern: { shape: "/", fgcolor: UNKNOWN_HATCH_COLOUR, size: 8, solidity: 0.15 },
          line: { width: 0 }, hoverinfo: "skip", showlegend: false,
        });
      }
    }

    // Break the line wherever lap numbers skip (an excluded lap, or one
    // with no time), so the laps either side aren't joined up.
    const x = [];
    const y = [];
    const custom = [];
    driverLaps.forEach((lap, j) => {
      if (j && lap.lap_number !== driverLaps[j - 1].lap_number + 1) {
        x.push(null);
        y.push(null);
        custom.push(null);
      }
      x.push(lap.lap_number);
      y.push(lap.lap_duration);
      const compound = compoundForLap(driverStints, lap.lap_number);
      custom.push(`${formatSeconds(lap.lap_duration)} · ${TYRE_COMPOUNDS[compound]?.label ?? "Unknown tyre"}`);
    });
    // A white halo keeps the driver-coloured line readable on any
    // compound's shading (e.g. a red Ferrari line on Soft).
    data.push({
      type: "scatter", mode: "lines", xaxis: "x", yaxis: names.trace, x, y,
      connectgaps: false, line: { color: "#ffffff", width: 5 }, hoverinfo: "skip", showlegend: false,
    });
    data.push({
      type: "scatter", mode: "lines+markers", name, xaxis: "x", yaxis: names.trace, x, y,
      customdata: custom, connectgaps: false,
      line: { color: colours.get(driver.driver_number), width: 2 },
      marker: { size: 4 },
      hovertemplate: `<b>${name}</b> · Lap %{x}<br>%{customdata}<extra></extra>`,
    });

    layout[names.layout] = axis(t, {
      domain: domains[i],
      range: invert ? [yMax, yMin] : [yMin, yMax],
      ...(i ? { matches: "y" } : {}),
      title: { text: `<b>${name}</b><br>Lap time (s)`, font: { size: 11 } },
      layer: "above traces",
    });
  });

  layout.xaxis = axis(t, { anchor: axisNames(panels.length - 1).trace, title: { text: "Lap" }, layer: "above traces" });
  layout.shapes = shapes;
  layout.height = 70 + panels.length * 180;

  const compoundOrder = [...Object.keys(TYRE_COMPOUNDS), "UNKNOWN"];
  return { data, layout, compounds: compoundOrder.filter((c) => compoundsUsed.has(c)) };
}
