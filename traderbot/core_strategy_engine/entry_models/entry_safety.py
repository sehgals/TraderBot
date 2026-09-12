"""Pure quote validation and trading-session reentry policy."""
import datetime as dt
import math
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo('America/New_York')


def quote_diagnostics(quote, now, settings):
    reasons=[]; age=None; spread=None
    if now.tzinfo is None: now=now.replace(tzinfo=dt.timezone.utc)
    def number(value):
        try:
            result=float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError): return None
    bid=number(quote.get('bid_price')); ask=number(quote.get('ask_price'))
    try:
        stamp=dt.datetime.fromisoformat(str(quote.get('timestamp')).replace('Z','+00:00'))
        if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=dt.timezone.utc)
        age=(now-stamp).total_seconds()
        if not -5 <= age <= float(settings.get('maximum_quote_age_seconds',60)): reasons.append('quote_stale')
    except (ValueError,TypeError): reasons.append('quote_timestamp_invalid')
    if bid is None or ask is None or bid<=0 or ask<=0: reasons.append('quote_prices_invalid')
    elif ask<bid: reasons.append('quote_crossed')
    else: spread=(ask-bid)/((ask+bid)/2)*100
    rejected=set(settings.get('rejected_quote_conditions', []))
    if rejected.intersection(quote.get('conditions') or []): reasons.append('quote_condition_rejected')
    valid=not reasons
    if valid and spread>float(settings.get('maximum_percent',.5)): reasons.append('spread_exceeded')
    return {'valid':valid,'passed':not reasons,'reasons':reasons,'quoted_spread_percent':spread,
            'quote_age_seconds':age,'bid_price':bid,'ask_price':ask,
            'quote_timestamp':quote.get('timestamp'),'feed':quote.get('feed','unknown'),
            'quote_conditions':quote.get('conditions',[]),'bid_exchange':quote.get('bid_exchange'),
            'ask_exchange':quote.get('ask_exchange'),'bid_size':quote.get('bid_size'),'ask_size':quote.get('ask_size')}


def checked_quote(client,symbol,settings,now=None):
    attempts=[]; quote={}
    for _ in range(2):
        try: quote=client.latest_quote(symbol) or {}
        except Exception: quote={}
        checked=now or dt.datetime.now(dt.timezone.utc)
        result=quote_diagnostics(quote,checked,settings)
        attempts.append(result)
        if result['passed']: break
    return quote,attempts


def reentry_policy(exit_trade,as_of,sessions):
    if not exit_trade: return {'cap_expired':True,'reason':'no_exit','expires_at':None}
    exited=exit_trade.get('exit_time')
    if not exited: return {'cap_expired':False,'reason':'exit_time_missing','expires_at':None}
    openings=[]
    for session in sessions or []:
        opened=dt.datetime.fromisoformat(f"{session['date']}T{session['open']}").replace(tzinfo=EASTERN)
        if opened>exited: openings.append(opened)
    expiry=min(openings) if openings else None
    return {'cap_expired':bool(expiry and as_of>=expiry),
            'reason':'next_session_setup' if expiry and as_of>=expiry else 'exit_restriction_active',
            'expires_at':expiry.isoformat() if expiry else None}
