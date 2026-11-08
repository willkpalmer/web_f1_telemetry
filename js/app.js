/**
 * OpenF1 Lap Viewer - UI wiring.
 *
 * Cascading pickers (Year -> Grand Prix -> Session -> Drivers) shared by
 * four chart tabs. All OpenF1 calls go through api.js, which caches every
 * response, so re-plotting with different options is instant.
 */

import * as api from "./api.js";
import * as charts from "./charts.js";
import { getCorners } from "./circuitInfo.js";
import { compareKeys, teamSortKey } from "./teamOrder.js";
import { driverLabel, formatIsoUtc, parseIsoMs, teamColour } from "./util.js";

const FIRST_HISTORICAL_YEAR = 2023;

// Most drivers that can be ticked at once - beyond this the charts
// (especially Tyre Strategy's one-panel-per-driver stack) get too crowded.
const MAX_SELECTED_DRIVERS = 6;

const DEFAULT_CHANNELS = ["Speed", "Throttle", "Brake", "Gear"];

const $ = (id) => document.getElementById(id);

const els = {
  year: $("year"),
  meeting: $("meeting"),
  session: $("session"),
  driverList: $("driver-list"),
  driverCount: $("driver-count"),
  clearDrivers: $("clear-drivers"),
  status: $("status"),
  statusText: $("status-text"),
  message: $("message"),
  messageText: $("message-text"),
  messageClose: $("message-close"),
  // Lap Summary
  metric: $("metric"),
  summaryExcludePit: $("summary-exclude-pit"),
  summaryInvert: $("summary-invert"),
  summaryPlot: $("summary-plot"),
  // Car Telemetry
  linkLaps: $("link-laps"),
  lap: $("lap"),
  lapField: $("lap-field"),
  perDriverLaps: $("per-driver-laps"),
  channels: $("channels"),
  telemetryPlot: $("telemetry-plot"),
  // Race Trace
  tracePlot: $("trace-plot"),
  // Tyre Strategy
  tyreExcludePit: $("tyre-exclude-pit"),
  tyreExcludeOutliers: $("tyre-exclude-outliers"),
  tyreOutlierPercent: $("tyre-outlier-percent"),
  tyreInvert: $("tyre-invert"),
  tyrePlot: $("tyre-plot"),
  tyreLegend: $("tyre-legend"),
};

const state = {
  year: null,
  meetingKey: null,
  sessionKey: null,
  meetingsByKey: new Map(),
  drivers: [], // current session's drivers, in display order
  driversLoaded: false,
  checked: new Set(), // ticked driver_numbers
  laps: null, // current session's laps, once loaded
  perDriverLap: new Map(), // driver_number -> lap number (unlinked mode)
  // Bumped on every user-driven year/Grand Prix change, so the slow
  // startup auto-detection can tell it's stale and stop.
  userSelectionGeneration: 0,
  pendingStartupSessionKey: null,
  busy: new Set(), // tab ids with a plot in progress
  renders: new Map(), // tab id -> () => figure | null, for re-theming
};

// ---- Status bar and messages ---------------------------------------------

const activeStatuses = new Map();
let statusId = 0;

function beginStatus(text) {
  const id = ++statusId;
  activeStatuses.set(id, text);
  renderStatus();
  return () => {
    activeStatuses.delete(id);
    renderStatus();
  };
}

function renderStatus() {
  const texts = [...activeStatuses.values()];
  els.status.hidden = texts.length === 0;
  els.statusText.textContent = texts.length ? texts[texts.length - 1] : "";
}

function showMessage(text, kind = "info") {
  els.message.dataset.kind = kind;
  els.messageText.textContent = text;
  els.message.hidden = false;
}

function hideMessage() {
  els.message.hidden = true;
}

/** Awaits `promise` with a status message showing; on failure shows the
 * error and resolves to null instead of throwing. */
