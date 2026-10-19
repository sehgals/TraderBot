"""Trading-session boundaries for market-data comparisons."""

import datetime as dt
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


def session_open_utc(observed_at, alpaca_client):
    """Return the observed trading day's calendar open in UTC."""
    trading_date = observed_at.astimezone(EASTERN).date()
    day = trading_date.isoformat()
    sessions = alpaca_client.calendar(day, day)
    session = next((item for item in sessions if item.get("date") == day), None)
    if session is None:
        raise ValueError(f"No trading session on {day}")
    open_time = dt.time.fromisoformat(session["open"])
    opened = dt.datetime.combine(trading_date, open_time, EASTERN).astimezone(dt.timezone.utc)
    if observed_at < opened:
        raise ValueError(f"Trading session has not opened on {day}")
    return opened
