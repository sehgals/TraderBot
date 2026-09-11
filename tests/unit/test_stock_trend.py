import datetime as dt
import math
import pytest
from traderbot.core_strategy_engine.entry_models.trend import score_stock_trend
from traderbot.core_strategy_engine.entry_models.breakout import evaluate_breakout

@pytest.mark.parametrize('bar,slope,expected', [
    ({'c':10,'vwap':9,'ema9':9,'ema21':8,'ema50':8}, .20, 100),
    ({'c':9,'vwap':9,'ema9':9,'ema21':9,'ema50':9}, .20, 40),
    ({}, None, 0),
    ({'c':float('nan'),'vwap':9,'ema9':9,'ema21':8,'ema50':8}, float('inf'), 40),
])
def test_components_and_boundaries(bar, slope, expected):
    result=score_stock_trend(bar,slope,1)
    assert result['score']==expected
    assert result['all_passed']==(expected==100)


def snapshot(symbol):
    nflx=symbol=='NFLX'
    return {
        'symbol':symbol, 'as_of':dt.datetime(2026,9,10,19,55,tzinfo=dt.timezone.utc),
        'bar':dict(c=76.03 if nflx else 492.49, vwap=75.9148433097012 if nflx else 491.50056893242885,
                   ema9=75.90319006258281 if nflx else 491.62972499685765,
                   ema21=75.91035187919688 if nflx else 491.485575290985,
                   ema50=75.95742119720676 if nflx else 491.68217639458794),
        'ema21_change_5bars':.009662649398059386 if nflx else .27118,
        'trend_atr14':.0875 if nflx else .2,
        'ema21_slope_5bars':.00012730285185212173 if nflx else .0005520742785032013,
        'previous_atr14':.0875 if nflx else .2,
        'atr14':.0875 if nflx else .2, 'recent_high_20':76.02 if nflx else 492.43,
        'recent_low_20':75.76 if nflx else 491,
        'relative_dollar_volume':.5658721426076191 if nflx else .3956180324775949,
        'ledger_cap':75.12595 if nflx else math.inf,
        'market_ok':False, 'sector_ok':nflx, 'above_exit':True, 'no_same_day_loss_reentry':True,
        'entry_filters':{'checks':{'spread_data_available':False,'spread_ok':False},'metrics':{}}
    }

@pytest.mark.parametrize('symbol,score,points',[('NFLX',51.87,8),('MSFT',59.96,16)])
def test_snapshot_based_inputs(symbol,score,points):
    c=evaluate_breakout(snapshot(symbol))
    assert c['setup_score']==score
    assert c['trend_assessment']['points']==points
    assert sum(r['points'] for r in c['trend_assessment']['components'])==points
    assert c['trend_assessment']['as_of']==c['as_of']
    assert c['minimum_setup_score']==80
    assert c['status']=='watch'

@pytest.mark.parametrize('gate',['spread_ok','ledger_price_ok','reward_risk_floor_ok'])
def test_hard_blocks_survive_high_component_score(gate):
    f=snapshot('MSFT')
    f['bar'].update(c=492.53,ema9=492,ema21=491,ema50=490)
    f['ema21_slope_5bars']=.003
    f['market_ok']=f['sector_ok']=True
    f['relative_dollar_volume']=2
    f['entry_filters']['checks']={'spread_ok':gate!='spread_ok'}
    if gate=='ledger_price_ok': f['ledger_cap']=1
    config={'entry_models':{'breakout':{'absolute_minimum_reward_risk':2}}} if gate=='reward_risk_floor_ok' else {}
    c=evaluate_breakout(f,config)
    assert c['setup_score']>=80
    assert c['status']=='watch'
    assert gate in c['hard_blockers']


def test_custom_weight_breakdown_matches_total():
    c=evaluate_breakout(snapshot('MSFT'),{'entry_score_weights':{'stock_trend':40}})
    a=c['trend_assessment']
    assert a['points']==c['factor_contributions']['stock_trend']
    assert sum(r['points'] for r in a['components'])==pytest.approx(a['points'],abs=.001)

@pytest.mark.parametrize("atr", [None, 0, -1, float("nan"), float("inf")])
def test_invalid_atr_earns_no_slope_credit(atr):
    r=score_stock_trend({}, .2, atr)['components'][-1]
    assert not r['passed'] and not r['available']
    assert r['observed'] is None


def test_atr_normalization_and_scale_invariance():
    assert score_stock_trend({},.2,.5)['components'][-1]['observed']==.4
    assert score_stock_trend({},20,50)['components'][-1]['observed']==.4
    assert not score_stock_trend({},.2,2)['components'][-1]['passed']
    assert not score_stock_trend({},-.2,.5)['components'][-1]['passed']
    assert not score_stock_trend({},.2,.5,.5)['components'][-1]['passed']


def test_configured_atr_threshold():
    c=evaluate_breakout(snapshot('MSFT'),{'entry_models':{'breakout':{'minimum_ema21_slope_atr':2}}})
    assert c['trend_assessment']['components'][-1]['threshold']==2
    assert c['trend_assessment']['points']==12

@pytest.mark.parametrize('distance,price,extension',[(0,0,10),(.1,5,10),(.25,12.5,10),(.5,25,10),(.75,25,7.5),(1,25,5),(1.5,25,0),(1.6,25,0)])
def test_breakout_curves(distance,price,extension):
    f=snapshot('MSFT'); f['previous_atr14']=1; f['recent_high_20']=100; f['bar']['c']=100+distance
    c=evaluate_breakout(f); a=c['breakout_assessment']
    assert a['price_action_points']==pytest.approx(price)
    assert a['overextension_points']==pytest.approx(extension)
    assert c['checks']['breakout_confirmed']==(distance>0)
    assert c['checks']['breakout_chase_ok']==(distance<=1.5)
    if distance==0 or distance>1.5: assert c['status']=='watch'

@pytest.mark.parametrize('atr',[None,0,-1,float('nan'),float('inf')])
def test_bad_breakout_atr_blocks(atr):
    f=snapshot('MSFT');f['previous_atr14']=atr
    c=evaluate_breakout(f)
    assert 'breakout_atr_available' in c['hard_blockers']
    assert c['factor_contributions']['price_action']==0
    assert c['status']=='watch'


def test_breakout_confirmation_and_chase_are_hard_even_with_low_score_threshold():
    for price,reason in [(492.43,'breakout_confirmed'),(493,'breakout_chase_ok')]:
        f=snapshot('MSFT'); f['bar']['c']=price;f['entry_filters']['checks']={}
        c=evaluate_breakout(f,{'minimum_entry_setup_score':0})
        assert reason in c['hard_blockers']
        assert c['status']=='watch'


def test_configured_breakout_curve_thresholds():
    f=snapshot('MSFT');f['previous_atr14']=1;f['bar']['c']=f['recent_high_20']+1
    c=evaluate_breakout(f,{'entry_models':{'breakout':{'full_breakout_score_atr':1,'maximum_chase_atr':2}}})
    assert c['breakout_assessment']['price_action_points']==25
    assert c['breakout_assessment']['overextension_points']==10
    with pytest.raises(ValueError):
        evaluate_breakout(f,{'entry_models':{'breakout':{'full_breakout_score_atr':2,'maximum_chase_atr':1}}})