async function run(statusText, promise) {
  const end = beginStatus(statusText);
  try {
    return await promise;
  } catch (err) {
    showMessage(err.message || String(err), "error");
    return null;
  } finally {
    end();
  }
}

// ---- Select helpers -------------------------------------------------------

function setOptions(select, placeholder, options = []) {
  select.replaceChildren();
  if (placeholder) select.append(new Option(placeholder, ""));
  for (const { value, label } of options) select.append(new Option(label, String(value)));
}

const selectedNumber = (select) => (select.value === "" ? null : Number(select.value));

// ---- Cascading selection: Year -> Grand Prix -> Session -> Drivers --------

function populateYears() {
  const currentYear = new Date().getFullYear();
  const years = [];
  for (let year = currentYear; year >= FIRST_HISTORICAL_YEAR; year--) years.push({ value: year, label: year });
  setOptions(els.year, "Select year", years);
}

function resetBelowYear() {
  state.meetingKey = null;
  setOptions(els.meeting, "Select Grand Prix");
  resetBelowMeeting();
}

function resetBelowMeeting() {
  state.meetingKey = null;
  setOptions(els.session, "Select session");
  resetBelowSession();
}

function resetBelowSession() {
  state.sessionKey = null;
  state.drivers = [];
  state.driversLoaded = false;
  state.checked.clear();
  state.laps = null;
  state.perDriverLap.clear();
  setOptions(els.lap, "Select a session first");
  renderDriverList();
  renderPerDriverLaps();
  updateButtons();
}

async function onYearChanged() {
  const year = selectedNumber(els.year);
  state.year = year;
  resetBelowYear();
  if (year === null) return;
  const meetings = await run(`Loading ${year} calendar…`, api.getMeetings(year));
  if (!meetings || state.year !== year) return;
  populateMeetings(meetings, true);
}

function populateMeetings(meetings, autoSelect) {
  const sorted = [...meetings].sort((a, b) => (a.date_start || "").localeCompare(b.date_start || ""));
  for (const meeting of sorted) state.meetingsByKey.set(meeting.meeting_key, meeting);
  setOptions(
    els.meeting,
    "Select Grand Prix",
    sorted.map((m) => ({ value: m.meeting_key, label: m.meeting_name }))
  );
  if (autoSelect && sorted.length) {
    els.meeting.value = String(sorted[0].meeting_key);
    onMeetingChanged();
  }
}

async function onMeetingChanged() {
  const meetingKey = selectedNumber(els.meeting);
  resetBelowMeeting();
  state.meetingKey = meetingKey;
  if (meetingKey === null) return;

  const sessions = await run("Loading sessions…", api.getSessions(meetingKey));
  if (!sessions || state.meetingKey !== meetingKey) return;

  const preferred = state.pendingStartupSessionKey;
  state.pendingStartupSessionKey = null;
  populateSessions(sessions, preferred);
}

function populateSessions(sessions, preferredSessionKey) {
  const sorted = [...sessions].sort((a, b) => (a.date_start || "").localeCompare(b.date_start || ""));
  setOptions(
    els.session,
    "Select session",
    sorted.map((s) => ({ value: s.session_key, label: s.session_name }))
  );
  if (!sorted.length) return;
  const match = sorted.find((s) => s.session_key === preferredSessionKey) || sorted[0];
  els.session.value = String(match.session_key);
  onSessionChanged();
}

function onSessionChanged() {
  const sessionKey = selectedNumber(els.session);
  resetBelowSession();
  state.sessionKey = sessionKey;
  if (sessionKey === null) return;
  renderDriverList();

  run("Loading drivers…", api.getDrivers(sessionKey)).then((drivers) => {
    if (drivers && state.sessionKey === sessionKey) populateDrivers(drivers);
  });

  // Warm the lap-data cache in the background so Plot feels instant, and
  // fill the telemetry tab's Lap picker once it's in.
  run("Loading lap data…", api.getLaps(sessionKey)).then((laps) => {
    if (laps && state.sessionKey === sessionKey) populateLapPicker(laps);
  });
}

