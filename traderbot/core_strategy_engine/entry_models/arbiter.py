def _candidate_rank(candidate):
    limit_price = float(candidate.get("limit_price") or 0)
    risk_per_share = float(candidate.get("risk_per_share") or 0)
    risk_percent = risk_per_share / limit_price if limit_price > 0 else float("inf")
    return (
        -int(candidate.get("setup_score") or 0),
        -float(candidate.get("expected_reward_risk") or 0),
        risk_percent,
        str(candidate.get("model_id") or ""),
    )


def select_entry_candidate(candidates):
    """Classify the setup and choose at most one active candidate.

    A close above the breakout boundary belongs exclusively to the breakout
    model. This prevents a prior pullback touch from mislabeling a confirmed
    breakout as a pullback. Other candidate types use a stable risk-adjusted
    ordering so input order cannot affect the result.
    """
    candidates = [candidate for candidate in candidates or [] if candidate]
    breakout = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("model_id") == "breakout_continuation"
        ),
        None,
    )
    pullback = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("model_id") == "pullback_reclaim"
        ),
        None,
    )
    breakout_territory = bool(
        breakout
        and float(breakout.get("last_price") or 0)
        > float(breakout.get("breakout_trigger") or float("inf"))
    )
    if breakout_territory:
        classified = breakout
        reason = "breakout_territory"
    elif pullback:
        classified = pullback
        reason = "pullback_territory"
    else:
        classified = None
        reason = "unclassified"

    if classified:
        selected = (
            classified if classified.get("status") == "active_signal" else None
        )
    else:
        active = [
            candidate
            for candidate in candidates
            if candidate.get("status") == "active_signal"
        ]
        selected = sorted(active, key=_candidate_rank)[0] if active else None
        if selected:
            reason = "deterministic_rank"

    return {
        "selected": selected,
        "classified": classified,
        "reason": reason,
        "candidate_count": len(candidates),
    }


__all__ = ["select_entry_candidate"]
