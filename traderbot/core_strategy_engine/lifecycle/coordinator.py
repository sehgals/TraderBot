ACTION_PRIORITIES = {
    "restore_protection": 100,
    "exit": 90,
    "portfolio_reduce": 85,
    "reduce": 80,
    "hold": 50,
    "watch": 45,
    "add": 30,
    "enter": 20,
    "freeze": 10,
}


def intent_priority(intent):
    explicit = intent.get("priority")
    if explicit is not None:
        return int(explicit)
    return ACTION_PRIORITIES.get(intent.get("action"), 0)


def select_action_intent(intents):
    candidates = [intent for intent in intents or [] if intent]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda intent: (
            -intent_priority(intent),
            str(intent.get("created_at") or ""),
            str(intent.get("action_id") or ""),
        ),
    )[0]


__all__ = ["ACTION_PRIORITIES", "select_action_intent"]