function populateDrivers(drivers) {
  // Grouped by team, in the order the teams finished last season's
  // Constructors' Championship, then alphabetically within each team.
  const meeting = state.meetingsByKey.get(state.meetingKey) || {};
  const season = meeting.year || state.year;
  const sortKey = (d) => [...teamSortKey(d.team_name, season), (d.name_acronym || "").toLowerCase()];
  state.drivers = [...drivers].sort((a, b) => compareKeys(sortKey(a), sortKey(b)));
  state.driversLoaded = true;
  renderDriverList();
  updateButtons();
}

// ---- Driver checklist -----------------------------------------------------

function checkedDrivers() {
  return state.drivers.filter((d) => state.checked.has(d.driver_number));
}

function renderDriverList() {
  els.driverList.replaceChildren();
  if (!state.drivers.length) {
    const empty = document.createElement("li");
    empty.className = "driver-empty";
    empty.textContent =
      state.sessionKey === null
        ? "Pick a session to list its drivers."
        : state.driversLoaded
          ? "OpenF1 has no drivers for this session yet."
          : "Loading drivers…";
    els.driverList.append(empty);
  }

  let previousTeam = null;
  for (const driver of state.drivers) {
    const team = driver.team_name || "Unknown team";
    if (team !== previousTeam) {
      const heading = document.createElement("li");
      heading.className = "team-heading";
      heading.textContent = team;
      els.driverList.append(heading);
      previousTeam = team;
    }

    const item = document.createElement("li");
    const label = document.createElement("label");
    label.className = "driver";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = String(driver.driver_number);
    box.checked = state.checked.has(driver.driver_number);
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = teamColour(driver);
    const acronym = document.createElement("span");
    acronym.className = "acronym";
    acronym.textContent = driverLabel(driver);
    const fullName = document.createElement("span");
    fullName.className = "full-name";
    fullName.textContent = driver.full_name || "";
    label.append(box, swatch, acronym, fullName);
    item.append(label);
    els.driverList.append(item);
  }
  updateDriverCount();
}

function onDriverToggled(event) {
  const box = event.target;
  if (!(box instanceof HTMLInputElement)) return;
  const number = Number(box.value);
  if (box.checked) {
    if (state.checked.size >= MAX_SELECTED_DRIVERS) {
      box.checked = false;
      showMessage(`At most ${MAX_SELECTED_DRIVERS} drivers can be selected - untick one first.`);
      return;
    }
    state.checked.add(number);
  } else {
    state.checked.delete(number);
  }
  updateDriverCount();
  if (!els.linkLaps.checked) renderPerDriverLaps();
}

function updateDriverCount() {
  const count = state.checked.size;
  els.driverCount.textContent = `${count} / ${MAX_SELECTED_DRIVERS} selected`;
  const full = count >= MAX_SELECTED_DRIVERS;
  for (const box of els.driverList.querySelectorAll("input[type=checkbox]")) {
    box.disabled = full && !box.checked;
  }
  els.clearDrivers.disabled = count === 0;
}

function clearDrivers() {
  state.checked.clear();
  for (const box of els.driverList.querySelectorAll("input[type=checkbox]")) box.checked = false;
  updateDriverCount();
  if (!els.linkLaps.checked) renderPerDriverLaps();
}

// ---- Lap pickers (Car Telemetry tab) ----------------------------------------

/** Laps usable for telemetry: they need a start time and a duration to
 * carve a time window out of /car_data. */
const isTimedLap = (lap) => lap.date_start && lap.lap_duration && lap.lap_number != null;

function timedLapNumbers(laps, driverNumber = null) {
  const numbers = new Set();
  for (const lap of laps || []) {
    if (isTimedLap(lap) && (driverNumber === null || lap.driver_number === driverNumber)) numbers.add(lap.lap_number);
  }
  return [...numbers].sort((a, b) => a - b);
}

