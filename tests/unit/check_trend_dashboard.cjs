const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ctx={React:{createElement:(tag,props,...children)=>({tag,props,children})},window:{addEventListener(){}},document:{getElementById(){}},ReactDOM:{createRoot:()=>({render(){}})},Intl,Date};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('traderbot/dashboard/static/app.js','utf8'),ctx);
const result=ctx.TrendDetails({assessment:{points:12,maximum_points:20,as_of:'2026-09-10T19:55:00Z',components:[{label:'EMA21 slope over five bars',observed:.000552074,threshold:.002,operator:'>=',unit:'ratio',passed:false,available:true,points:0,maximum_points:4}]}});
const rendered=JSON.stringify(result);
for(const text of ['Trend: 12/20','0.0552%','0.2000%','Fail','0/4','Assessed']) assert.ok(rendered.includes(text),text);
assert.ok(JSON.stringify(ctx.TrendDetails({})).includes('Trend breakdown not recorded'));
console.log('Dashboard trend rendering checks passed');

const atr=JSON.stringify(ctx.TrendDetails({assessment:{points:4,maximum_points:20,components:[{observed:.4,threshold:.2,unit:'atr',operator:'>=',passed:true,points:4,maximum_points:4}]}}));
assert.ok(atr.includes('0.4000 ATR')); assert.ok(atr.includes('0.2000 ATR'));
const breakout=JSON.stringify(ctx.BreakoutDetails({assessment:{resistance:100,close:100.75,atr14:1,distance_atr:.75,full_score_atr:.5,maximum_chase_atr:1.5,price_action_points:25,price_action_maximum:25,overextension_points:7.5,overextension_maximum:10,blockers:['breakout_chase_ok']}}));
for(const text of ['Previous-bar ATR 1.0000','Distance 0.7500 ATR','Price action 25/25','Overextension 7.5/10','breakout chase ok']) assert.ok(breakout.includes(text),text);
assert.equal(vm.runInContext("healthReason('ema21_slope_positive')",ctx),'EMA21 slope not positive');
assert.equal(vm.runInContext("healthReason('market_regime_favorable')",ctx),'Market regime unfavorable');
