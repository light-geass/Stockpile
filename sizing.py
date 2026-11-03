"""Position size from a fixed rupee risk budget. Never places an order."""


def shares(entry, stop, risk_budget):
    """Whole shares such that (entry − stop) × shares ≤ risk_budget. None if the stop is not below entry."""
    if not entry or not stop or not risk_budget or stop >= entry:
        return None
    # floor with a tolerance: 1000 // 0.1 evaluates to 9999 in binary floating point, so a ₹10 budget with a
    # 0.1 per-share risk would be sized as 100 shares / ₹10 of risk instead of 99.
    return int(risk_budget / (entry - stop) + 1e-9) or None


def guidance(entry, stop, risk_budget):
    n = shares(entry, stop, risk_budget)
    risk = round(entry - stop, 4) if entry and stop and stop < entry else None
    return {"entry": entry, "stop": stop, "risk_per_share": risk, "shares": n,
            "budget": risk_budget, "note": "Notional size only. No order is sent."}
