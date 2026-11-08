/**
 * Team ordering for the driver list: teams in the order they finished the
 * *previous* season's Constructors' Championship.
 *
 * This is a small bundled table rather than a live lookup, so it's instant
 * and needs no extra network call. Add a line to
 * FINAL_CONSTRUCTORS_STANDINGS after each season ends; until then, a
 * season with no entry for its previous year falls back to the most
 * recent season that has one.
 *
 * Teams get renamed (AlphaTauri -> RB -> Racing Bulls, Alfa Romeo -> Kick
 * Sauber -> Audi, ...), so standings are stored per team *lineage* and
 * OpenF1's team_name for any given season is mapped onto it via
 * TEAM_NAME_ALIASES.
 */

// Season -> final Constructors' Championship order, by team lineage.
const FINAL_CONSTRUCTORS_STANDINGS = {
  2022: ["red_bull", "ferrari", "mercedes", "alpine", "mclaren",
         "sauber", "aston_martin", "haas", "racing_bulls", "williams"],
  2023: ["red_bull", "mercedes", "ferrari", "mclaren", "aston_martin",
         "alpine", "williams", "racing_bulls", "sauber", "haas"],
  2024: ["mclaren", "ferrari", "red_bull", "mercedes", "aston_martin",
         "alpine", "haas", "racing_bulls", "williams", "sauber"],
  2025: ["mclaren", "mercedes", "red_bull", "ferrari", "williams",
         "racing_bulls", "aston_martin", "haas", "sauber", "alpine"],
};

// OpenF1 team_name (lower-cased) -> team lineage.
const TEAM_NAME_ALIASES = {
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
};

/** The previous season's final standings, or the most recent earlier
 * season's if that one hasn't been added yet. */
function standingsFor(season) {
  const known = Object.keys(FINAL_CONSTRUCTORS_STANDINGS)
    .map(Number)
    .filter((year) => year < season);
  return known.length ? FINAL_CONSTRUCTORS_STANDINGS[Math.max(...known)] : [];
}

/**
 * Sort key [rank, name] placing `teamName` by where its team finished in
 * the Constructors' Championship the season before `season`. Teams that
 * didn't take part that season (e.g. a brand-new entry), or whose name
 * isn't recognised, go after all the ranked teams, alphabetically.
 */
export function teamSortKey(teamName, season) {
  const name = (teamName || "").toLowerCase();
  const standings = season ? standingsFor(season) : [];
  const rank = standings.indexOf(TEAM_NAME_ALIASES[name]);
  if (rank !== -1) return [rank, name];
  return [standings.length, name || "~"]; // "~" sorts a missing team name last
}

/** Comparator for two [number, string, ...] sort keys. */
export function compareKeys(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}
