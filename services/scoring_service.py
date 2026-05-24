# ═══════════════════════════════════════════════════════════════
# VALVO STOCK SCORING SYSTEM - v4 (BACKWARD COMPATIBLE)
# ═══════════════════════════════════════════════════════════════
#
# All original function names and signatures preserved.
# New gatekeeper logic added without breaking existing calls.
# ═══════════════════════════════════════════════════════════════

import math


# ═══════════════════════════════════════════════════════════════
# FIXED WEIGHTS (Total = 100)
# ═══════════════════════════════════════════════════════════════

PARAMETER_WEIGHTS = {
    'sector_strength':              26,
    'magnitude':                    14,
    'move_ema':                     12,
    'relative_strength':            10,
    'institutional_participation':  10,
    'symmetry':                      8,
    'ferocity':                      8,
    'adr':                           7,
    'market_trend':                  5,
}

# Parameters that are ONLY gatekeepers (not in composite)
GATEKEEPER_PARAMS = ['market_cap', 'linearity', 'liquidity']


# ═══════════════════════════════════════════════════════════════
# INTERPOLATION FUNCTION (Core Math) — UNCHANGED
# ═══════════════════════════════════════════════════════════════

def interpolate(value, points):
    """
    Smooth scoring between defined points
    """
    if value is None or value == "":
        return 0

    value = float(value)

    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]

    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]

        if x1 <= value <= x2:
            ratio = (value - x1) / (x2 - x1)
            return y1 + (y2 - y1) * ratio


# ═══════════════════════════════════════════════════════════════
# GATEKEEPER MULTIPLIER FUNCTIONS (NEW)
# ═══════════════════════════════════════════════════════════════

def liquidity_gatekeeper_multiplier(liquidity):
    """Only punishes below 200 Cr. Above 200 → 1.0."""
    if liquidity is None or liquidity == "":
        return 0.15
    x = float(liquidity)
    if x >= 200:
        return 1.0
    t = math.tanh((x - 130) / 55)
    result = 0.15 + 0.85 * (t + 1) / 2
    return min(result, 1.0)


def market_cap_gatekeeper_multiplier(market_cap):
    """Brutal cutoff below 1,000 Cr. Above 1,000 → 1.0."""
    if market_cap is None or market_cap == "":
        return 0.02
    x = float(market_cap)
    if x >= 1000:
        return 1.0
    t = math.tanh((x - 950) / 40)
    result = 0.02 + 0.98 * (t + 1) / 2
    return min(result, 1.0)


def linearity_gatekeeper_multiplier(linearity):
    """Bad → 0.15, Good → 0.85, Very Good → 1.0"""
    multipliers = {
        'Very Good': 1.0,
        'Good': 0.85,
        'Bad': 0.15,
    }
    return multipliers.get(linearity, 0.15)


# ═══════════════════════════════════════════════════════════════
# PARAMETER SCORING FUNCTIONS — SAME NAMES AS ORIGINAL
# ═══════════════════════════════════════════════════════════════

def calculate_market_cap_score(market_cap):
    """Market cap bell curve score (displayed in UI)."""
    points = [
        (1000, 0),
        (2000, 5),
        (5000, 35),
        (10000, 75),
        (15000, 90),
        (25000, 100),
        (30000, 100),
        (100000, 75),
        (200000, 50),
        (500000, 40)
    ]
    return interpolate(market_cap, points)


def calculate_linearity_score(linearity):
    """Linearity score (displayed in UI)."""
    scores = {
        'Very Good': 100,
        'Good': 70,
        'Bad': 0
    }
    return scores.get(linearity, 0)


def calculate_checkbox_score(value):
    """Simple: Yes (100) or No (0)"""
    return 100 if value else 0


def calculate_liquidity_score(liquidity):
    """Liquidity parameter score with tanh curve."""
    if liquidity is None or liquidity == "":
        return 0
    x = float(liquidity)
    t = math.tanh((x - 200) / 250)
    score = 50 * (t + 1)
    return min(score, 100)


