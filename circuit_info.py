"""
Corner-location data for track maps.

OpenF1 has no circuit-layout endpoint of its own, and this used to call
FastF1 (which sources it from MultiViewer) for it instead - but that meant
a live call to api.multiviewer.app, and on top of that host being flaky to
reach from some networks, FastF1 also needed to download and process a
session's full telemetry just to calibrate corner distances into metres.

Both problems go away by not doing this at app runtime at all: corner
positions are static per circuit, so `circuit_corners.json` bakes them in
ahead of time (see scripts/generate_circuit_corners.py) as each corner's
position expressed as a *fraction* of the lap (0-1) rather than an
absolute distance - which sidesteps needing FastF1's metre calibration,
and lets the app just multiply by whatever distance-into-lap it already
computes for the specific lap being plotted.
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA_PATH = Path(__file__).parent / "circuit_corners.json"
_CIRCUITS: dict[str, list[dict]] = json.loads(_DATA_PATH.read_text())


class CircuitInfoError(Exception):
    """Raised when corner data isn't available for a circuit."""


def get_corners(circuit_key: int) -> list[dict]:
    """
    Corner markers - number, letter (for same-numbered corners split into
    sub-corners, when known), and position as a fraction of the lap (0-1) -
    for the given circuit. Multiply `fraction` by a lap's own distance-into
    -lap to place corners on that lap's own distance axis.
    """
    corners = _CIRCUITS.get(str(circuit_key))
    if corners is None:
        raise CircuitInfoError(f"No bundled corner data for circuit_key={circuit_key}")
    return corners