function populateLapPicker(laps) {
  state.laps = laps;
  const numbers = timedLapNumbers(laps);
  setOptions(
    els.lap,
    numbers.length ? "Select lap" : "No timed laps",
    numbers.map((n) => ({ value: n, label: `Lap ${n}` }))
  );
  if (numbers.length) els.lap.value = String(numbers[0]);
  if (!els.linkLaps.checked) renderPerDriverLaps();
  updateButtons();
}

function onLinkLapsToggled() {
  const linked = els.linkLaps.checked;
  els.lapField.hidden = !linked;
  els.perDriverLaps.hidden = linked;
  if (!linked) renderPerDriverLaps();
}

/** Rebuilds one lap picker per ticked driver, keeping each driver's
 * earlier choice where possible and defaulting new rows to the shared
 * picker's lap. */
function renderPerDriverLaps() {
  els.perDriverLaps.replaceChildren();
  const drivers = checkedDrivers();
  for (const number of [...state.perDriverLap.keys()]) {
    if (!state.checked.has(number)) state.perDriverLap.delete(number);
  }
  if (!drivers.length) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "Tick drivers to pick a lap for each.";
    els.perDriverLaps.append(hint);
    return;
  }

  const defaultLap = selectedNumber(els.lap);
  for (const driver of drivers) {
    const number = driver.driver_number;
    const lapNumbers = timedLapNumbers(state.laps, number);

    const label = document.createElement("label");
    label.className = "field inline";
    const caption = document.createElement("span");
    caption.textContent = driverLabel(driver);
    const select = document.createElement("select");
    setOptions(select, lapNumbers.length ? "Select lap" : "No timed laps", lapNumbers.map((n) => ({ value: n, label: `Lap ${n}` })));

    const wanted = state.perDriverLap.get(number) ?? defaultLap;
    const chosen = lapNumbers.includes(wanted) ? wanted : lapNumbers[0];
    if (chosen !== undefined) {
      select.value = String(chosen);
      state.perDriverLap.set(number, chosen);
    }
    select.addEventListener("change", () => {
      const value = selectedNumber(select);
      if (value === null) state.perDriverLap.delete(number);
      else state.perDriverLap.set(number, value);
    });

    label.append(caption, select);
    els.perDriverLaps.append(label);
  }
}

/** driver_number -> lap number to plot, or null (after saying why) if the
 * selection is incomplete. */
function lapSelectionByDriver(drivers) {
  if (els.linkLaps.checked) {
    const lap = selectedNumber(els.lap);
    if (lap === null) {
      showMessage("Select a lap to plot.");
      return null;
    }
    return new Map(drivers.map((d) => [d.driver_number, lap]));
  }
  const selection = new Map();
  const missing = [];
  for (const driver of drivers) {
    const lap = state.perDriverLap.get(driver.driver_number);
    if (lap === undefined) missing.push(driverLabel(driver));
    else selection.set(driver.driver_number, lap);
  }
  if (missing.length) {
    showMessage(`Select a lap for: ${missing.join(", ")}`);
    return null;
  }
  return selection;
}

// ---- Startup default: current year + most recent GP with data -------------

function setYearSilently(year) {
  els.year.value = String(year);
  state.year = year;
  resetBelowYear();
}

