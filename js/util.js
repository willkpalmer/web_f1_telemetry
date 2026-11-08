/** Small shared helpers: timestamps, colours, number formatting. */

const ISO_RE = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/;

/**
 * Milliseconds since the epoch for an OpenF1 ISO timestamp, keeping
 * sub-millisecond precision (OpenF1 sends microseconds, which some
 * browsers' Date parsing rejects or truncates). Timestamps without a
 * zone are treated as UTC.
 */
export function parseIsoMs(value) {
  const match = ISO_RE.exec(value || "");
  if (!match) return Date.parse(value);
  const [, base, fraction = "", zone = "Z"] = match;
  const wholeSeconds = Date.parse(base + zone);
  return wholeSeconds + (fraction ? Number(fraction) * 1000 : 0);
}

/** A UTC timestamp OpenF1 accepts in a filter, e.g. 2024-03-02T15:05:31.123 */
export function formatIsoUtc(ms) {
  return new Date(ms).toISOString().slice(0, 23);
}

/** 92.456 -> "1:32.456"; 31.2 -> "31.200 s" */
export function formatSeconds(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "–";
  if (seconds < 60) return `${seconds.toFixed(3)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${(seconds - minutes * 60).toFixed(3).padStart(6, "0")}`;
}

export function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** "#rrggbb" for an OpenF1 team_colour ("3671C6"), grey if missing/bad. */
export function teamColour(driver) {
  const hex = driver && driver.team_colour;
  return /^[0-9a-f]{6}$/i.test(hex || "") ? `#${hex}` : "#888888";
}

export function driverLabel(driver) {
  return driver.name_acronym || String(driver.driver_number);
}

// --- HLS colour maths (ported from Python's colorsys) -------------------

function rgbToHls(r, g, b) {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (min + max) / 2;
  if (min === max) return [0, l, 0];
  const s = l <= 0.5 ? (max - min) / (max + min) : (max - min) / (2 - max - min);
  const rc = (max - r) / (max - min);
  const gc = (max - g) / (max - min);
  const bc = (max - b) / (max - min);
  let h;
  if (r === max) h = bc - gc;
  else if (g === max) h = 2 + rc - bc;
  else h = 4 + gc - rc;
  h = (((h / 6) % 1) + 1) % 1;
  return [h, l, s];
}

function hueToChannel(m1, m2, hue) {
  hue = ((hue % 1) + 1) % 1;
  if (hue < 1 / 6) return m1 + (m2 - m1) * hue * 6;
  if (hue < 0.5) return m2;
  if (hue < 2 / 3) return m1 + (m2 - m1) * (2 / 3 - hue) * 6;
  return m1;
}

function hlsToRgb(h, l, s) {
  if (s === 0) return [l, l, l];
  const m2 = l <= 0.5 ? l * (1 + s) : l + s - l * s;
  const m1 = 2 * l - m2;
  return [hueToChannel(m1, m2, h + 1 / 3), hueToChannel(m1, m2, h), hueToChannel(m1, m2, h - 1 / 3)];
}

/** Nudges a hex colour's lightness apart for the `index`-th driver sharing
 * it, alternating lighter/darker and growing the shift for each further
 * one; index 0 returns the colour unchanged. */
function shiftLightness(hex, index) {
  if (index === 0) return hex;
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const [h, l, s] = rgbToHls(r, g, b);
  const step = Math.floor((index + 1) / 2);
  const sign = index % 2 ? 1 : -1;
  const shifted = Math.min(0.92, Math.max(0.08, l + sign * step * 0.22));
  const toHex = (v) => Math.round(v * 255).toString(16).padStart(2, "0");
  return `#${hlsToRgb(h, shifted, s).map(toHex).join("")}`;
}

/**
 * Map of driver_number -> colour to plot that driver in.
 *
 * Team-mates share an identical team_colour, so each driver beyond the
 * first with a given colour gets its lightness nudged apart, keeping every
 * line legible while still reading as "that team's colour".
 */
export function driverPlotColours(drivers) {
  const colours = new Map();
  const seen = new Map();
  for (const driver of drivers) {
    const base = teamColour(driver);
    const index = seen.get(base) || 0;
    seen.set(base, index + 1);
    colours.set(driver.driver_number, shiftLightness(base, index));
  }
  return colours;
}
