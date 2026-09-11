import datetime as dt
import pytest
from traderbot.core_strategy_engine.position_health import assessment_bar_status
from traderbot.core_strategy_engine.engine import health_refresh_due

@pytest.mark.parametrize('now,status',[
('2026-09-11T15:59:00Z','Latest completed bar'),
('2026-09-11T16:00:00Z','Waiting for newer bar'),
('2026-09-11T16:04:59Z','Waiting for newer bar'),
('2026-09-11T16:05:00Z','Assessment overdue')])
def test_on_hourly_bar(now,status):
    result,end=assessment_bar_status('2026-09-11T14:00:00Z',now)
    assert result==status
    assert end=='2026-09-11T15:00:00+00:00'


def test_missing_incomplete_bar_and_closed_session():
    assert assessment_bar_status(None,'2026-09-11T16:00:00Z')[0]=='Assessment missing'
    assert assessment_bar_status('2026-09-11T16:00:00Z','2026-09-11T16:30:00Z')[0]=='Assessment unavailable'
    assert assessment_bar_status('2026-11-27T17:00:00Z','2026-11-29T16:00:00Z','2026-11-27T18:00:00Z')[0]=='Latest completed bar'
    assert assessment_bar_status('2026-11-27T16:00:00Z','2026-11-29T16:00:00Z','2026-11-27T18:00:00Z')[0]=='Assessment overdue'


def test_poll_uses_last_check_without_overwriting_evaluation():
    state={'position_health':{'score':74},'position_health_last_evaluated_at':'2026-09-11T15:22:00Z',
           'position_health_last_checked_at':'2026-09-11T16:00:00Z'}
    settings={'refresh_seconds':3300}
    assert not health_refresh_due(state,settings,dt.datetime(2026,9,11,16,2,59,tzinfo=dt.timezone.utc))
    assert health_refresh_due(state,settings,dt.datetime(2026,9,11,16,3,tzinfo=dt.timezone.utc))


def test_unchanged_assessment_retains_evaluation_timestamp(monkeypatch):
    from traderbot.core_strategy_engine import engine
    assessment={'score':74,'bar_id':'ON:1Hour:2026-09-11T14:00:00Z'}
    state={'position_health':assessment,'position_health_last_evaluated_at':'2026-09-11T15:22:00Z'}
    monkeypatch.setattr(engine,'position_health_config',lambda config:{'enabled':True,'refresh_seconds':180})
    monkeypatch.setattr(engine,'ensure_position_episode',lambda *args:{})
    monkeypatch.setattr(engine,'position_health_market_context',lambda *args:{})
    monkeypatch.setattr(engine,'evaluate_position_health',lambda *args,**kwargs:dict(assessment))
    result=engine.refresh_position_health(None,{},state,{'qty':'5'})
    assert result is assessment
    assert state['position_health_last_evaluated_at']=='2026-09-11T15:22:00Z'
    assert state['position_health_last_checked_at']
