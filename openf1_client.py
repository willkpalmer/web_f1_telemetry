"""
Thin wrapper around the OpenF1 REST API.

Historical data (2023 onwards) is free and needs no authentication.
Docs: https://openf1.org/docs/
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

import requests

BASE_URL = "https://api.openf1.org/v1"
REQUEST_TIMEOUT = 20  # seconds


class OpenF1Error(Exception):
    """Raised when a request to the OpenF1 API fails or returns bad data."""


def _get(endpoint: str, extra_query: Optional[str] = None, **params: Any) -> list[dict]:
    """
    GET an OpenF1 endpoint and return the decoded JSON list.

    Keyword arguments become query-string filters, e.g.
    _get("laps", session_key=9161, driver_number=63).

    `extra_query` is appended to the URL verbatim (unencoded) for OpenF1's
    comparison-operator filters, e.g. "date>=...&date<=...": their API
    expects a literal '>'/'<' in the query string, which requests' own
    `params=` encoding would otherwise percent-escape into a filter it
    doesn't recognise.
    """
    # Drop params the caller left as None so we don't send "key=None".
    clean_params = {k: v for k, v in params.items() if v is not None}

    try:
        if extra_query:
            query = urlencode(clean_params)
            sep = "&" if query else ""
            url = f"{BASE_URL}/{endpoint}?{query}{sep}{extra_query}"
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
        else:
            url = f"{BASE_URL}/{endpoint}"
            response = requests.get(url, params=clean_params, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            # OpenF1 responds 404 ("No results found") for a query that
            # legitimately matches nothing, rather than an empty 200 list.
            return []
        if response.status_code == 429:
            raise OpenF1Error(
                f"OpenF1 is rate-limiting requests to '{endpoint}' "
                "(too many requests) - wait a few seconds and try again."
            )
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


def get_car_data(
    session_key: int, driver_number: int, date_from: str, date_to: str
) -> list[dict]:
    """
    High-frequency (~3.7 Hz) telemetry for one driver within a time window:
    speed, rpm, throttle, brake, n_gear, drs. `date_from`/`date_to` are ISO
    timestamps (e.g. a lap's date_start and date_start + lap_duration) -
    OpenF1 refuses to hand back a whole session's data in one request, so a
    tight time window is required.
    """
    return _get(
        "car_data",
        extra_query=f"date>={date_from}&date<={date_to}",
        session_key=session_key,
        driver_number=driver_number,
    )
