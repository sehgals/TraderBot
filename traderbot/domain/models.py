"""Core domain entities, value objects, and typed data models for TraderBot.

Provides strongly-typed domain objects with seamless conversions to and from
legacy broker/JSON dictionaries.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    TRAILING_STOP = "trailing_stop"


class PositionState(str, Enum):
    FLAT = "FLAT"
    ENTRY_PENDING = "ENTRY_PENDING"
    OPEN = "OPEN"
    REDUCED = "REDUCED"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class Bar:
    """Historical market bar value object."""
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: Optional[float] = None

    @classmethod
    def from_alpaca(cls, payload: Dict[str, Any]) -> "Bar":
        return cls(
            timestamp=str(payload.get("t") or payload.get("timestamp") or ""),
            open=float(payload.get("o") or payload.get("open") or 0.0),
            high=float(payload.get("h") or payload.get("high") or 0.0),
            low=float(payload.get("l") or payload.get("low") or 0.0),
            close=float(payload.get("c") or payload.get("close") or 0.0),
            volume=int(payload.get("v") or payload.get("volume") or 0),
            vwap=float(payload.get("vw")) if payload.get("vw") is not None else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "t": self.timestamp,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
            "vw": self.vwap,
        }


@dataclass(frozen=True)
class Position:
    """Broker holding position entity."""
    symbol: str
    quantity: int
    avg_entry_price: float
    current_price: float
    unrealized_pl: float = 0.0
    unrealized_plpc: float = 0.0

    @property
    def market_value(self) -> float:
        return round(abs(self.quantity) * self.current_price, 2)

    @property
    def unrealized_pnl(self) -> float:
        return round((self.current_price - self.avg_entry_price) * self.quantity, 2)

    @property
    def unrealized_pnl_percent(self) -> float:
        if self.avg_entry_price <= 0:
            return 0.0
        return round(((self.current_price / self.avg_entry_price) - 1.0) * 100, 4)

    @classmethod
    def from_alpaca(cls, payload: Dict[str, Any]) -> "Position":
        qty = int(float(payload.get("qty") or 0))
        entry = float(payload.get("avg_entry_price") or 0.0)
        curr = float(payload.get("current_price") or 0.0)
        pl = float(payload.get("unrealized_pl") or 0.0)
        plpc = float(payload.get("unrealized_plpc") or 0.0)
        return cls(
            symbol=str(payload.get("symbol") or "").upper(),
            quantity=qty,
            avg_entry_price=entry,
            current_price=curr,
            unrealized_pl=pl,
            unrealized_plpc=plpc,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": str(self.quantity),
            "avg_entry_price": str(self.avg_entry_price),
            "current_price": str(self.current_price),
            "market_value": str(self.market_value),
            "unrealized_pl": str(self.unrealized_pnl),
            "unrealized_plpc": str(self.unrealized_pnl_percent / 100.0),
        }


@dataclass(frozen=True)
class Order:
    """Trading order representation."""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    status: str
    client_order_id: Optional[str] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    filled_avg_price: Optional[float] = None
    filled_qty: int = 0

    @classmethod
    def from_alpaca(cls, payload: Dict[str, Any]) -> "Order":
        return cls(
            order_id=str(payload.get("id") or ""),
            symbol=str(payload.get("symbol") or "").upper(),
            side=OrderSide(str(payload.get("side", "buy")).lower()),
            order_type=OrderType(str(payload.get("type", "market")).lower()),
            quantity=int(float(payload.get("qty") or 0)),
            status=str(payload.get("status") or "new").lower(),
            client_order_id=payload.get("client_order_id"),
            limit_price=float(payload.get("limit_price")) if payload.get("limit_price") else None,
            stop_price=float(payload.get("stop_price")) if payload.get("stop_price") else None,
            filled_avg_price=float(payload.get("filled_avg_price")) if payload.get("filled_avg_price") else None,
            filled_qty=int(float(payload.get("filled_qty") or 0)),
        )

    def to_dict(self) -> Dict[str, Any]:
        res = {
            "id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "type": self.order_type.value,
            "qty": str(self.quantity),
            "status": self.status,
            "filled_qty": str(self.filled_qty),
        }
        if self.limit_price is not None:
            res["limit_price"] = str(self.limit_price)
        if self.stop_price is not None:
            res["stop_price"] = str(self.stop_price)
        if self.filled_avg_price is not None:
            res["filled_avg_price"] = str(self.filled_avg_price)
        return res


@dataclass
class PositionEpisode:
    """Tracks position holding lifecycle, extremes (MAE/MFE), and shadow telemetry."""
    episode_id: str
    symbol: str
    fill_price: float
    initial_stop_price: float
    initial_risk_per_share: float
    state: PositionState = PositionState.OPEN
    high_water_price: float = field(default=0.0)
    low_water_price: float = field(default=0.0)
    state_version: int = 1
    mfe_percent: float = 0.0
    mae_percent: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    rollout_cohort: str = "control"
    stop_mode: str = "legacy"
    target_mode: str = "legacy"

    def __post_init__(self):
        if self.high_water_price == 0.0 and self.fill_price > 0:
            self.high_water_price = self.fill_price
        if self.low_water_price == 0.0 and self.fill_price > 0:
            self.low_water_price = self.fill_price

    def update_price_extremes(self, current_price: float) -> None:
        if current_price <= 0 or self.fill_price <= 0:
            return
        self.high_water_price = max(self.high_water_price or self.fill_price, current_price)
        self.low_water_price = min(self.low_water_price or self.fill_price, current_price)
        mfe_dollars = max(0.0, self.high_water_price - self.fill_price)
        mae_dollars = max(0.0, self.fill_price - self.low_water_price)
        self.mfe_percent = round((mfe_dollars / self.fill_price) * 100, 4)
        self.mae_percent = round((mae_dollars / self.fill_price) * 100, 4)
        if self.initial_risk_per_share > 0:
            self.mfe_r = round(mfe_dollars / self.initial_risk_per_share, 4)
            self.mae_r = round(mae_dollars / self.initial_risk_per_share, 4)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PositionEpisode":
        return cls(
            episode_id=str(data.get("episode_id") or ""),
            symbol=str(data.get("symbol") or ""),
            fill_price=float(data.get("fill_price") or data.get("entry_fill_price") or 0.0),
            initial_stop_price=float(data.get("initial_stop_price") or 0.0),
            initial_risk_per_share=float(data.get("initial_risk_per_share") or 0.0),
            state=PositionState(data.get("state", "OPEN")),
            high_water_price=float(data.get("max_price") or data.get("high_water_price") or 0.0),
            low_water_price=float(data.get("min_price") or data.get("low_water_price") or 0.0),
            state_version=int(data.get("state_version", 1)),
            mfe_percent=float(data.get("mfe_percent", 0.0)),
            mae_percent=float(data.get("mae_percent", 0.0)),
            mfe_r=float(data.get("mfe_r", 0.0)),
            mae_r=float(data.get("mae_r", 0.0)),
            rollout_cohort=str(data.get("rollout_cohort", "control")),
            stop_mode=str(data.get("stop_mode", "legacy")),
            target_mode=str(data.get("target_mode", "legacy")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "symbol": self.symbol,
            "fill_price": self.fill_price,
            "initial_stop_price": self.initial_stop_price,
            "initial_risk_per_share": self.initial_risk_per_share,
            "state": self.state.value if isinstance(self.state, PositionState) else str(self.state),
            "max_price": self.high_water_price,
            "min_price": self.low_water_price,
            "state_version": self.state_version,
            "mfe_percent": self.mfe_percent,
            "mae_percent": self.mae_percent,
            "mfe_r": self.mfe_r,
            "mae_r": self.mae_r,
            "rollout_cohort": self.rollout_cohort,
            "stop_mode": self.stop_mode,
            "target_mode": self.target_mode,
        }
