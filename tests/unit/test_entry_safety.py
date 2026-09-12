import datetime as dt
import pytest
from traderbot.core_strategy_engine.entry_models.entry_safety import quote_diagnostics, checked_quote, reentry_policy
from traderbot.core_strategy_engine.entry_models.filters import evaluate_entry_filters
from traderbot.core_strategy_engine.engine import validate_entry_submission, live_bar_context
from traderbot.broker.execution_gateway import ExecutionGateway

NOW=dt.datetime(2026,9,11,16,tzinfo=dt.timezone.utc)

def quote(**kw):
    return {'bid_price':100,'ask_price':100.1,'timestamp':NOW.isoformat(),'feed':'iex',**kw}

@pytest.mark.parametrize('fields,reason',[
({'timestamp':'garbage'},'quote_timestamp_invalid'),({'timestamp':'2026-09-11T15:00:00Z'},'quote_stale'),
({'bid_price':float('nan')},'quote_prices_invalid'),({'ask_price':'invalid'},'quote_prices_invalid'),
({'ask_price':99},'quote_crossed'),({'ask_price':120},'spread_exceeded')])
def test_quote_failure_reasons(fields,reason):
    result=quote_diagnostics(quote(**fields),NOW,{})
    assert not result['passed'] and reason in result['reasons']


def test_quote_retry_bounded_and_recorded():
    class Client:
        calls=0
        def latest_quote(self,symbol):
            self.calls+=1
            return quote(ask_price=120 if self.calls==1 else 100.1)
    client=Client();q,attempts=checked_quote(client,'TEST',{},NOW)
    assert client.calls==2 and attempts[-1]['passed']
    assert attempts[0]['reasons']==['spread_exceeded']
    client.latest_quote=lambda symbol:quote(ask_price=120)
    assert len(checked_quote(client,'TEST',{},NOW)[1])==2


def test_independent_daily_intraday_thresholds_and_feed():
    f={'symbol':'TEST','as_of':NOW,'bar':{'matched_average_dollar_volume':100000,'dollar_volume_sample_size':20}}
    c={'entry_filters':{'liquidity':{'enabled':True,'minimum_intraday_dollar_volume':50000,'minimum_daily_dollar_volume':1000000}}}
    ctx={'feed':'iex','timeframe':'5Min','daily_liquidity':{'average_daily_dollar_volume':500000,'daily_sample_size':10}}
    r=evaluate_entry_filters(f,c,ctx)
    assert r['checks']['liquidity_ok'] and not r['checks']['daily_liquidity_ok']
    assert r['metrics']['liquidity_coverage']=='single_exchange'
    assert r['metrics']['matched_intraday_average_dollar_volume']==100000


def test_reentry_weekend_holiday_expiry_does_not_delete_exit():
    exit={'exit_time':dt.datetime(2026,9,4,19,tzinfo=dt.timezone.utc),'exit_price':100,'realized_pl':-10}
    sessions=[{'date':'2026-09-04','open':'09:30'},{'date':'2026-09-08','open':'09:30'}]
    assert not reentry_policy(exit,dt.datetime(2026,9,7,16,tzinfo=dt.timezone.utc),sessions)['cap_expired']
    assert reentry_policy(exit,dt.datetime(2026,9,8,13,30,tzinfo=dt.timezone.utc),sessions)['cap_expired']
    assert exit['exit_price']==100
    assert not reentry_policy(exit,NOW,[])['cap_expired']


def test_submission_rounded_price_and_quote_checks():
    now=dt.datetime.now(dt.timezone.utc)
    class Client:
        def latest_quote(self,symbol): return quote(timestamp=now.isoformat())
    plan={'status':'active_signal','last_bar_time':now.isoformat(),'limit_price':100.006,'stop_price':99,'target_price':101.01}
    result=validate_entry_submission(Client(),{'symbol':'TEST'},plan)
    assert result['rounded_limit_price']==100.01
    assert 'reward_risk_floor_ok' in result['reasons']
    plan['target_price']=103
    assert validate_entry_submission(Client(),{'symbol':'TEST'},plan)['passed']
    plan['last_bar_time']='2026-01-01T14:00:00Z'
    assert 'stale_entry_plan' in validate_entry_submission(Client(),{'symbol':'TEST'},plan)['reasons']


