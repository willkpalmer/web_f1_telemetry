/**
 * Corner locations for the telemetry x-axis.
 *
 * OpenF1 has no circuit-layout endpoint, so data/circuit_corners.json
 * bundles each circuit's corners ahead of time, keyed by OpenF1's
 * circuit_key. Each corner's position is stored as a *fraction* of the
 * lap (0-1) rather than a distance, so it can be multiplied by whatever
 * distance-into-lap the app integrates for the specific lap being
 * plotted.
 */

let circuitsPromise = null;

function loadCircuits() {
  if (!circuitsPromise) {
    circuitsPromise = fetch("data/circuit_corners.json")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .catch((err) => {
        circuitsPromise = null; // let a later call retry
        throw err;
      });
  }
  return circuitsPromise;
}

/**
 * Corner markers - {number, letter, fraction} - for the given circuit, or
 * [] if it isn't in the bundled data (e.g. a brand-new track), in which
 * case the telemetry plot falls back to a plain distance axis.
 */
export async function getCorners(circuitKey) {
  if (circuitKey === null || circuitKey === undefined) return [];
  try {
    const circuits = await loadCircuits();
    return circuits[String(circuitKey)] || [];
  } catch {
    return [];
  }
}
