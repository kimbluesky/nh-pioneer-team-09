"""Price, inventory and exposure rules shared by the dated worker and bar replay."""

import math

FEE = 2.0
MAX_DAY = 300.0
MAX_IDEA = 150.0
TICK = 0.25
TICK_USD = 0.50


MULT = TICK_USD / TICK
ROUND_TRIP = 2 * FEE + 2 * TICK_USD
RISK_RESERVE = 2 * FEE + 4 * TICK_USD


def room(net):
    return max(0.0, min(MAX_IDEA, MAX_DAY + min(net, 0.0)))


def ceil_price(distance):
    return math.ceil(distance / TICK - 1e-8) * TICK


def stop_distance(atr, net, rule):
    # Two round trips of price movement keep a one-tick bounce from defining the stop.
    minimum = 2 * ROUND_TRIP / MULT
    budget = math.floor((room(net) - RISK_RESERVE) / TICK_USD) * TICK
    if budget < minimum:
        return 0.0
    return min(budget, ceil_price(max(minimum, rule["stop_atr"] * atr)))


def entry_side(score, atr, rule):
    if not math.isfinite(score) or not math.isfinite(atr) or atr <= 0 or abs(score) <= rule["cut"]:
        return 0
    if abs(score) * atr * MULT < fee_floor(rule):
        return 0
    return 1 if score > 0 else -1


def fee_floor(rule):
    return {"target": 0.0, "half": ROUND_TRIP * 0.5, "forecast": ROUND_TRIP}[rule["cost_filter"]]


def pullback_level(rule, last, atr, side):
    depth = rule.get("pullback_atr", 0.0)
    return last - side * ceil_price(atr * depth) if depth > 0 else None


def component_key(rule):
    return str(rule["horizon"]) + "_" + rule["model"]


def route(rule, scores, atr):
    best, strength = None, -1.0
    for leg in rule["components"]:
        key = component_key(leg)
        value = scores[key]
        if entry_side(value, atr, leg) and abs(value) / leg["cut"] > strength:
            best, strength = leg, abs(value) / leg["cut"]
    return best


def add_price_ok(rule, pos, price, net):
    move = pos["side"] * (price - pos["first"])
    return (
        pos["maxq"] == 2
        and not pos["added"]
        and len(pos["lots"]) == 1
        and rule["add_r"] * pos["r"] <= move < pos["partial_distance"]
        and move * MULT + 2 * RISK_RESERVE <= min(pos["budget"], room(net))
    )


def add_stop(pos, net):
    side = pos["side"]
    q = len(pos["lots"])
    budget = min(pos["budget"], room(net))
    cap = sum(pos["lots"]) / q - side * (budget - q * RISK_RESERVE) / (q * MULT)
    guarded = (math.ceil(cap / TICK - 1e-8) if side == 1 else math.floor(cap / TICK + 1e-8)) * TICK
    return max(pos["first"], guarded) if side == 1 else min(pos["first"], guarded)


def forecast_action(rule, pos, score, liquidation_net):
    strength = pos["side"] * score
    if strength < -rule["cut"]:
        return "opposite_signal"
    if len(pos["lots"]) == 2 and liquidation_net > 0 and strength <= 0.2 * rule["cut"]:
        return "model_reduce"
    if len(pos["lots"]) == 1 and liquidation_net > 0 and strength <= 0:
        return "profit_edge_faded"
    return None


def trail_after_partial(rule, pos, completed_close):
    if pos["added"] and len(pos["lots"]) == 1:
        side = pos["side"]
        candidate = completed_close - side * ceil_price(rule["trail_r"] * pos["r"])
        if side * (candidate - pos["stop"]) > 0:
            return candidate
    return pos["stop"]
