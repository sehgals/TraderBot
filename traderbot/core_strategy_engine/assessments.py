from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class EntryAssessment:
    symbol: str
    as_of: str | None
    score: int | None
    status: str
    mode: str | None = None
    limit_price: float | None = None
    target_price: float | None = None
    initial_stop_price: float | None = None
    blockers: list[str] = field(default_factory=list)
    model_version: str = "entry-v1"

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PositionHealthAssessment:
    symbol: str
    as_of: str | None
    bar_id: str | None
    score: int | None
    state: str
    recommended_action: str
    downside_score: int | None = None
    trend_score: int | None = None
    reward_risk_score: int | None = None
    entry_return_percent: float | None = None
    remaining_r: float | None = None
    stop_price: float | None = None
    stop_qty: int = 0
    position_qty: int = 0
    data_complete: bool = False
    data_fresh: bool = False
    reasons: list[str] = field(default_factory=list)
    model_version: str = "health-v1"
    components: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ActionIntent:
    action_id: str
    symbol: str
    action: str
    priority: int
    episode_id: str | None = None
    qty: int | None = None
    reason: str | None = None
    assessment_id: str | None = None
    expected_state_version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


__all__ = ["ActionIntent", "EntryAssessment", "PositionHealthAssessment"]
