"""
One-off/occasional generator for circuit_corners.json.

Rather than calling api.multiviewer.app (via FastF1) at app runtime -
which has turned out to be unreliably reachable from some machines - this
script fetches each circuit's corner layout once, computes each corner's
position as a *fraction* of the lap (rather than an absolute distance in
metres, since we don't have a calibrated reference lap here - see below),
and bakes the results into circuit_corners.json, which the app reads
locally with no network dependency at all.

Fraction, not metres: MultiViewer's raw corner "length" values and its
track outline (x/y polyline) are in the same, but unstated and non-metric,
unit system. FastF1 normally rescales that to metres by calibrating
against one real lap's telemetry. We don't need that: dividing a corner's
raw length by the polyline's own total arc length gives its position as a
fraction of one lap (0-1) in a way that's unit-independent, and the app
already computes its own real distance-into-lap from OpenF1 telemetry, so
multiplying that by each corner's fraction places it correctly on the
app's own axis without needing metres at all. Cross-checked against
FastF1's calibrated (metre) values for Monza and Silverstone: agrees
within ~2-4%, which is well inside the "not highly accurate, but useful
for visualization" accuracy MultiViewer's own data ships with.

Usage:
    python scripts/generate_circuit_corners.py [start_year] [end_year]

Re-run and commit the updated circuit_corners.json whenever a new circuit
shows up (or a layout changes) and corner numbers stop appearing for it.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Optional

OPENF1_BASE = "https://api.openf1.org/v1"
MULTIVIEWER_BASE = "https://api.multiviewer.app/api/v1"
OUTPUT_PATH = Path(__file__).parent.parent / "circuit_corners.json"


def _get_json(url: str) -> Optional[dict | list]:
    # Shells out to curl rather than using `requests`: on some machines
    # (this generator's own author's included) Python's OpenSSL-based TLS
    # stack fails every request to Cloudflare-fronted hosts, while curl
    # (Windows' native schannel TLS) doesn't - see circuit_info.py's
    # docstring for the full story. This script is a maintenance tool run
    # by hand, not the shipped app, so shelling out is a fine trade for
    # "just works" here.
    try:
        result = subprocess.run(
            ["curl", "-s", "-S", "--compressed", url],
            capture_output=True, timeout=30, check=True,
        )
    except Exception as exc:
        print(f"  ! request failed for {url}: {exc}")
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        print(f"  ! bad JSON from {url}")
        return None


def _path_length(xs: list[float], ys: list[float]) -> float:
    total = 0.0
    for i in range(1, len(xs)):
        total += math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1])
    return total


def collect_circuits(start_year: int, end_year: int) -> dict[int, dict]:
    """circuit_key -> {"name": ..., "year": <most recent year seen>}"""
    circuits: dict[int, dict] = {}
    for year in range(start_year, end_year + 1):
        meetings = _get_json(f"{OPENF1_BASE}/meetings?year={year}")
        if not meetings:
            continue
        for meeting in meetings:
            circuit_key = meeting.get("circuit_key")
            if circuit_key is None:
                continue
            # Keep the most recent year we've seen this circuit hosted a
            # meeting in - most likely to reflect its current layout.
            existing = circuits.get(circuit_key)
            if existing is None or year >= existing["year"]:
                circuits[circuit_key] = {
                    "name": meeting.get("circuit_short_name", str(circuit_key)),
                    "year": year,
                }
    return circuits


def fetch_corners(circuit_key: int, year: int) -> Optional[list[dict]]:
    data = _get_json(f"{MULTIVIEWER_BASE}/circuits/{circuit_key}/{year}")
    if not data or "corners" not in data or "x" not in data or "y" not in data:
        return None

    total = _path_length(data["x"], data["y"])
    if total <= 0:
        return None

    corners = []
    for corner in data["corners"]:
        corners.append({
            "number": int(corner["number"]),
            "letter": "",  # MultiViewer's raw feed doesn't split sub-corners
            "fraction": round(corner["length"] / total, 5),
        })
    corners.sort(key=lambda c: c["fraction"])
    return corners


def main() -> None:
    start_year = int(sys.argv[1]) if len(sys.argv) > 1 else 2023
    end_year = int(sys.argv[2]) if len(sys.argv) > 2 else 2026

    print(f"Collecting circuits from OpenF1 meetings, {start_year}-{end_year}...")
    circuits = collect_circuits(start_year, end_year)
    print(f"Found {len(circuits)} distinct circuits.")

    existing: dict[str, list[dict]] = {}
    if OUTPUT_PATH.exists():
        existing = json.loads(OUTPUT_PATH.read_text())

    result = dict(existing)
    for circuit_key, info in sorted(circuits.items()):
        print(f"  circuit_key={circuit_key} ({info['name']}, {info['year']})...", end=" ")
        corners = fetch_corners(circuit_key, info["year"])
        if corners is None:
            print("FAILED - keeping any existing entry")
            continue
        result[str(circuit_key)] = corners
        print(f"OK ({len(corners)} corners)")

    OUTPUT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(result)} circuits to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