async function autoselectStartupDefaults() {
  const generation = state.userSelectionGeneration;
  const isStale = () => generation !== state.userSelectionGeneration;

  let year = new Date().getFullYear();
  for (;;) {
    setYearSilently(year);
    const meetings = await run(`Loading ${year} calendar…`, api.getMeetings(year));
    if (isStale() || !meetings) return;

    const now = Date.now();
    const past = meetings.filter((m) => !m.is_cancelled && m.date_start && parseIsoMs(m.date_start) <= now);
    if (!past.length && year > FIRST_HISTORICAL_YEAR) {
      // Nothing has happened yet this year (e.g. before the season
      // starts) - fall back to last year's most recent meeting.
      year -= 1;
      continue;
    }

    populateMeetings(meetings, false);
    past.sort((a, b) => b.date_start.localeCompare(a.date_start));

    // Walk back from the most recent meeting/session until one actually
    // has data (OpenF1 can lag a live weekend by a while).
    for (const meeting of past) {
      const sessions = await run(`Checking ${meeting.meeting_name}…`, api.getSessions(meeting.meeting_key));
      if (isStale() || !sessions) return;
      const newestFirst = [...sessions].sort((a, b) => (b.date_start || "").localeCompare(a.date_start || ""));
      for (const session of newestFirst) {
        const drivers = await run(
          `Checking ${meeting.meeting_name} – ${session.session_name}…`,
          api.getDrivers(session.session_key)
        );
        if (isStale() || !drivers) return;
        if (drivers.length) {
          state.pendingStartupSessionKey = session.session_key;
          els.meeting.value = String(meeting.meeting_key);
          onMeetingChanged();
          return;
        }
      }
    }
    return;
  }
}

// ---- Rendering --------------------------------------------------------------

const PLOT_CONFIG = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"],
  toImageButtonOptions: { format: "png", scale: 2 },
};

function chartTitle(suffix) {
  const meeting = els.meeting.selectedOptions[0]?.textContent || "";
  const session = els.session.selectedOptions[0]?.textContent || "";
  return `${meeting} – ${session} – ${suffix}`;
}

/** Remembers how to build a tab's figure (so it can be rebuilt on a theme
 * change) and draws it. */
function render(tab, title, build) {
  state.renders.set(tab, { title, build });
  draw(tab);
}

function draw(tab) {
  const entry = state.renders.get(tab);
  if (!entry) return;
  const panel = $(`panel-${tab}`);
  const plot = panel.querySelector(".plot");
  const placeholder = panel.querySelector(".placeholder");
  const heading = panel.querySelector(".chart-title");

  const figure = entry.build();
  if (!figure) {
    window.Plotly?.purge(plot);
    plot.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = "No data for this selection.";
    heading.textContent = entry.title;
    if (tab === "tyres") els.tyreLegend.hidden = true;
    return;
  }

  if (!window.Plotly) {
    showMessage("The charting library failed to load - check your connection and reload the page.", "error");
    return;
  }
  heading.textContent = entry.title;
  placeholder.hidden = true;
  plot.hidden = false;
  window.Plotly.react(plot, figure.data, figure.layout, PLOT_CONFIG);
  if (tab === "tyres") renderTyreLegend(figure.compounds);
}

function renderTyreLegend(compounds) {
  els.tyreLegend.replaceChildren();
  const entries = [
    ...compounds.map((c) => ({ key: c, label: charts.TYRE_COMPOUNDS[c]?.label ?? "Unknown" })),
    { key: "EXCLUDED", label: "Excluded" },
  ];
  for (const { key, label } of entries) {
    const item = document.createElement("li");
    const swatch = document.createElement("span");
    swatch.className = `tyre-swatch tyre-${key.toLowerCase()}`;
    if (charts.TYRE_COMPOUNDS[key]) swatch.style.background = charts.TYRE_COMPOUNDS[key].colour;
    item.append(swatch, label);
    els.tyreLegend.append(item);
  }
  els.tyreLegend.hidden = false;
}

function updateButtons() {
  const hasDrivers = state.drivers.length > 0;
  els.summaryPlot.disabled = !hasDrivers || state.busy.has("summary");
  els.tracePlot.disabled = !hasDrivers || state.busy.has("trace");
  els.tyrePlot.disabled = !hasDrivers || state.busy.has("tyres");
  els.telemetryPlot.disabled =
    !hasDrivers || timedLapNumbers(state.laps).length === 0 || state.busy.has("telemetry");
}

/** Shared preamble for every Plot button: checks a session and drivers are
 * picked, marks the tab busy, and runs `body(sessionKey, drivers)`. */
