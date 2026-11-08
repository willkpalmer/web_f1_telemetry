/**
 * Thin wrapper around the OpenF1 REST API.
 *
 * Historical data (2023 onwards) is free and needs no authentication.
 * Docs: https://openf1.org/docs/
 *
 * Every successful response is cached in memory by URL (and in-flight
 * requests are shared), so re-plotting after a checkbox change, or two
 * parts of the UI asking for the same session's laps, never costs a
 * second network call. Requests are also spaced out a little and retried
 * on HTTP 429, because OpenF1's free tier rate-limits bursts - which
 * plotting telemetry for several drivers at once easily produces.
 */

const BASE_URL = "https://api.openf1.org/v1";
const REQUEST_TIMEOUT_MS = 20000;
const MIN_REQUEST_GAP_MS = 350;
const MAX_RATE_LIMIT_RETRIES = 3;

export class OpenF1Error extends Error {}

const cache = new Map(); // url -> Promise<list>

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// A simple FIFO gate: each request waits for its turn and then for at
// least MIN_REQUEST_GAP_MS since the previous one started.
let gate = Promise.resolve();
let lastRequestStart = 0;
function waitForTurn() {
  const turn = gate.then(async () => {
    const wait = lastRequestStart + MIN_REQUEST_GAP_MS - Date.now();
    if (wait > 0) await sleep(wait);
    lastRequestStart = Date.now();
  });
  gate = turn;
  return turn;
}

async function fetchJsonList(endpoint, url) {
  for (let attempt = 0; ; attempt++) {
    await waitForTurn();

    let response;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      response = await fetch(url, { signal: controller.signal });
    } catch (err) {
      const reason = err.name === "AbortError" ? "timed out" : err.message;
      throw new OpenF1Error(`Request to '${endpoint}' failed: ${reason}`);
    } finally {
      clearTimeout(timer);
    }

    if (response.status === 404) {
      // OpenF1 responds 404 ("No results found") for a query that
      // legitimately matches nothing, rather than an empty 200 list.
      return [];
    }
    if (response.status === 429) {
      if (attempt < MAX_RATE_LIMIT_RETRIES) {
        await sleep(1500 * 2 ** attempt);
        continue;
      }
      throw new OpenF1Error(
        `OpenF1 is rate-limiting requests to '${endpoint}' (too many requests) - ` +
          "wait a few seconds and try again."
      );
    }
    if (!response.ok) {
      throw new OpenF1Error(`Request to '${endpoint}' failed: HTTP ${response.status}`);
    }

    let data;
    try {
      data = await response.json();
    } catch {
      throw new OpenF1Error(`Could not decode JSON from '${endpoint}'`);
    }
    if (!Array.isArray(data)) {
      throw new OpenF1Error(`Unexpected response shape from '${endpoint}'`);
    }
    return data;
  }
}

/**
 * GET an OpenF1 endpoint and resolve to the decoded JSON list.
 *
 * `params` become query-string filters; null/undefined ones are dropped.
 * `extraQuery` is appended verbatim for OpenF1's comparison-operator
 * filters (e.g. "date>=...&date<=..."), which need a literal '>' / '<'
 * next to the '=' rather than being encoded as part of a value.
 */
function get(endpoint, params = {}, extraQuery = "") {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) query.append(key, String(value));
  }
  let queryString = query.toString();
  if (extraQuery) queryString = queryString ? `${queryString}&${extraQuery}` : extraQuery;
  const url = `${BASE_URL}/${endpoint}${queryString ? `?${queryString}` : ""}`;

  if (!cache.has(url)) {
    const request = fetchJsonList(endpoint, url);
    cache.set(url, request);
    // Don't cache failures - a retry should actually retry.
    request.catch(() => cache.delete(url));
  }
  return cache.get(url);
}

/** All Grand Prix / testing meetings for a given year, unsorted. */
export const getMeetings = (year) => get("meetings", { year });

/** All sessions (Practice, Qualifying, Race, ...) within a meeting. */
export const getSessions = (meetingKey) => get("sessions", { meeting_key: meetingKey });

/** Driver/car info (name, number, team, team colour) for a session. */
export const getDrivers = (sessionKey) => get("drivers", { session_key: sessionKey });

/**
 * Lap-by-lap timing data for every driver in a session - one request for
 * the whole field is far cheaper than one per driver.
 */
export const getLaps = (sessionKey) => get("laps", { session_key: sessionKey });

/** Tyre stint info (compound, lap range, tyre age) for a session. */
export const getStints = (sessionKey) => get("stints", { session_key: sessionKey });

/**
 * High-frequency (~3.7 Hz) telemetry for one driver within a time window:
 * speed, rpm, throttle, brake, n_gear, drs. `dateFrom`/`dateTo` are ISO
 * timestamps - OpenF1 refuses to hand back a whole session's data in one
 * request, so a tight time window is required.
 */
export const getCarData = (sessionKey, driverNumber, dateFrom, dateTo) =>
  get(
    "car_data",
    { session_key: sessionKey, driver_number: driverNumber },
    `date>=${dateFrom}&date<=${dateTo}`
  );
