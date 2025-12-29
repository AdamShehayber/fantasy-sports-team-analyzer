from typing import List, Dict, Tuple


POSITION_WEIGHTS = {
    "QB": 1.00,
    "RB": 1.08,
    "WR": 1.08,
    "TE": 1.00,
    "FLEX": 1.00,
    "K": 0.45,
    "DEF": 0.65,
    "D/ST": 0.65  # Alternative naming for defense
}

# Position limits for starters
POSITION_LIMITS = {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "FLEX": 1,
    "K": 1,
    "DEF": 1,
    "D/ST": 1
}

# Scoring presets
SCORING_PRESETS = {
    "Standard": {"reception_bonus": 0.0},
    "Half-PPR": {"reception_bonus": 0.5},
    "PPR": {"reception_bonus": 1.0}
}

def calculate_player_score(player: Dict, scoring_type: str = "PPR") -> float:
    """Calculate individual player score with scoring preset bonuses"""
    projection = float(player.get("projection", 0) or 0)
    position = (player.get("position") or "UNK").upper()
    
    # Apply position weight
    weight = POSITION_WEIGHTS.get(position, 1.0)
    base_score = projection * weight
    
    # Apply scoring preset 
    preset = SCORING_PRESETS.get(scoring_type, SCORING_PRESETS["PPR"])
    reception_bonus = preset["reception_bonus"]
    
    
    avg_receptions = get_avg_receptions_by_position(position)
    bonus_score = avg_receptions * reception_bonus
    
    return base_score + bonus_score

def get_avg_receptions_by_position(position: str) -> float:
    """Estimate average receptions by position (placeholder until Week 4 API)"""
    reception_estimates = {
        "QB": 0,
        "RB": 3.5,
        "WR": 6.0,
        "TE": 4.5,
        "FLEX": 5.0,  # Average between RB/WR/TE
        "K": 0,
        "DEF": 0,
        "D/ST": 0
    }
    return reception_estimates.get(position, 0)

def team_strength_v3(players: List[Dict], scoring_type: str = "PPR") -> Tuple[float, float]: #67
    """
    Calculate separate starter and bench team strength
    Returns: (starter_total, bench_total)
    """
    starter_total = 0.0
    bench_total = 0.0
    
    for player in players:
        score = calculate_player_score(player, scoring_type)
        is_starter = player.get("is_starter", True)
        
        if is_starter:
            starter_total += score
        else:
            bench_total += score
    
    return starter_total, bench_total

def validate_lineup(players: List[Dict]) -> Dict[str, any]:
    """
    Validate lineup against position limits for starters only
    Returns validation result with details
    """
    starter_counts = {}
    violations = []
    
    # Count starters by position
    for player in players:
        if player.get("is_starter", True):
            position = (player.get("position") or "UNK").upper()
            starter_counts[position] = starter_counts.get(position, 0) + 1
    
    # Check against limits
    for position, count in starter_counts.items():
        limit = POSITION_LIMITS.get(position, 1)
        if count > limit:
            violations.append({
                "position": position,
                "current": count,
                "limit": limit,
                "excess": count - limit
            })
    
    return {
        "valid": len(violations) == 0,
        "violations": violations,
        "starter_counts": starter_counts,
        "position_limits": POSITION_LIMITS
    }

def can_add_starter(players: List[Dict], new_position: str) -> bool:
    """Check if a new starter can be added for the given position"""
    validation = validate_lineup(players)
    current_count = validation["starter_counts"].get(new_position.upper(), 0)
    limit = POSITION_LIMITS.get(new_position.upper(), 1)
    return current_count < limit

def position_breakdown_v3(players: List[Dict], scoring_type: str = "PPR") -> Dict[str, Dict[str, float]]:
    """
    Calculate position breakdown with separate starter/bench totals
    Returns nested dict: {position: {"starter": score, "bench": score}}
    """
    breakdown = {}
    
    for player in players:
        position = (player.get("position") or "UNK").upper()
        score = calculate_player_score(player, scoring_type)
        is_starter = player.get("is_starter", True)
        
        if position not in breakdown:
            breakdown[position] = {"starter": 0.0, "bench": 0.0}
        
        if is_starter:
            breakdown[position]["starter"] += score
        else:
            breakdown[position]["bench"] += score
    
    return breakdown

# Legacy functions for backward compatibility
def team_strength(players: List[Dict], scoring: str = "PPR") -> float:
    """Legacy function - returns only starter total for backward compatibility"""
    starter_total, _ = team_strength_v3(players, scoring)
    return starter_total

def position_breakdown(players: List[Dict]) -> Dict[str, float]:
    """Legacy function - returns combined totals for backward compatibility"""
    breakdown_v3 = position_breakdown_v3(players)
    combined = {}
    for position, scores in breakdown_v3.items():
        combined[position] = scores["starter"] + scores["bench"]
    return combined
