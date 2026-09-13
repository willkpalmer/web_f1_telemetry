"""
Thin wrapper around the OpenF1 REST API.

Historical data (2023 onwards) is free and needs no authentication.
Docs: https://openf1.org/docs/
"""

from __future__ import annotations

from typing import Any, Optional

import requests

BASE_URL = "https://api.openf1.org/v1"
REQUEST_TIMEOUT = 20  # seconds


class OpenF1Error(Exception):
    """Raised when a request to the OpenF1 API fails or returns bad data."""


def _get(endpoint: str, **params: Any) -> list[dict]:
    """
    GET an OpenF1 endpoint and return the decoded JSON list.

    Keyword arguments become query-string filters, e.g.
    _get("laps", session_key=9161, driver_number=63).
    """
    url = f"{BASE_URL}/{endpoint}"
    # Drop params the caller left as None so we don't send "key=None".
    clean_params = {k: v for k, v in params.items() if v is not None}

    try:
        response = requests.get(url, params=clean_params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise OpenF1Error(f"Request to '{endpoint}' failed: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise OpenF1Error(f"Could not decode JSON from '{endpoint}'") from exc

    if not isinstance(data, list):
        raise OpenF1Error(f"Unexpected response shape from '{endpoint}'")

    return data


def get_meetings(year: int) -> list[dict]:
    """All Grand Prix / testing meetings for a given year, unsorted."""
    return _get("meetings", year=year)


def get_sessions(meeting_key: int) -> list[dict]:
    """All sessions (Practice, Qualifying, Race, ...) within a meeting."""
    return _get("sessions", meeting_key=meeting_key)


def get_drivers(session_key: int) -> list[dict]:
    """Driver/car info (name, number, team, team colour) for a session."""
    return _get("drivers", session_key=session_key)


def get_laps(session_key: int, driver_number: Optional[int] = None) -> list[dict]:
    """
    Lap-by-lap timing data for a session.

    Fetching without a driver_number returns every driver's laps in one
    request, which is far cheaper than one request per driver.
    """
    return _get("laps", session_key=session_key, driver_number=driver_number)


def get_stints(session_key: int) -> list[dict]:
    """Tyre stint info (compound, lap range, tyre age) for a session."""
    return _get("stints", session_key=session_key)