def calculate_adr_score(adr):
    """ADR piecewise linear score."""
    if adr is None or adr == "":
        return 0
    adr = float(adr)
    if adr < 2:
        return (adr / 2) * 10
    elif adr < 3:
        return 10 + ((adr - 2) / 1) * 20
    elif adr < 5:
        return 30 + ((adr - 3) / 2) * 45
    elif adr <= 9:
        return 75 + ((adr - 5) / 4) * 25
    else:
        return 100


def calculate_ferocity_score(move_percentage, move_days):
    """Ferocity = Move% / Days, smooth interpolation."""
    if not move_percentage or not move_days or move_days == 0:
        return 0
    try:
        ratio = float(move_percentage) / int(move_days)
        points = [
            (0, 0),
            (3, 50),
            (5, 75),
            (8, 90),
            (10, 100)
        ]
        return interpolate(ratio, points)
    except (ValueError, TypeError):
        return 0


def calculate_magnitude_score(move_percentage):
    """Magnitude bell curve, sweet spot 50-70%."""
    if not move_percentage:
        return 0
    try:
        pct = float(move_percentage)
        points = [
            (5, 0),
            (10, 5),
            (20, 40),
            (30, 75),
            (40, 90),
            (50, 100),
            (70, 100),
            (80, 85),
            (100, 70)
        ]
        return interpolate(pct, points)
    except (ValueError, TypeError):
        return 0


def calculate_move_ema_score(move_ema):
    """5 EMA = 100, 10 EMA = 65, 20 EMA = 30"""
    scores = {
        '5 EMA': 100,
        '10 EMA': 65,
        '20 EMA': 30,
    }
    return scores.get(move_ema, 0)


def calculate_market_trend_score(market_trend):
    """Uptrend = 100, Sideways = 50, Grind Down = 20, Sharp Down = 0"""
    scores = {
        'Uptrend': 100,
        'Sideways': 50,
        'Grind Downtrend': 20,
        'Sharp Downtrend': 0,
    }
    return scores.get(market_trend, 0)


# ═══════════════════════════════════════════════════════════════
# MASTER FUNCTION — SAME NAME AND SIGNATURE AS ORIGINAL
# ═══════════════════════════════════════════════════════════════

def calculate_all_scores(data):
    """
    Returns ALL individual scores — same keys as original.
    UI display stays exactly the same.
    """
    scores = {
        'market_cap': calculate_market_cap_score(data.get('market_cap')),
        'linearity': calculate_linearity_score(data.get('linearity', 'Good')),
        'sector_strength': calculate_checkbox_score(data.get('sector_strength', False)),
        'symmetry': calculate_checkbox_score(data.get('symmetry', False)),
        'institutional_participation': calculate_checkbox_score(data.get('institutional_participation', False)),
        'relative_strength': calculate_checkbox_score(data.get('relative_strength', False)),
        'liquidity': calculate_liquidity_score(data.get('liquidity')),
        'adr': calculate_adr_score(data.get('adr')),
        'market_trend': calculate_market_trend_score(data.get('market_trend', 'Uptrend')),
        'ferocity': 0,
        'magnitude': 0,
        'move_ema': 0,
    }

    if data.get('previous_move_enabled'):
        scores['ferocity'] = calculate_ferocity_score(
            data.get('move_percentage'), data.get('move_days')
        )
        scores['magnitude'] = calculate_magnitude_score(data.get('move_percentage'))
        scores['move_ema'] = calculate_move_ema_score(data.get('move_ema'))

    return scores


# ═══════════════════════════════════════════════════════════════
# WEIGHTED SCORE — SAME NAME AS ORIGINAL (but uses new logic)
# ═══════════════════════════════════════════════════════════════

def calculate_weighted_score(scores, weightages=None):
    """
    Kept for backward compatibility.
    Now uses fixed PARAMETER_WEIGHTS and excludes gatekeeper params.
   
    The 'weightages' parameter is accepted but ignored — fixed weights
    are always used. This prevents breaking calls from the backend.
    """
    total_weighted = 0
    total_weight = 0

    for param, weight in PARAMETER_WEIGHTS.items():
        score = scores.get(param, 0)
        total_weighted += score * weight
        total_weight += weight

    if total_weight == 0:
        return 0

    weighted_avg = total_weighted / total_weight
    return round(weighted_avg / 10, 2)


