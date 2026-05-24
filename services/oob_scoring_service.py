# ═══════════════════════════════════════════════════════════════
# VALVO — OOB SCORING ENGINE  v5
# Out of Base Setup
# ═══════════════════════════════════════════════════════════════

import math


PARAMETER_WEIGHTS = {
    'sector_strength':              26,
    'move_quality':                 20,
    'move_ema':                     12,
    'market_trend':                 12,
    'relative_strength':            10,
    'institutional_participation':  10,
    'adr':                           7,
    'extension_base':                3,
}

GATEKEEPER_PARAMS = ['linearity', 'liquidity', 'market_cap', 'extension']


def interpolate(value, points):
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
            return y1 + (y2 - y1) * (value - x1) / (x2 - x1)
    return 0


# ── GATEKEEPERS ──

def linearity_gatekeeper_multiplier(linearity):
    return {'Very Good': 1.0, 'Good': 0.85, 'Bad': 0.15}.get(linearity, 0.15)

def liquidity_gatekeeper_multiplier(liquidity):
    if liquidity is None or liquidity == "":
        return 0.15
    x = float(liquidity)
    if x >= 200:
        return 1.0
    t = math.tanh((x - 130) / 55)
    return round(min(0.15 + 0.85 * (t + 1) / 2, 1.0), 4)

def market_cap_gatekeeper_multiplier(market_cap):
    if market_cap is None or market_cap == "":
        return 0.02
    x = float(market_cap)
    if x >= 1000:
        return 1.0
    t = math.tanh((x - 950) / 40)
    return round(min(0.02 + 0.98 * (t + 1) / 2, 1.0), 4)

def extension_gatekeeper_multiplier(extension_pct):
    if extension_pct is None or extension_pct == "":
        return 1.0
    x = float(extension_pct)
    if x <= 80:
        return 1.0
    drop = min(x - 80, 40) / 40
    return round(max(1.0 - 0.40 * drop, 0.60), 4)


# ── MOVE QUALITY (merged ferocity + magnitude) ──

def calculate_ferocity_score(move_percentage, move_days):
    if not move_percentage or not move_days:
        return 0
    try:
        ratio = float(move_percentage) / int(move_days)
        if ratio <= 0:
            return 0
        points = [
            (0, 0), (0.5, 5), (1.0, 15), (1.5, 28), (2.0, 45),
            (2.5, 58), (3.0, 68), (4.0, 80), (5.0, 88),
            (6.0, 93), (8.0, 97), (10, 100),
        ]
        return interpolate(ratio, points)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0

def calculate_magnitude_score(move_percentage):
    if not move_percentage:
        return 0
    try:
        pct = float(move_percentage)
        points = [
            (5, 0), (10, 5), (15, 20), (20, 40), (30, 75),
            (40, 90), (50, 100), (70, 100), (80, 95),
            (100, 85), (115, 60),
        ]
        return interpolate(pct, points)
    except (ValueError, TypeError):
        return 0

def calculate_move_quality_score(move_percentage, move_days):
    ferocity  = calculate_ferocity_score(move_percentage, move_days)
    magnitude = calculate_magnitude_score(move_percentage)
    mq = ferocity * 0.70 + magnitude * 0.30
    try:
        if float(move_percentage) < 10:
            mq = min(mq, 40)
    except (ValueError, TypeError):
        pass
    return round(mq, 2)


# ── OTHER PARAMETER SCORES ──

def calculate_move_ema_score(move_ema):
    return {'5 EMA': 100, '10 EMA': 65, '20 EMA': 30}.get(move_ema, 0)

def calculate_market_trend_score(market_trend):
    return {
        'Uptrend': 100, 'Sideways': 50,
        'Grind Downtrend': 20, 'Sharp Downtrend': 0,
    }.get(market_trend, 0)

def calculate_checkbox_score(value):
    return 100 if value else 0

def calculate_adr_score(adr):
    if adr is None or adr == "":
        return 0
    points = [
        (0, 0), (2, 5), (3, 15), (4, 35), (5.5, 75),
        (7, 100), (9, 100), (9.5, 97), (11, 80), (13, 60), (15, 40),
    ]
    return interpolate(adr, points)