async function plotAction(tab, body) {
  const sessionKey = state.sessionKey;
  if (sessionKey === null) return;
  const drivers = checkedDrivers();
  if (!drivers.length) {
    showMessage("Tick at least one driver to plot.");
    return;
  }
  hideMessage();
  state.busy.add(tab);
  updateButtons();
  try {
    await body(sessionKey, drivers);
  } finally {
    state.busy.delete(tab);
    updateButtons();
  }
}

// ---- Plot actions -------------------------------------------------------------

function plotSummary() {
  return plotAction("summary", async (sessionKey, drivers) => {
    const metric = els.metric.value;
    const options = {
      metric,
      excludePitLaps: els.summaryExcludePit.checked,
      invert: els.summaryInvert.checked,
    };
    const title = chartTitle(metric);
    const laps = await run("Loading lap data…", api.getLaps(sessionKey));
    if (!laps || state.sessionKey !== sessionKey) return;
    render("summary", title, () => charts.lapSummary({ laps, drivers, ...options }));
  });
}

function plotTelemetry() {
  return plotAction("telemetry", async (sessionKey, drivers) => {
    const lapByDriver = lapSelectionByDriver(drivers);
    if (!lapByDriver) return;
    const channels = [...els.channels.querySelectorAll("input:checked")].map((box) => box.value);
    if (!channels.length) {
      showMessage("Tick at least one channel to plot.");
      return;
    }

    const lapNumbers = new Set(lapByDriver.values());
    const title = chartTitle(lapNumbers.size === 1 ? `Lap ${[...lapNumbers][0]}` : "Various laps");
    const meeting = state.meetingsByKey.get(state.meetingKey);

    const laps = await run("Loading lap data…", api.getLaps(sessionKey));
    if (!laps || state.sessionKey !== sessionKey) return;
    const lapIndex = new Map(laps.filter(isTimedLap).map((lap) => [`${lap.driver_number}:${lap.lap_number}`, lap]));

    const requests = [];
    for (const driver of drivers) {
      const lap = lapIndex.get(`${driver.driver_number}:${lapByDriver.get(driver.driver_number)}`);
      if (!lap) continue; // this driver has no timed lap with that number
      const start = parseIsoMs(lap.date_start);
      const from = formatIsoUtc(Math.floor(start));
      const to = formatIsoUtc(Math.ceil(start + lap.lap_duration * 1000));
      requests.push(
        api.getCarData(sessionKey, driver.driver_number, from, to).then((rows) => [driver.driver_number, rows])
      );
    }

    const end = beginStatus("Loading telemetry…");
    const [results, corners] = await Promise.all([
      Promise.allSettled(requests),
      getCorners(meeting?.circuit_key),
    ]).finally(end);
    if (state.sessionKey !== sessionKey) return;

    const dataByDriver = new Map();
    for (const result of results) {
      if (result.status === "fulfilled") dataByDriver.set(...result.value);
    }
    const failure = results.find((r) => r.status === "rejected");
    if (failure) showMessage(failure.reason?.message || String(failure.reason), "error");

    render("telemetry", title, () => charts.telemetry({ drivers, dataByDriver, channels, corners }));
  });
}

function plotRaceTrace() {
  return plotAction("trace", async (sessionKey, drivers) => {
    const title = chartTitle("Race Trace");
    const laps = await run("Loading lap data…", api.getLaps(sessionKey));
    if (!laps || state.sessionKey !== sessionKey) return;
    render("trace", title, () => charts.raceTrace({ laps, drivers }));
  });
}

