"""
Team ordering for the driver list: teams in the order they finished the
*previous* season's Constructors' Championship.

This is a small bundled table rather than a live lookup, so it's instant
and needs no extra network call - the same approach as circuit_info.py.
Add a line to FINAL_CONSTRUCTORS_STANDINGS after each season ends; until
then, a season with no entry for its previous year falls back to the most
recent season that has one.

Teams get renamed (AlphaTauri -> RB -> Racing Bulls, Alfa Romeo -> Kick
Sauber -> Audi, ...), so standings are stored per team *lineage* and
OpenF1's team_name for any given season is mapped onto it via
TEAM_NAME_ALIASES.
"""

from __future__ import annotations

from typing import Optional

# Season -> final Constructors' Championship order, by team lineage.
FINAL_CONSTRUCTORS_STANDINGS: dict[int, list[str]] = {
    2022: ["red_bull", "ferrari", "mercedes", "alpine", "mclaren",
           "sauber", "aston_martin", "haas", "racing_bulls", "williams"],
    2023: ["red_bull", "mercedes", "ferrari", "mclaren", "aston_martin",
           "alpine", "williams", "racing_bulls", "sauber", "haas"],
    2024: ["mclaren", "ferrari", "red_bull", "mercedes", "aston_martin",
           "alpine", "haas", "racing_bulls", "williams", "sauber"],
    2025: ["mclaren", "mercedes", "red_bull", "ferrari", "williams",
           "racing_bulls", "aston_martin", "haas", "sauber", "alpine"],
}

# OpenF1 team_name (casefolded) -> team lineage.
TEAM_NAME_ALIASES: dict[str, str] = {
    "red bull racing": "red_bull",
    "red bull": "red_bull",
    "ferrari": "ferrari",
    "mercedes": "mercedes",
    "mclaren": "mclaren",
    "aston martin": "aston_martin",
    "alpine": "alpine",
    "williams": "williams",
    "haas f1 team": "haas",
    "haas": "haas",
    "alphatauri": "racing_bulls",
    "rb": "racing_bulls",
    "visa cash app rb": "racing_bulls",
    "racing bulls": "racing_bulls",
    "alfa romeo": "sauber",
    "kick sauber": "sauber",
    "sauber": "sauber",
    "audi": "sauber",
    "cadillac": "cadillac",
}


def _standings_for(season: int) -> list[str]:
    """The previous season's final standings, or the most recent earlier
    season's if that one hasn't been added yet."""
    known = [year for year in FINAL_CONSTRUCTORS_STANDINGS if year < season]
    return FINAL_CONSTRUCTORS_STANDINGS[max(known)] if known else []


def team_sort_key(team_name: Optional[str], season: Optional[int]) -> tuple[int, str]:
    """
    Sort key placing `team_name` by where its team finished in the
    Constructors' Championship the season before `season`. Teams that
    didn't take part that season (e.g. a brand-new entry), or whose name
    isn't recognised, go after all the ranked teams, alphabetically.
    """
    name = (team_name or "").casefold()
    standings = _standings_for(season) if season is not None else []
    lineage = TEAM_NAME_ALIASES.get(name)
    if lineage in standings:
        return standings.index(lineage), name
    return len(standings), name or "~"  # "~" sorts a missing team name last