def calculate_extension_base_score(extension_pct):
    if extension_pct is None or extension_pct == "":
        return 0
    points = [
        (0, 0), (15, 10), (25, 50), (30, 80), (40, 100),
        (55, 100), (65, 80), (75, 55), (80, 40), (90, 15), (120, 0),
    ]
    return interpolate(extension_pct, points)


# ── Display-only scores ──

def calculate_market_cap_score(market_cap):
    if market_cap is None or market_cap == "":
        return 0
    points = [
        (0, 0), (500, 10), (1000, 25), (2000, 50),
        (5000, 75), (10000, 90), (25000, 100), (30000, 100),
        (100000, 80), (200000, 60),
    ]
    return interpolate(market_cap, points)

def calculate_linearity_score(linearity):
    return {'Very Good': 100, 'Good': 70, 'Bad': 0}.get(linearity, 0)

def calculate_liquidity_score(liquidity):
    if liquidity is None or liquidity == "":
        return 0
    x = float(liquidity)
    t = math.tanh((x - 200) / 250)
    return round(min(50 * (t + 1), 100), 2)


# ── MASTER PIPELINE ──

def calculate_all_scores(data):
    move_pct  = data.get('move_percentage')
    move_days = data.get('move_days')
    ext_pct   = data.get('extension_pct')

    return {
        'sector_strength':              calculate_checkbox_score(data.get('sector_strength', False)),
        'move_quality':                 calculate_move_quality_score(move_pct, move_days),
        'move_ema':                     calculate_move_ema_score(data.get('move_ema', '5 EMA')),
        'market_trend':                 calculate_market_trend_score(data.get('market_trend', 'Uptrend')),
        'relative_strength':            calculate_checkbox_score(data.get('relative_strength', False)),
        'institutional_participation':  calculate_checkbox_score(data.get('institutional_participation', False)),
        'adr':                          calculate_adr_score(data.get('adr')),
        'extension_base':               calculate_extension_base_score(ext_pct),
        'market_cap':  calculate_market_cap_score(data.get('market_cap')),
        'linearity':   calculate_linearity_score(data.get('linearity', 'Good')),
        'liquidity':   calculate_liquidity_score(data.get('liquidity')),
        'ferocity':    calculate_ferocity_score(move_pct, move_days),
        'magnitude':   calculate_magnitude_score(move_pct),
    }

def calculate_weighted_score(scores):
    total_weighted = sum(scores.get(p, 0) * w for p, w in PARAMETER_WEIGHTS.items())
    total_weight   = sum(PARAMETER_WEIGHTS.values())
    if total_weight == 0:
        return 0
    return round((total_weighted / total_weight) / 10, 4)

def calculate_score_from_data(data):
    all_scores = calculate_all_scores(data)
    raw_composite = calculate_weighted_score(all_scores)

    ext_pct = data.get('extension_pct')
    gatekeepers = {
        'linearity':  linearity_gatekeeper_multiplier(data.get('linearity', 'Bad')),
        'liquidity':  liquidity_gatekeeper_multiplier(data.get('liquidity')),
        'market_cap': market_cap_gatekeeper_multiplier(data.get('market_cap')),
        'extension':  extension_gatekeeper_multiplier(ext_pct),
    }
    combined_gatekeeper = round(
        gatekeepers['linearity'] *
        gatekeepers['liquidity'] *
        gatekeepers['market_cap'] *
        gatekeepers['extension'], 4
    )

    final_score = round(raw_composite * combined_gatekeeper, 2)

    try:
        ratio = round(float(data.get('move_percentage', 0) or 0) /
                      max(int(data.get('move_days', 1) or 1), 1), 2)
    except (ValueError, TypeError):
        ratio = 0

    return {
        'individual_scores':   all_scores,
        'ferocity_ratio':      ratio,
        'raw_composite':       round(raw_composite, 4),
        'gatekeepers':         {k: round(v, 4) for k, v in gatekeepers.items()},
        'combined_gatekeeper': combined_gatekeeper,
        'final_score':         final_score,
        'rating':              get_rating(final_score),
    }

def get_rating(final_score):
    if final_score >= 8:   return 'Excellent'
    elif final_score >= 6: return 'Strong'
    elif final_score >= 4: return 'Average'
    else:                  return 'Weak'