def test_ambiguous_submission_reconciles_without_resubmit():
    class Client:
        existing=None; calls=0
        def order_by_client_order_id(self,key): return self.existing
        def submit_order(self,payload):
            self.calls+=1;self.existing={'id':'accepted','status':'new'}
            raise TimeoutError()
    client=Client();gateway=ExecutionGateway(client)
    payload={'symbol':'TEST','side':'buy','client_order_id':'unique'}
    assert gateway.submit_order(payload)['id']=='accepted'
    assert gateway.submit_order(payload)['id']=='accepted'
    assert client.calls==1


def test_live_context_records_completed_daily_metrics_and_caches():
    now=dt.datetime.now(dt.timezone.utc)
    class Client:
        market_data_feed='iex'
        def __init__(self):self._entry_context_cache={};self.daily_calls=0
        def stock_bars(self,symbol,start,end,timeframe):
            if timeframe=='1Day':
                self.daily_calls+=1
                return [{'t':(now-dt.timedelta(days=1)).isoformat(),'c':10,'v':1000},
                        {'t':now.isoformat(),'c':10,'v':999999}]
            return []
        def calendar(self,start,end):return []
    client=Client()
    for _ in range(2):
        ctx=live_bar_context(client,'TEST',{})[3]
        assert ctx['daily_liquidity']['average_daily_dollar_volume']==10000
        assert ctx['feed']=='iex'
    assert client.daily_calls==1

def test_new_session_removes_cap_but_preserves_loss_restriction_same_day():
    from traderbot.core_strategy_engine.entry_models.features import build_entry_features
    bars=[{'t':dt.datetime(2026,9,8,14,tzinfo=dt.timezone.utc),'c':120,'ema9':119,'ema21':118,
           'ema50':117,'atr14':1,'h':121,'l':119,'volume_ratio':1,'vwap':119} for _ in range(55)]
    exited={'exit_time':dt.datetime(2026,9,4,19,tzinfo=dt.timezone.utc),'exit_price':100,'realized_pl':-10}
    ctx={'sessions':[{'date':'2026-09-08','open':'09:30'}]}
    f=build_entry_features('TEST',bars,bars,exit_trade=exited,filter_context=ctx)
    assert f['ledger_cap']==float('inf') and f['no_same_day_loss_reentry']
    exited['exit_time']=dt.datetime(2026,9,8,13,45,tzinfo=dt.timezone.utc)
    f=build_entry_features('TEST',bars,bars,exit_trade=exited,filter_context=ctx)
    assert f['ledger_cap']==98.5 and not f['no_same_day_loss_reentry']


def test_allocator_refreshes_stale_candidate_without_submission(tmp_path):
    from unittest.mock import patch
    from traderbot.monitoring import supervisor
    from test_portfolio_allocator import BrokerSnapshot, allocation_record
    record=allocation_record('2026-09-02T14:25:00+00:00')
    updated=allocation_record()['result']['dynamic_plan']
    watcher={'symbol':'AAA','state_path':tmp_path/'aaa.json','group':'new'}
    with patch.object(supervisor,'refresh_dynamic_plan_only',return_value=updated) as refresh, patch.object(supervisor,'run_watcher') as execute:
        result,_=supervisor.run_portfolio_allocator(BrokerSnapshot(),[record],{'portfolio_allocator':{'enabled':True,'shadow_mode':True}},[watcher],tmp_path,{'timestamp':'2026-09-02T14:35:00+00:00'})
    assert refresh.call_count==1 and not execute.called
    assert result['selected'][0]['symbol']=='AAA'
    assert refresh.call_args.args[1]['entry_snapshot_end']=='2026-09-02T14:35:00+00:00'
