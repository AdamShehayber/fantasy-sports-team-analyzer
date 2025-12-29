from typing import Optional, List, Dict
import requests
from datetime import datetime



from .scoring import get_avg_receptions_by_position, SCORING_PRESETS

# Baseline projections per position (approximate, unweighted)
POSITION_BASELINES = {
    "QB": 20.0,
    "RB": 15.0,
    "WR": 14.0,
    "TE": 9.0,
    "FLEX": 12.0,
    "K": 8.0,
    "DEF": 7.0,
    "D/ST": 7.0,
}

# Team adjustments (mock)
TEAM_ADJUSTMENTS = {
    "KC": 1.0,
    "SF": 0.8,
    "DAL": 0.5,
    "BUF": 0.6,
    "PHI": 0.6,
    "BAL": 0.5,
}

def _stable_variation(name: str) -> float:
    """Produce a small deterministic bump based on the player's name."""
    if not name:
        return 0.0
    # Map hash to range [-1.0, +1.0]
    h = abs(hash(name)) % 1000
    return (h / 999.0) * 2.0 - 1.0

def project_player(name: str, position: str, team: Optional[str], scoring_type: str = "PPR") -> float:
    """
    Return a base per-game projection for a player (unweighted).
    This is a deterministic mock that considers position baseline, team, and scoring preset.
    """
    pos = (position or "UNK").upper()
    base = POSITION_BASELINES.get(pos, 10.0)
    team_adj = TEAM_ADJUSTMENTS.get((team or "").upper(), 0.0)
    name_adj = _stable_variation(name) * 1.2  # up to ~±1.2 points

    # Apply a small boost based on scoring preset (reception bonus proxy)
    preset = SCORING_PRESETS.get(scoring_type, SCORING_PRESETS["PPR"])
    reception_bonus = preset.get("reception_bonus", 1.0)
    bonus = get_avg_receptions_by_position(pos) * reception_bonus * 0.15  # modest effect on base

    projection = base + team_adj + name_adj + bonus
    return max(0.0, round(float(projection), 2))

# ------------------------------
# External API adapter (Sleeper)
# ------------------------------

SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

FALLBACK_CATALOG: Dict[str, Dict] = {
    # Minimal offline catalog with common players for testing/demo
    "4034": {"player_id": "4034", "full_name": "Travis Kelce", "position": "TE", "team": "KC"},
    "6884": {"player_id": "6884", "full_name": "Patrick Mahomes", "position": "QB", "team": "KC"},
    "5863": {"player_id": "5863", "full_name": "Josh Allen", "position": "QB", "team": "BUF"},
    "4110": {"player_id": "4110", "full_name": "Stefon Diggs", "position": "WR", "team": "BUF"},
    "4046": {"player_id": "4046", "full_name": "Christian McCaffrey", "position": "RB", "team": "SF"},
    "4038": {"player_id": "4038", "full_name": "Derrick Henry", "position": "RB", "team": "TEN"},
    "6799": {"player_id": "6799", "full_name": "Justin Jefferson", "position": "WR", "team": "MIN"},
    "5841": {"player_id": "5841", "full_name": "Tyreek Hill", "position": "WR", "team": "MIA"},
    "6786": {"player_id": "6786", "full_name": "Ja'Marr Chase", "position": "WR", "team": "CIN"},
    "5890": {"player_id": "5890", "full_name": "Jalen Hurts", "position": "QB", "team": "PHI"},
    "5848": {"player_id": "5848", "full_name": "Lamar Jackson", "position": "QB", "team": "BAL"},
    "4037": {"player_id": "4037", "full_name": "Davante Adams", "position": "WR", "team": "LV"},
    "4031": {"player_id": "4031", "full_name": "Cooper Kupp", "position": "WR", "team": "LAR"},
    "5840": {"player_id": "5840", "full_name": "Joe Burrow", "position": "QB", "team": "CIN"},
    "6781": {"player_id": "6781", "full_name": "Mark Andrews", "position": "TE", "team": "BAL"},
}

def _fetch_sleeper_players(timeout_sec: int = 5) -> Dict[str, Dict]:
    """Fetch the Sleeper players catalog (large). Returns dict keyed by player_id.
    Falls back to a small local catalog when the external API is unavailable,
    to ensure the app remains functional offline.
    """
    data: Dict[str, Dict] = {}
    try:
        resp = requests.get(SLEEPER_PLAYERS_URL, timeout=timeout_sec)
        resp.raise_for_status()
        raw = resp.json()
        if isinstance(raw, dict):
            data = raw
        else:
            # Some mirrors return list; normalize into dict keyed by player_id
            for item in (raw or []):
                pid = str(item.get("player_id"))
                if pid:
                    data[pid] = item
    except Exception:
        # Network error: fall back to local minimal catalog
        data = {}
    # Use fallback catalog if external source failed or returned empty
    if not data:
        return FALLBACK_CATALOG.copy()
    return data
#109
def search_players(query: str, team: Optional[str], position: Optional[str], season: int, week: Optional[int], scoring_type: str = "PPR", limit: int = 20, catalog: Optional[Dict[str, Dict]] = None) -> List[Dict]:
    """Search players by name/team/position and attach a simple projection. 
    Uses Sleeper players catalog. Falls back to empty list on failure.
    If a pre-fetched `catalog` is provided, it will be used to avoid
    repeated external API calls.
    """
    q = (query or "").strip().lower()
    t = (team or "").strip().upper()
    p = (position or "").strip().upper()

    
    catalog = catalog if catalog is not None else _fetch_sleeper_players()
    results: List[Dict] = []
    if not catalog:
        return results

    # Sleeper format fields
    for pid, info in catalog.items():
        full_name = (info.get("full_name") or info.get("first_name") or "") + (" " + (info.get("last_name") or "") if info.get("last_name") else "")
        pos = (info.get("position") or "").upper()
        team_code = (info.get("team") or "").upper()

        if q and full_name.lower().find(q) == -1:
            continue
        if t and team_code != t:
            continue
        if p and pos != p:
            continue

        proj = project_player(full_name, pos, team_code, scoring_type=scoring_type)
        results.append({
            "player_id": pid,
            "full_name": full_name.strip() or info.get("full_name") or "Unknown",
            "position": pos or "",
            "team": team_code or "",
            "season": season,
            "week": week,
            "projection_points": proj,
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "source": "live",
        })

        if len(results) >= limit:
            break

    return results