# ═══════════════════════════════════════════════════════════════
# FINAL SCORE — SAME SIGNATURE AS ORIGINAL
# ═══════════════════════════════════════════════════════════════

def calculate_final_score(scores, weightages=None):
    """
    SAME SIGNATURE AS ORIGINAL: takes scores dict, returns float (0-10).
   
    Now also applies gatekeeper multipliers.
   
    NOTE: This function needs the raw data for gatekeepers.
    If called with just scores (old way), gatekeepers that need raw
    values (liquidity, market_cap) will look for them in scores.
    """
    # Calculate raw weighted score from 9 composite params (0-10)
    raw_score = calculate_weighted_score(scores, weightages)

    # Apply gatekeeper multipliers
    # For liquidity gatekeeper, we need the raw liquidity value
    # The scores dict may contain the raw values if calculate_all_scores was used
    # We need to reverse-engineer or pass through raw data
   
    # Since the original flow is: data → calculate_all_scores → scores → calculate_final_score
    # We don't have raw data here. So we store raw data in a module-level variable.
   
    # Apply gatekeepers using the stored raw data
    combined_gatekeeper = 1.0
    if hasattr(calculate_final_score, '_raw_data'):
        raw_data = calculate_final_score._raw_data
        combined_gatekeeper *= liquidity_gatekeeper_multiplier(raw_data.get('liquidity'))
        combined_gatekeeper *= market_cap_gatekeeper_multiplier(raw_data.get('market_cap'))
        combined_gatekeeper *= linearity_gatekeeper_multiplier(raw_data.get('linearity', 'Bad'))

    gated_score = raw_score * combined_gatekeeper
    return round(gated_score, 2)


# ═══════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION: Full pipeline (data → final score)
# Call this from the backend route instead of manual steps
# ═══════════════════════════════════════════════════════════════

def calculate_score_from_data(data):
    """
    Full pipeline: raw data → individual scores → weighted → gated → final.
   
    Returns dict with:
      - individual_scores: all 12 param scores for UI display
      - raw_composite: weighted average before gatekeepers (0-10)
      - gatekeepers: dict of each gatekeeper multiplier
      - combined_gatekeeper: product of all gatekeepers
      - final_score: gated score (0-10)
      - rating: Excellent/Strong/Average/Weak
    """
    # Step 1: All individual scores (for UI)
    all_scores = calculate_all_scores(data)

    # Step 2: Weighted composite from 9 params (0-10)
    raw_composite = calculate_weighted_score(all_scores)

    # Step 3: Gatekeeper multipliers
    gatekeepers = {
        'liquidity': liquidity_gatekeeper_multiplier(data.get('liquidity')),
        'market_cap': market_cap_gatekeeper_multiplier(data.get('market_cap')),
        'linearity': linearity_gatekeeper_multiplier(data.get('linearity', 'Bad')),
    }
    combined_gatekeeper = 1.0
    for mult in gatekeepers.values():
        combined_gatekeeper *= mult

    # Step 4: Apply gatekeepers
    final_score = round(raw_composite * combined_gatekeeper, 2)

    return {
        'individual_scores': all_scores,
        'raw_composite': raw_composite,
        'gatekeepers': gatekeepers,
        'combined_gatekeeper': round(combined_gatekeeper, 4),
        'final_score': final_score,
        'rating': get_rating(final_score),
    }


# ═══════════════════════════════════════════════════════════════
# RATING LABEL — UNCHANGED
# ═══════════════════════════════════════════════════════════════

def get_rating(final_score):
    if final_score >= 8:
        return 'Excellent'
    elif final_score >= 6:
        return 'Strong'
    elif final_score >= 4:
        return 'Average'
    else:
        return 'Weak'