function plotTyres() {
  return plotAction("tyres", async (sessionKey, drivers) => {
    const percent = Number(els.tyreOutlierPercent.value);
    if (!(percent >= 1 && percent <= 100)) {
      showMessage("The outlier threshold must be between 1% and 100%.");
      return;
    }
    const options = {
      excludePitLaps: els.tyreExcludePit.checked,
      excludeOutliers: els.tyreExcludeOutliers.checked,
      outlierThreshold: percent / 100,
      invert: els.tyreInvert.checked,
    };
    const title = chartTitle("Lap Times by Tyre");
    const result = await run(
      "Loading lap and tyre data…",
      Promise.all([api.getLaps(sessionKey), api.getStints(sessionKey)])
    );
    if (!result || state.sessionKey !== sessionKey) return;
    const [laps, stints] = result;
    render("tyres", title, () => charts.tyreStrategy({ laps, stints, drivers, ...options }));
  });
}

// ---- Tabs -----------------------------------------------------------------------

const TAB_STORAGE_KEY = "openf1-viewer:tab";

function selectTab(tab, focus = false) {
  for (const button of document.querySelectorAll('[role="tab"]')) {
    const selected = button.dataset.tab === tab;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    if (selected && focus) button.focus();
    $(`panel-${button.dataset.tab}`).hidden = !selected;
  }
  // A chart drawn while its tab was hidden has the wrong size.
  const plot = $(`panel-${tab}`).querySelector(".plot");
  if (window.Plotly && plot && !plot.hidden && plot.data) window.Plotly.Plots.resize(plot);
  try {
    localStorage.setItem(TAB_STORAGE_KEY, tab);
  } catch {
    /* storage unavailable - not important */
  }
}

function initTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  for (const button of tabs) {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      const index = tabs.indexOf(button);
      let next = null;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
      if (event.key === "Home") next = tabs[0];
      if (event.key === "End") next = tabs[tabs.length - 1];
      if (next) {
        event.preventDefault();
        selectTab(next.dataset.tab, true);
      }
    });
  }
  let saved = null;
  try {
    saved = localStorage.getItem(TAB_STORAGE_KEY);
  } catch {
    /* ignore */
  }
  selectTab(tabs.some((b) => b.dataset.tab === saved) ? saved : tabs[0].dataset.tab);
}

// ---- Wiring -----------------------------------------------------------------------

function populateStaticControls() {
  setOptions(els.metric, null, Object.keys(charts.METRICS).map((m) => ({ value: m, label: m })));
  for (const name of Object.keys(charts.TELEMETRY_CHANNELS)) {
    const label = document.createElement("label");
    label.className = "check";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = name;
    box.checked = DEFAULT_CHANNELS.includes(name);
    label.append(box, ` ${name}`);
    els.channels.append(label);
  }
}

function init() {
  populateYears();
  populateStaticControls();
  initTabs();
  renderDriverList();
  updateButtons();

  els.year.addEventListener("change", () => {
    state.userSelectionGeneration++;
    onYearChanged();
  });
  els.meeting.addEventListener("change", () => {
    state.userSelectionGeneration++;
    onMeetingChanged();
  });
  els.session.addEventListener("change", () => {
    state.userSelectionGeneration++;
    onSessionChanged();
  });
  els.driverList.addEventListener("change", onDriverToggled);
  els.clearDrivers.addEventListener("click", clearDrivers);
  els.linkLaps.addEventListener("change", onLinkLapsToggled);
  els.tyreExcludeOutliers.addEventListener("change", () => {
    els.tyreOutlierPercent.disabled = !els.tyreExcludeOutliers.checked;
  });
  els.messageClose.addEventListener("click", hideMessage);

  els.summaryPlot.addEventListener("click", plotSummary);
  els.telemetryPlot.addEventListener("click", plotTelemetry);
  els.tracePlot.addEventListener("click", plotRaceTrace);
  els.tyrePlot.addEventListener("click", plotTyres);

  // Re-theme drawn charts when the OS switches light/dark.
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    for (const tab of state.renders.keys()) draw(tab);
  });

  if (!window.Plotly) {
    showMessage("The charting library failed to load - check your connection and reload the page.", "error");
  }

  autoselectStartupDefaults();
}

init();
