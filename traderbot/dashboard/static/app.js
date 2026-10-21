const h = React.createElement;
let printDetails = [];
window.addEventListener('beforeprint',()=>{printDetails=Array.from(document.querySelectorAll('details:not([open])'));printDetails.forEach(detail=>detail.open=true);});
window.addEventListener('afterprint',()=>{printDetails.forEach(detail=>detail.open=false);printDetails=[];});
const money = v => v == null ? '—' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(Number(v));
const pct = v => v == null ? '—' : `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`;
const stamp = v => v ? new Date(v).toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}) : 'Not available';
const human = v => String(v || 'Unknown').replaceAll('_',' ');
function blockerReason(value) {
 const key=String(value).replaceAll(' ','_');
 return ({spread_data_available:'Valid spread data unavailable',spread_ok:'Spread requirement not met',breakout_chase_ok:'Breakout exceeds chase limit',breakout_confirmed:'Breakout not confirmed',breakout_atr_available:'Breakout ATR unavailable',entry_signal_inactive:'Entry signal inactive',execution_chase_limit:'Execution ask exceeds chase limit',quote_stale:'Quote is stale',quote_prices_invalid:'Bid or ask is missing or invalid',quote_timestamp_invalid:'Quote timestamp is invalid',quote_crossed:'Ask is below bid',quote_condition_rejected:'Quote condition rejected',spread_exceeded:'Spread exceeds maximum',setup_score_below_minimum:'Setup score below minimum',stale_entry_plan:'Entry plan is stale',liquidity_ok:'Liquidity below minimum',liquidity_data_available:'Liquidity data unavailable',ledger_price_ok:'Entry price exceeds reentry cap',reward_risk_floor_ok:'Reward/risk below minimum',risk_geometry_valid:'Invalid entry, stop or target prices',risk_geometry_invalid:'Invalid entry, stop or target prices',model_enabled:'Entry model disabled',no_same_day_loss_reentry:'Same-day reentry after a loss is restricted'}[key] || `Requirement not met: ${human(key)}`);
}
function BlockerConditions({candidate:c}) {
 const d=c.diagnostics||{},m=d.metrics||{},a=c.breakout_assessment||{};
 const n=v=>v==null?'Unavailable':Number(v).toLocaleString('en-US',{maximumFractionDigits:4});
 const reasons=[...new Set([...(c.blockers||[]),...(d.eligibility_reasons||[])].map(v=>String(v).replaceAll(' ','_')))];
 if(!reasons.length)return h('span',null,'No recorded blockers');
 return h('div',null,...reasons.map(key=>{
  let condition='';
  if(key==='spread_data_available')condition=(m.reasons||[]).map(blockerReason).join('; ');
  if(key==='spread_ok')condition=m.quoted_spread_percent==null?`Cannot verify spread; maximum ${n(m.maximum_spread_percent)}%`:`${n(m.quoted_spread_percent)}% > maximum ${n(m.maximum_spread_percent)}%`;
  if(key==='breakout_chase_ok')condition=`${n(a.distance_atr)} ATR > maximum ${n(a.maximum_chase_atr)} ATR`;
  if(key==='setup_score_below_minimum')condition=`${n(c.score)} < minimum ${n(c.minimum_score)}`;
  return h('p',{key},blockerReason(key),condition?`: ${condition}`:'');
 }));
}
const healthReason = v => ({ema21_slope_positive:'EMA21 slope not positive',ema9_above_ema21:'EMA9 not above EMA21',market_regime_favorable:'Market regime unfavorable',relative_strength_positive:'Relative strength not positive',price_above_ema21:'Price not above EMA21',price_above_ema50:'Price not above EMA50'}[v] || human(v));
function pill(text, tone='neutral') { return h('span',{className:`pill ${tone}`},text); }
function table(headers, rows, empty) {return h('div',{className:'table-wrap',tabIndex:0,role:'region','aria-label':`${headers.join(', ')} table; scroll horizontally for more columns`},h('table',null,h('thead',null,h('tr',null,...headers.map(x=>h('th',{key:x,scope:'col'},x)))),h('tbody',null,...(rows.length ? rows.map((row,i)=>h('tr',{key:i},...row.map((cell,j)=>h('td',{key:j},cell)))) : [h('tr',{key:'empty'},h('td',{colSpan:headers.length,className:'empty'},empty))]))));}
function TrendDetails({assessment:a}) {
 if (!a || !Array.isArray(a.components)) return h('small',null,'Trend breakdown not recorded');
 const value=(v,unit)=>v==null?'Unavailable':unit==='atr'?`${Number(v).toFixed(4)} ATR`:unit==='ratio'?`${(v*100).toFixed(4)}%`:Number(v).toFixed(4);
 return h('details',null,h('summary',null,`Trend: ${a.points}/${a.maximum_points}`),
  h('small',null,`Assessed ${stamp(a.as_of)}`),
  table(['Condition','Observed','Required','Result','Points'],a.components.map(r=>[
   r.label,value(r.observed,r.unit),`${r.operator} ${value(r.threshold,r.unit)}`,
   r.available===false?'Unavailable':r.passed?'Pass':'Fail',`${r.points}/${r.maximum_points}`]),'No trend components recorded'));
}
function BreakoutDetails({assessment:a}) {
 if (!a) return null;
 const n=v=>v==null?'Unavailable':Number(v).toFixed(4);
 return h('details',null,h('summary',null,'Breakout score details'),
 h('p',null,`Resistance ${n(a.resistance)} | Close ${n(a.close)} | Previous-bar ATR ${n(a.atr14)}`),
 h('p',null,`Distance ${n(a.distance_atr)} ATR | Full credit at ${a.full_score_atr} ATR | Chase limit ${a.maximum_chase_atr} ATR`),
 h('p',null,`Price action ${a.price_action_points}/${a.price_action_maximum} | Overextension ${a.overextension_points}/${a.overextension_maximum}`),
 h('p',null,(a.blockers||[]).map(blockerReason).join(', ')||'Breakout gates passed'),h('small',null,`Assessed ${stamp(a.as_of)}`));
}
function EntryDiagnostics({candidate:c}) {
 const d=c.diagnostics||{},m=d.metrics||{},policy=d.reentry_policy||{};
 const n=v=>v==null?'Unavailable':Number(v).toLocaleString('en-US',{maximumFractionDigits:4});
 return h('details',null,h('summary',null,'Entry checks'),
 h('p',null,`Feed: ${m.feed||m.liquidity_feed||'Unknown'} | Coverage: ${human(m.liquidity_coverage)}`),
 h('p',null,`Spread ${n(m.quoted_spread_percent)}% / maximum ${n(m.maximum_spread_percent)}% | Quote age at check ${n(m.quote_age_seconds)}s | Quote ${stamp(m.quote_timestamp)}`),
 h('p',null,`Bid ${n(m.bid_price)} / Ask ${n(m.ask_price)} | ${(m.reasons||[]).map(blockerReason).join(', ')}`),
 h('p',null,`Matched intraday dollar volume ${n(m.matched_intraday_average_dollar_volume)} / minimum ${n(m.minimum_average_dollar_volume)} | ${m.liquidity_timeframe||'Unknown timeframe'} | ${n(m.dollar_volume_sample_size)} samples`),
 h('p',null,`Average daily dollar volume ${n(m.average_daily_dollar_volume)} | ${n(m.daily_sample_size)} completed sessions | Minimum ${m.minimum_daily_dollar_volume==null?'Not configured':n(m.minimum_daily_dollar_volume)}`),
 h('p',null,`Reentry: ${human(policy.reason)} | Restriction expiry ${stamp(policy.expires_at)} | Price cap ${n(d.ledger_cap)}`),
 d.submission_validation&&h('p',null,`Last submission check (${stamp(d.submission_validation.checked_at)}): ${(d.submission_validation.reasons||[]).map(blockerReason).join('; ')||'No recorded blockers'}. This check may belong to an earlier assessment.`));
}
function OperationsView({data, section='all', onOpenAddStock}) {
 const [symbol,setSymbol]=React.useState(''),[attention,setAttention]=React.useState(false);
 const [showAll,setShowAll]=React.useState(false);
 const limit=showAll?undefined:10;
 const matches=row=>row.symbol.toLowerCase().includes(symbol.trim().toLowerCase());
 const showWatchers = section === 'all' || section === 'watchers';
 const showCandidates = section === 'all' || section === 'candidates';
 const showAlerts = section === 'all' || section === 'alerts';
 const showActivity = section === 'all' || section === 'activity';
 return h('section',null,
  showWatchers && h('section',{className:'panel'},
   h('div',{className:'section-head'},
    h('h2',null,'Watcher operations'),
    h('div',{className:'filters'},
     onOpenAddStock && h('button',{type:'button',className:'btn-add-stock',onClick:onOpenAddStock},'+ Add stock'),
     h('input',{'aria-label':'Filter operations by symbol',placeholder:'Filter by symbol',value:symbol,onChange:e=>setSymbol(e.target.value)})
    )
   ),
   h('p',{className:'muted'},`Files checked: ${stamp(data.as_of)} | Supervisor process: ${data.process_status}`),
   h('p',{className:'muted'},data.note),
   h('div',{className:'summary-strip'},...Object.entries(data.counts).map(([key,count])=>h('span',{key,className:['Overdue','Recent errors','Source unavailable'].includes(key)&&count?'amber-text':''},`${count} ${key.toLowerCase()}`))),
   h('div',{className:'filters'},
    h('label',{className:'muted'},h('input',{type:'checkbox',checked:attention,onChange:e=>setAttention(e.target.checked)}),' Show watchers needing attention'),
    h('label',{className:'muted'},h('input',{type:'checkbox',checked:showAll,onChange:e=>setShowAll(e.target.checked)}),' Show all operational rows')
   ),
   h('p',{className:'muted'},'Tables show up to 10 rows initially. Filter by symbol or show all rows to inspect more.'),
   table(['Symbol','Activity status','Last activity','Next expected'],data.watchers.filter(matches).filter(w=>!attention||!['Within schedule','Disabled'].includes(w.status)).slice(0,limit).map(w=>[w.symbol,pill(w.status,w.status==='Within schedule'?'green':w.status==='Disabled'?'neutral':'amber'),stamp(w.last_activity),stamp(w.next_expected)]),'No watchers match, or watcher configuration is unavailable.'),
   data.issues.length>0&&h('details',null,h('summary',null,`${data.issues.length} source issues`),...data.issues.map((issue,i)=>h('p',{key:i},`${issue.source}: ${issue.message}`)))
  ),
  showCandidates && h('section',{className:'panel'},
   h('div',{className:'section-head'},
    h('div',null,
     h('h2',null,'Entry candidates'),
     h('p',{className:'muted'},'Recorded setup assessments for configured candidates not currently held. Scores do not imply an order will be placed.')
    ),
    h('div',{className:'filters'},
     onOpenAddStock && h('button',{type:'button',className:'btn-add-stock',onClick:onOpenAddStock},'+ Add candidate stock'),
     !showWatchers && h('input',{'aria-label':'Filter operations by symbol',placeholder:'Filter by symbol',value:symbol,onChange:e=>setSymbol(e.target.value)})
    )
   ),
   table(['Symbol','Score','Model','Status','Blocker condition','Assessment time','Source'],data.candidates.filter(matches).slice(0,limit).map(c=>[c.symbol,h('div',null,c.score??'Unavailable',h(TrendDetails,{assessment:c.trend_assessment}),h(BreakoutDetails,{assessment:c.breakout_assessment})),human(c.model),h('div',null,c.decision_label||human(c.status),h(EntryDiagnostics,{candidate:c})),h(BlockerConditions,{candidate:c}),stamp(c.as_of),c.source_status]),'No candidate assessments match this filter.')
  ),
  showAlerts && h('section',{className:'panel'},
   h('div',{className:'section-head'},
    h('h2',null,'Alert history'),
    !showWatchers && !showCandidates && h('input',{'aria-label':'Filter operations by symbol',placeholder:'Filter by symbol',value:symbol,onChange:e=>setSymbol(e.target.value)})
   ),
   table(['Time','Symbol','Source','Event','Detail'],data.alerts.filter(matches).slice(0,limit).map(a=>[stamp(a.timestamp),a.symbol,a.source,pill(human(a.kind),'amber'),human(a.detail)]),'No alerts in the recent log window. Missing sources are listed above.')
  ),
  showActivity && h('section',{className:'panel'},
   h('div',{className:'section-head'},
    h('h2',null,'Recent watcher activity'),
    !showWatchers && !showCandidates && !showAlerts && h('input',{'aria-label':'Filter operations by symbol',placeholder:'Filter by symbol',value:symbol,onChange:e=>setSymbol(e.target.value)})
   ),
   table(['Time','Symbol','Event','Failures','Next run'],data.activity.filter(matches).slice(0,limit).map(a=>[stamp(a.timestamp),a.symbol,human(a.status),a.failures??'—',a.next_run_seconds==null?'—':`${a.next_run_seconds}s`]),'No activity in the recent log window.')
  )
 );
}
function FeedComparisonView({rows}) {
 const [symbol,setSymbol]=React.useState('');
 const list = rows || [];
 const filtered=list.filter(row=>row.symbol.toLowerCase().includes(symbol.trim().toLowerCase()));
 const n=(value,digits=2)=>value==null?'Unavailable':Number(value).toFixed(digits);
 const tone=score=>score>=80?'green':score>=65?'neutral':'amber';
 const tastyPreferred = list.filter(r => (r.preferred_source || '').toLowerCase().includes('tasty')).length;
 const alpacaPreferred = list.filter(r => (r.preferred_source || '').toLowerCase().includes('alpaca')).length;
 const highGrade = list.filter(r => Number(r.score) >= 80).length;
 return h('section',null,
  list.length > 0 && h('div',{className:'metrics'},
   h('section',{className:'metric'},h('span',null,'Tickers compared'),h('strong',null,list.length),h('small',null,'Active feeds evaluated')),
   h('section',{className:'metric'},h('span',null,'tastytrade preferred'),h('strong',null,tastyPreferred),h('small',null,'Tighter midpoint diff / completeness')),
   h('section',{className:'metric'},h('span',null,'Alpaca preferred'),h('strong',null,alpacaPreferred),h('small',null,'Tighter spread / higher fill')),
   h('section',{className:'metric'},h('span',null,'Grade A feeds (>=80)'),h('strong',null,highGrade),h('small',null,'High quality consistency'))
  ),
  h('section',{className:'panel'},
   h('div',{className:'section-head'},
    h('div',null,
     h('h2',null,'Feed comparison by ticker'),
     h('p',{className:'muted'},'Rolling Alpaca versus tastytrade quality score. This is diagnostic and does not change trading decisions.')
    ),
    h('input',{'aria-label':'Filter feed comparisons by symbol',placeholder:'Filter by symbol',value:symbol,onChange:e=>setSymbol(e.target.value)})
   ),
   table(['Symbol','Score','Preferred feed','Samples','Median difference','Bar completeness','Latest comparison'],filtered.map(row=>[
    row.symbol,pill(`${row.score} · ${row.grade}`,tone(row.score)),h('div',null,h('b',null,human(row.preferred_source)),h('small',null,row.reason)),row.samples,`${n(row.median_midpoint_difference_bps)} bps`,`${n(row.bar_completeness_percent)}%`,stamp(row.observed_at)
   ]),'No tastytrade comparison records are available yet.')
  )
 );
}
function OrdersView({data}) {
 const [symbol,setSymbol]=React.useState(''),[status,setStatus]=React.useState('all');
 const orders=data.orders.filter(o=>(o.symbol||'').toLowerCase().includes(symbol.trim().toLowerCase())&&(status==='all'||o.status===status));
 return h('section',{className:'panel'},h('div',{className:'section-head'},h('h2',null,'Recent orders'),h('div',{className:'filters'},h('input',{'aria-label':'Filter orders by symbol',value:symbol,placeholder:'Order symbol',onChange:e=>setSymbol(e.target.value)}),h('select',{'aria-label':'Filter order status',value:status,onChange:e=>setStatus(e.target.value)},h('option',{value:'all'},'All statuses'),...Array.from(new Set(data.orders.map(o=>o.status).filter(Boolean))).map(s=>h('option',{key:s,value:s},human(s)))))),
  h('p',{className:'muted'},`Latest 20 orders | Fetched ${stamp(data.connection.orders.as_of)} | ${data.connection.orders.stale?'Stale / unavailable':'Current'}`),
  table(['Submitted','Symbol','Side','Quantity','Filled','Status'],orders.map(o=>[stamp(o.submitted_at),o.symbol,human(o.side),o.qty,o.filled_qty,pill(human(o.status))]),data.connection.orders.stale?'Orders unavailable or outdated.':'No recent orders match this filter.'));
}
function PositionHealthTriadMeter({position: p}) {
 if (p.position_health_downside_score == null && p.position_health_trend_score == null && p.position_health_reward_risk_score == null) return null;
 const ds = Number(p.position_health_downside_score || 0);
 const ts = Number(p.position_health_trend_score || 0);
 const rr = Number(p.position_health_reward_risk_score || 0);
 const dsPts = (ds * 0.45).toFixed(1);
 const tsPts = (ts * 0.35).toFixed(1);
 const rrPts = (rr * 0.20).toFixed(1);
 const tone = v => v >= 80 ? 'fill-green' : v >= 45 ? 'fill-amber' : 'fill-red';
 const step = v => `w-${Math.min(100, Math.max(0, Math.round(v / 10) * 10))}`;
 return h('div', {className: 'health-triad-grid'},
  h('div', {className: 'triad-col'},
   h('span', null, 'Downside (45%)'),
   h('strong', null, `${ds}/100 · ${dsPts} pts`),
   h('div', {className: 'meter-bar'}, h('div', {className: `meter-fill ${tone(ds)} ${step(ds)}`}))
  ),
  h('div', {className: 'triad-col'},
   h('span', null, 'Trend (35%)'),
   h('strong', null, `${ts}/100 · ${tsPts} pts`),
   h('div', {className: 'meter-bar'}, h('div', {className: `meter-fill ${tone(ts)} ${step(ts)}`}))
  ),
  h('div', {className: 'triad-col'},
   h('span', null, 'Reward/Risk (20%)'),
   h('strong', null, `${rr}/100 · ${rrPts} pts`),
   h('div', {className: 'meter-bar'}, h('div', {className: `meter-fill ${tone(rr)} ${step(rr)}`}))
  )
 );
}
function RiskProximityMeter({position: p}) {
 const ret = p.total_gain_loss_percent == null ? null : Number(p.total_gain_loss_percent);
 const stopQty = Number(p.position_health_stop_qty || 0);
 const qty = Number(p.qty || 0);
 const stopCovered = p.position_health_stop_price > 0 && stopQty >= qty;
 let statusTone = 'badge-green', statusLabel = 'Safe buffer';
 let bufferText = '';
 if (ret != null) {
  if (ret <= -8) {
   statusTone = 'badge-red';
   statusLabel = 'Catastrophic Stop Hit (Exit Intent)';
   bufferText = 'Loss exceeds 8.0% emergency limit';
  } else if (ret <= -6) {
   statusTone = 'badge-amber';
   statusLabel = 'Hard Reduction Hit (50% Trim)';
   bufferText = `${(ret + 8).toFixed(2)}% buffer before catastrophic stop`;
  } else if (ret < 0) {
   statusTone = 'badge-blue';
   statusLabel = 'Monitoring Drawdown';
   bufferText = `${(ret + 6).toFixed(2)}% buffer before 50% hard reduction`;
  } else {
   statusTone = 'badge-green';
   statusLabel = 'Gain / Positive Return';
   bufferText = `${(ret + 6).toFixed(2)}% cushion above hard reduction threshold`;
  }
 }
 return h('div', {className: 'risk-proximity-box'},
  h('div', {className: 'risk-proximity-header'},
   h('span', null, 'Safety circuit breakers & stop status'),
   h('span', {className: `badge ${statusTone}`}, statusLabel)
  ),
  ret != null && h('p', {className: 'muted'}, `Return relative to entry: ${pct(ret)} | ${bufferText}`),
  h('p', {className: stopCovered ? 'green-text' : 'red-text'},
   stopCovered
    ? `✓ Broker stop coverage complete: ${stopQty}/${qty} shares protected at ${money(p.position_health_stop_price)}`
    : `⚠ Unprotected: broker stop covers ${stopQty}/${qty} shares. Action required: restore protection.`
  )
 );
}
function RulesInsightView() {
 const [tab, setTab] = React.useState('lifecycle');
 const [simReturn, setSimReturn] = React.useState(0);
 const [simTrend, setSimTrend] = React.useState(75);
 const [simRR, setSimRR] = React.useState(1.5);
 const [simStop, setSimStop] = React.useState(true);

 const calcDownside = r => {
  if (r >= 0) return 100;
  if (r >= -3) return Math.round(70 + (r + 3) / 3 * 30);
  if (r >= -6) return Math.round(30 + (r + 6) / 3 * 40);
  if (r > -8) return Math.round(0 + (r + 8) / 2 * 30);
  return 0;
 };
 const dsScore = calcDownside(Number(simReturn));
 const rrScore = Math.round(Math.max(0, Math.min(100, 50 * Number(simRR))));
 const totalScore = Math.round(0.45 * dsScore + 0.35 * Number(simTrend) + 0.20 * rrScore);

 let state = 'Healthy', action = 'hold (add-eligible)', stateTone = 'green';
 let overrideMsg = '';
 if (!simStop) {
  state = 'Unprotected';
  action = 'restore_protection';
  stateTone = 'amber';
  overrideMsg = 'Circuit breaker: Missing or incomplete broker stop coverage forces Unprotected status.';
 } else if (simReturn <= -8) {
  state = 'Critical';
  action = 'exit';
  stateTone = 'neutral';
  overrideMsg = 'Circuit breaker: Loss exceeds -8.0% catastrophic stop threshold. Immediate exit Intent.';
 } else if (simReturn <= -6) {
  state = 'At Risk';
  action = 'reduce';
  stateTone = 'amber';
  overrideMsg = 'Circuit breaker: Loss exceeds -6.0% hard reduction threshold. One-time 50% trim + additions disabled.';
 } else if (totalScore >= 80) {
  state = 'Healthy';
  action = 'hold (add-eligible)';
  stateTone = 'green';
 } else if (totalScore >= 65) {
  state = 'Stable';
  action = 'hold';
  stateTone = 'neutral';
 } else if (totalScore >= 45) {
  state = 'Watch';
  action = 'watch';
  stateTone = 'amber';
 } else if (totalScore >= 25) {
  state = 'At Risk';
  action = 'reduce';
  stateTone = 'amber';
 } else {
  state = 'Critical';
  action = 'exit';
  stateTone = 'neutral';
 }

 return h('section', {className: 'panel rules-panel'},
  h('div', {className: 'section-head'},
   h('div', null,
    h('h2', null, 'TraderBot strategy rules & lifecycle guide'),
    h('p', {className: 'muted'}, 'Interactive insight into TraderBot rule evaluation, priority hierarchies, health scoring, circuit breakers, and candidate gates.')
   )
  ),
  h('div', {className: 'rules-nav', role: 'tablist'},
   h('button', {type: 'button', className: `rules-tab-btn ${tab === 'lifecycle' ? 'active' : ''}`, onClick: () => setTab('lifecycle')}, '1. Execution Hierarchy'),
   h('button', {type: 'button', className: `rules-tab-btn ${tab === 'health' ? 'active' : ''}`, onClick: () => setTab('health')}, '2. Health Triad (45/35/20)'),
   h('button', {type: 'button', className: `rules-tab-btn ${tab === 'breakers' ? 'active' : ''}`, onClick: () => setTab('breakers')}, '3. Hard Circuit Breakers'),
   h('button', {type: 'button', className: `rules-tab-btn ${tab === 'entry' ? 'active' : ''}`, onClick: () => setTab('entry')}, '4. Entry Models & Gates'),
   h('button', {type: 'button', className: `rules-tab-btn ${tab === 'simulator' ? 'active' : ''}`, onClick: () => setTab('simulator')}, '5. Live Rule Simulator')
  ),
  tab === 'lifecycle' && h('div', null,
   h('p', {className: 'muted'}, 'All broker operations are strictly serialized through one execution gateway. Intent arbitration follows an immutable priority order, ensuring risk containment before growth:'),
   h('div', {className: 'lifecycle-flow'},
    [
     ['1', 'Reconcile broker state', 'priority-1', 'Reconcile open orders, fills, broker-held positions, and buying power before taking any actions.'],
     ['2', 'Restore protection', 'priority-1', 'Broker-held stop must cover 100% of open shares. If stop is missing or undersized, orders are placed immediately.'],
     ['3', 'Exit', 'priority-1', 'Emergency exit triggered by -8% catastrophic loss, intended stop hit, or Critical health state.'],
     ['4', 'Reduce', 'priority-2', 'Hard reduction triggered by -6% entry-relative loss: one-time market order to trim 50% of position and freeze future adds.'],
     ['5', 'Hold', 'priority-3', 'Position remains within Healthy (80-100), Stable (65-79), or Watch (45-64) bounds; no order mutation.'],
     ['6', 'Add', 'priority-4', 'Health >= 80, fresh setup >= 85, price >= entry, RR >= 1.5, favorable QQQ regime. Maximum 1 add per episode (up to 50% of initial shares).'],
     ['7', 'Enter', 'priority-4', 'Flat symbol with active signal, setup score >= 80 (or 85), and all hard safety gates (spread, liquidity, quote age) cleared.']
    ].map(([num, title, cls, desc]) => h('div', {key: num, className: `lifecycle-step ${cls}`},
     h('span', {className: 'lifecycle-num'}, num),
     h('div', null, h('b', null, title), h('p', {className: 'muted'}, desc))
    ))
   )
  ),
  tab === 'health' && h('div', null,
   h('p', {className: 'muted'}, 'Position Health is a pure evaluation of an open holding. It replaces entry setup scoring the instant the first share fills:'),
   h('div', {className: 'rule-formula'}, 'Health Score = (0.45 * Downside Score) + (0.35 * 60m Trend Score) + (0.20 * Remaining R:R Score)'),
   h('div', {className: 'rules-grid'},
    h('div', {className: 'rule-card'},
     h('h3', null, 'Downside State (45% Weight)', pill('45 pts', 'green')),
     h('p', null, 'Measures return relative to average entry price: (current / entry - 1). Piecewise linear decay:'),
     h('div', {className: 'rule-formula'}, '>= 0%: 100 pts | -3%: 70 pts | -6%: 30 pts | -8%: 0 pts'),
     h('p', {className: 'muted'}, 'Interpolated linearly between milestones. Protects capital against prolonged drawdowns.')
    ),
    h('div', {className: 'rule-card'},
     h('h3', null, '60-min Trend Health (35% Weight)', pill('35 pts', 'neutral')),
     h('p', null, 'Evaluates holding timeframe completed 60-minute bar indicators:'),
     h('div', {className: 'rule-formula'}, 'Price > EMA21 (25) + EMA9 > EMA21 (20) + EMA21 Slope > 0 (20) + Price > EMA50 (15) + RS vs QQQ (10) + QQQ Regime (10) = 100'),
     h('p', {className: 'muted'}, 'Requires positive slope and moving average stacking to sustain holding status.')
    ),
    h('div', {className: 'rule-card'},
     h('h3', null, 'Remaining Reward/Risk (20% Weight)', pill('20 pts', 'amber')),
     h('p', null, 'Evaluates remaining payoff from current price to immutable entry target versus distance to active stop:'),
     h('div', {className: 'rule-formula'}, 'R = (Target - Price) / (Price - Stop) | Score = clamp(50 * R, 0, 100)'),
     h('p', {className: 'muted'}, 'An R of 2.0 or higher scores a full 100 points. As target nears, score naturally decreases.')
    )
   ),
   h('div', {className: 'summary-strip'},
    h('span', null, pill('Healthy (80-100)', 'green'), ' Hold; eligible for discretionary adds'),
    h('span', null, pill('Stable (65-79)', 'neutral'), ' Hold; healthy trend, no adds'),
    h('span', null, pill('Watch (45-64)', 'amber'), ' Hold with closer monitoring'),
    h('span', null, pill('At Risk (25-44)', 'amber'), ' Reduction candidate (50% trim)'),
    h('span', null, pill('Critical (0-24)', 'neutral'), ' Exit candidate (liquidate)')
   )
  ),
  tab === 'breakers' && h('div', null,
   h('p', {className: 'muted'}, 'Hard overrides supersede weighted scoring to guarantee capital protection and process safety:'),
   h('div', {className: 'rules-grid'},
    h('div', {className: 'rule-card'},
     h('h3', null, '-6% Hard Reduction (50% Trim)', pill('Automatic Trim', 'amber')),
     h('p', null, 'If entry-relative loss reaches or exceeds 6.0%, a one-time market reduction is immediately submitted for 50% of current quantity.'),
     h('p', {className: 'muted'}, 'All subsequent additions and ladder buys are permanently disabled for the episode.')
    ),
    h('div', {className: 'rule-card'},
     h('h3', null, '-8% Catastrophic Stop', pill('Emergency Exit', 'neutral')),
     h('p', null, 'If entry-relative loss reaches 8.0%, an emergency liquidation order is sent. Backed by broker-held protective stop order.'),
     h('p', {className: 'muted'}, 'Independent of software state; protects against flash market crashes.')
    ),
    h('div', {className: 'rule-card'},
     h('h3', null, 'Structural Trend Failure Override', pill('Score Cap', 'amber')),
     h('p', null, 'If Price drops below EMA21 and EMA50 while EMA21 slope is negative, health score is automatically capped below 45 (Watch score).'),
     h('p', {className: 'muted'}, 'Forces position into At Risk state regardless of initial reward/risk.')
    ),
    h('div', {className: 'rule-card'},
     h('h3', null, 'Stop Coverage Invariant', pill('Unprotected', 'neutral')),
     h('p', null, 'Every open share must be matched by a valid broker stop order. If broker stop is missing or undersized:'),
     h('p', {className: 'muted'}, 'Status is marked Unprotected, additions are blocked, and intent is set to restore_protection.')
    )
   )
  ),
  tab === 'entry' && h('div', null,
   h('p', {className: 'muted'}, 'Candidates are evaluated on 5-minute bars using a 6-factor weighted model and hard safety gates:'),
   h('div', {className: 'rules-grid'},
    h('div', {className: 'rule-card'},
     h('h3', null, 'Weighted Factor Score (0-100)', pill('Min 80/85 pts', 'green')),
     h('p', null, 'Normalized factor contributions:'),
     h('div', {className: 'rule-formula'}, 'Price Action (25%) + Stock Trend (20%) + Market/Regime (15%) + Liquidity (15%) + R:R Geometry (15%) + Rel Strength (10%)'),
     h('p', {className: 'muted'}, 'Requires >= 80 to qualify for entry; additions require fresh setup >= 85.')
    ),
    h('div', {className: 'rule-card'},
     h('h3', null, 'Breakout Model ATR Mechanics', pill('Chase Limit', 'neutral')),
     h('p', null, 'Breakout requires completed close > previous 20-bar resistance:'),
     h('div', {className: 'rule-formula'}, 'Distance = (Close - Resistance) / ATR14 | Full credit: 0.50 ATR | Chase limit: 1.50 ATR'),
     h('p', {className: 'muted'}, 'Distance > 1.50 ATR is strictly blocked. EMA21 slope must be >= 0.20 ATR units.')
    ),
    h('div', {className: 'rule-card'},
     h('h3', null, 'Hard Safety Blockers', pill('Pre-Trade Gates', 'amber')),
     h('div', {className: 'rule-chip-list'},
      pill('Spread <= 0.50%', 'green'), pill('Quote Age Fresh', 'green'), pill('Intraday Volume Met', 'green'),
      pill('No Same-Day Loss Reentry', 'neutral'), pill('Reentry Ledger Cap Valid', 'neutral')
     ),
     h('p', {className: 'muted'}, 'Execution ask cannot exceed chase limit. Limit prices are never raised to chase asks.')
    )
   )
  ),
  tab === 'simulator' && h('div', {className: 'simulator-box'},
   h('h3', null, 'Live Health & Circuit Breaker Simulator'),
   h('p', {className: 'muted'}, 'Adjust parameters below to see how TraderBot calculates Health Score, classifies States, and triggers hard Circuit Breakers in real time:'),
   h('div', {className: 'sim-controls'},
    h('div', {className: 'sim-control'},
     h('label', null, h('span', null, 'Entry-relative Return:'), h('b', null, pct(simReturn))),
     h('input', {type: 'range', min: -10, max: 15, step: 0.5, value: simReturn, onChange: e => setSimReturn(Number(e.target.value))})
    ),
    h('div', {className: 'sim-control'},
     h('label', null, h('span', null, '60-min Trend Score:'), h('b', null, `${simTrend} / 100`)),
     h('input', {type: 'range', min: 0, max: 100, step: 5, value: simTrend, onChange: e => setSimTrend(Number(e.target.value))})
    ),
    h('div', {className: 'sim-control'},
     h('label', null, h('span', null, 'Remaining Reward/Risk (R):'), h('b', null, `${Number(simRR).toFixed(2)} R`)),
     h('input', {type: 'range', min: 0, max: 3, step: 0.1, value: simRR, onChange: e => setSimRR(Number(e.target.value))})
    ),
    h('div', {className: 'sim-control'},
     h('label', null, h('span', null, 'Broker Stop Order Active:'), h('b', null, simStop ? 'Covered' : 'Missing')),
     h('label', {className: 'muted'}, h('input', {type: 'checkbox', checked: simStop, onChange: e => setSimStop(e.target.checked)}), ' Stop covers 100% of open shares')
    )
   ),
   h('div', {className: 'sim-result'},
    h('div', {className: 'sim-stat'}, h('span', null, 'Downside Score'), h('strong', null, `${dsScore}/100`)),
    h('div', {className: 'sim-stat'}, h('span', null, 'Calculated Health'), h('strong', null, `${totalScore}/100`)),
    h('div', {className: 'sim-stat'}, h('span', null, 'Active State'), pill(state, stateTone)),
    h('div', {className: 'sim-stat'}, h('span', null, 'Recommended Action'), h('strong', null, human(action)))
   ),
   overrideMsg && h('p', {className: 'amber-text', role: 'alert'}, overrideMsg)
  )
 );
}
function AddStockModal({onClose, onStockAdded}) {
 const [symbol,setSymbol]=React.useState('');
 const [group,setGroup]=React.useState('candidate');
 const [archetype,setArchetype]=React.useState('default');
 const [archetypes,setArchetypes]=React.useState([]);
 const [skipValidation,setSkipValidation]=React.useState(false);
 const [inspecting,setInspecting]=React.useState(false);
 const [submitting,setSubmitting]=React.useState(false);
 const [inspection,setInspection]=React.useState(null);
 const [message,setMessage]=React.useState(null);

 React.useEffect(()=>{
  if(typeof fetch==='undefined')return;
  fetch('/api/universe/archetypes')
   .then(r=>r.json())
   .then(list=>{if(Array.isArray(list))setArchetypes(list);})
   .catch(()=>{});
 },[]);

 const runInspect=React.useCallback(async(sym)=>{
  const clean=sym.trim().toUpperCase();
  if(!clean){setInspection(null);return;}
  setInspecting(true);
  try {
   const r=await fetch('/api/universe/inspect?symbol='+encodeURIComponent(clean));
   const d=await r.json();
   setInspection(d);
  } catch {
   setInspection(null);
  } finally {
   setInspecting(false);
  }
 },[]);

 const handleSymbolChange=e=>{
  const v=e.target.value.toUpperCase();
  setSymbol(v);
  setMessage(null);
  if(v.trim().length>=1){
   runInspect(v);
  } else {
   setInspection(null);
  }
 };

 const handleSubmit=async e=>{
  if(e)e.preventDefault();
  if(!symbol.trim())return;
  setSubmitting(true);
  setMessage(null);
  try {
   const res=await fetch('/api/universe/add',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
     symbol:symbol.trim().toUpperCase(),
     group,
     archetype,
     skip_validation:skipValidation
    })
   });
   const data=await res.json();
   if(res.ok&&data.success){
    setMessage({
     type:'success',
     text:data.message||`Successfully enrolled ${data.symbol} into ${data.group} watchers with archetype '${data.archetype}'.`
    });
    if(onStockAdded)onStockAdded(data);
   } else {
    setMessage({
     type:'error',
     text:data.error||data.message||'Failed to enroll stock into universe.'
    });
   }
  } catch {
   setMessage({type:'error',text:'Network or server error while adding stock.'});
  } finally {
   setSubmitting(false);
  }
 };

 const selectedArchetypeMeta=archetypes.find(a=>a.key===archetype);

 return h('div',{className:'modal-backdrop',onClick:e=>{if(e.target===e.currentTarget)onClose();}},
  h('div',{className:'modal-dialog',role:'dialog','aria-modal':'true','aria-label':'Onboard new stock'},
   h('div',{className:'modal-head'},
    h('div',null,
     h('h2',null,'Onboard new stock'),
     h('p',{className:'muted'},'Add a symbol to TraderBot universe with automated sector ETF resolution and rollout cohort assignment.')
    ),
    h('button',{type:'button',className:'modal-close-btn','aria-label':'Close dialog',onClick:onClose},'✕')
   ),
   message && h('div',{className:`banner-${message.type}`,role:message.type==='error'?'alert':'status'},message.text),
   h('form',{onSubmit:handleSubmit},
    h('div',{className:'form-group'},
     h('label',{className:'form-label',htmlFor:'stock-symbol-input'},
      h('span',null,'Stock ticker symbol'),
      inspecting && h('span',{className:'muted'},'Resolving sector & status...')
     ),
     h('input',{
      id:'stock-symbol-input',
      className:'form-input',
      placeholder:'e.g. NVDA, PLTR, MSFT, AAPL',
      value:symbol,
      onChange:handleSymbolChange,
      maxLength:10,
      autoFocus:true,
      required:true
     }),
     inspection && !inspection.error && h('div',{className:'inspection-badge-row'},
      inspection.sector_etf && pill(`${inspection.sector_etf} · ${inspection.sector_name}`,'neutral'),
      inspection.cohort && pill(`Cohort: ${inspection.cohort}`,'neutral'),
      inspection.is_enrolled
       ? pill(`Already in ${inspection.enrolled_group}_watchers`,'amber')
       : pill('Ready to onboard','green'),
      inspection.broker_status==='active' && pill('Alpaca active & tradable','green')
     ),
     inspection?.error && h('small',{className:'red-text'},inspection.error)
    ),
    h('div',{className:'form-row'},
     h('div',{className:'form-group'},
      h('label',{className:'form-label',htmlFor:'stock-group-select'},'Enrollment group'),
      h('select',{
       id:'stock-group-select',
       className:'form-select',
       value:group,
       onChange:e=>setGroup(e.target.value)
      },
       h('option',{value:'candidate'},'Candidate (new_watchers) — Recommended'),
       h('option',{value:'managed'},'Managed Watcher (managed_watchers)')
      )
     ),
     h('div',{className:'form-group'},
      h('label',{className:'form-label',htmlFor:'stock-archetype-select'},'Strategy & risk archetype'),
      h('select',{
       id:'stock-archetype-select',
       className:'form-select',
       value:archetype,
       onChange:e=>setArchetype(e.target.value)
      },
       archetypes.length ? archetypes.map(a=>h('option',{key:a.key,value:a.key},a.label))
        : [
            h('option',{key:'default',value:'default'},'Default (Balanced)'),
            h('option',{key:'large_cap_tech',value:'large_cap_tech'},'Large Cap Tech / Momentum'),
            h('option',{key:'high_beta_growth',value:'high_beta_growth'},'High Beta Growth'),
            h('option',{key:'defensive_value',value:'defensive_value'},'Defensive Value')
          ]
      )
     )
    ),
    selectedArchetypeMeta && h('div',{className:'archetype-desc-card'},
     h('b',null,selectedArchetypeMeta.label),
     h('p',null,selectedArchetypeMeta.description),
     h('small',null,`Max symbol notional: ${selectedArchetypeMeta.max_notional_percent}% · Initial notional: $${selectedArchetypeMeta.dynamic_entry_notional}`)
    ),
    h('div',{className:'form-group'},
     h('label',{className:'muted'},
      h('input',{
       type:'checkbox',
       checked:skipValidation,
       onChange:e=>setSkipValidation(e.target.checked)
      }),
      ' Skip live broker asset pre-flight verification'
     )
    ),
    h('div',{className:'modal-actions'},
     h('button',{type:'button',className:'btn-secondary',onClick:onClose},'Cancel'),
     h('button',{
      type:'submit',
      className:'btn-primary',
      disabled:submitting || !symbol.trim() || (inspection?.is_enrolled===true)
     },submitting ? 'Enrolling...' : '+ Add stock to universe')
    )
   )
  )
 );
}
function App() {
 const [streaming,setStreaming]=React.useState(false);
 const [mainTab,setMainTab]=React.useState('positions');
 const [showAddStock,setShowAddStock]=React.useState(false);
 const [data,setData]=React.useState(null), [error,setError]=React.useState(false), [loading,setLoading]=React.useState(false), [search,setSearch]=React.useState(''), [view,setView]=React.useState('all');
 const refresh=React.useCallback(async()=>{
   setLoading(true);
   try { const r=await fetch('/api/snapshot',{signal:AbortSignal.timeout(10000)}); if(!r.ok)throw Error(); setData(await r.json());setError(false); }
   catch { setError(true); } finally {setLoading(false);}
 },[]);
 React.useEffect(()=>{refresh();},[refresh]);
 React.useEffect(()=>{
   if(data?.mode!=='live')return;
   let events, lastMessage=Date.now();
   const connect=()=>{
     events?.close();setStreaming(false);lastMessage=Date.now();
     if(!navigator.onLine)return;
     events=new EventSource('/api/events');
     events.onmessage=e=>{try{setData(JSON.parse(e.data));lastMessage=Date.now();setStreaming(true);setError(false);}catch{setStreaming(false);}};
     events.onerror=()=>setStreaming(false);
   };
   const offline=()=>{events?.close();setStreaming(false);};
   window.addEventListener('offline',offline);
   window.addEventListener('online',connect);
   const watchdog=setInterval(()=>{if(Date.now()-lastMessage>15000)connect();},5000);
   connect();
   return ()=>{events?.close();clearInterval(watchdog);window.removeEventListener('offline',offline);window.removeEventListener('online',connect);};
 },[data?.mode]);
 if(!data)return h('main',null,h('h1',null,'TraderBot'),h('p',{role:'status'},error?'Unable to load the saved report. Check that the local dashboard is running.':'Loading dashboard...'),error&&h('button',{onClick:refresh,disabled:loading},'Retry'));
 const live=data.mode==='live';
 const a=data.account, positions=data.positions.filter(p=>p.symbol.toLowerCase().includes(search.toLowerCase()) && (view==='all'||['At Risk','Critical','Unprotected','Unavailable'].includes(p.position_health_state || 'Unavailable')));
 const risk=data.positions.filter(p=>['At Risk','Critical','Unprotected'].includes(p.position_health_state)).length;
 const total=data.positions.reduce((s,p)=>s+Number(p.market_value||0),0);
 const feedCount=(data.feed_comparisons||[]).length;
 const posCount=data.positions.length;
 const ordersCount=(data.orders||[]).length;
 const watchersCount=(data.operations?.watchers||[]).length;
 const candidatesCount=(data.operations?.candidates||[]).length;
 const alertsCount=(data.operations?.alerts||[]).length;
 const activityCount=(data.operations?.activity||[]).length;
 return h('main',{'aria-busy':loading},
  h('header',null,h('div',null,h('div',{className:'eyebrow'},'TRADERBOT / PORTFOLIO REPORT'),h('h1',null,'Portfolio overview'),h('p',{className:'muted'},live?'Broker portfolio updates with independently timestamped health assessments.':'Your portfolio and protection, from the latest saved report.')),
   h('div',{className:'header-status'},
    pill(live?(streaming?'Updates connected':'Reconnecting updates'):'Saved snapshot',live&&!streaming?'amber':'neutral'),
    live && h('button',{type:'button',className:'btn-add-stock',onClick:()=>setShowAddStock(true)},'+ Add stock'),
    h('button',{onClick:refresh,disabled:loading},loading?'Loading...':live?'Refresh view':'Refresh report'))),
  h('div',{className:'notice',role:'status'},error?'Refresh failed. Showing the previously loaded snapshot.':live?`Broker refresh every ${data.refresh_interval_seconds>=60?Math.round(data.refresh_interval_seconds/60)+' minutes':(data.refresh_interval_seconds||20)+' seconds'} | Last account update: ${stamp(data.connection.account.as_of)} | ${data.market.is_open===true?'Market open':data.market.is_open===false?'Market closed':'Market status unavailable'}`:data.warning||`Report date: ${data.report_date} | File saved: ${stamp(data.report_as_of)} | Values are not live.`),
  live && h('section',{className:'panel'},h('h2',null,'Data status'),...Object.entries(data.connection).filter(([key])=>key!=='calendar').map(([key,status])=>h('div',{className:'status-row',key},human(key),pill(status.stale?'Stale / unavailable':'Current',status.stale?'amber':'green'),stamp(status.as_of))),!streaming&&h('p',{className:'amber-text',role:'status'},'Update connection interrupted. Values may be outdated; reconnecting automatically.')),
  h('div',{className:'metrics'},...[
   ['Portfolio equity',money(a.equity),'Broker account value'],['Available cash',money(a.cash),'Cash balance'],['Buying power',money(a.buying_power),'Non-margin buying power'],['Day change',pct(a.day_gain_percent),a.day_gain==null?'Account daily return':money(a.day_gain)]
  ].map(([label,value,sub])=>h('section',{className:'metric',key:label},h('span',null,label),h('strong',null,value),h('small',null,sub)))),
  h('div',{className:'summary-strip'},h('span',null,`${data.positions.length} open positions`),h('span',null,`${money(total)} invested`),h('span',{className:risk?'amber-text':''},`${risk} positions need attention`),h('span',null,live?`Prices fetched: ${stamp(data.connection.positions.as_of)}`:`Report: ${data.report_date || "not available"}`)),
  live && h('div',{className:'main-nav-tabs',role:'tablist','aria-label':'Dashboard sections'},
   h('button',{type:'button',role:'tab','aria-selected':mainTab==='positions',className:`main-nav-tab ${mainTab==='positions'?'active':''}`,onClick:()=>setMainTab('positions')},'Positions & health',posCount>0&&h('span',{className:'tab-badge'},posCount)),
   h('button',{type:'button',role:'tab','aria-selected':mainTab==='orders',className:`main-nav-tab ${mainTab==='orders'?'active':''}`,onClick:()=>setMainTab('orders')},'Orders',ordersCount>0&&h('span',{className:'tab-badge'},ordersCount)),
   h('button',{type:'button',role:'tab','aria-selected':mainTab==='watchers',className:`main-nav-tab ${mainTab==='watchers'?'active':''}`,onClick:()=>setMainTab('watchers')},'Watchers',watchersCount>0&&h('span',{className:'tab-badge'},watchersCount)),
   h('button',{type:'button',role:'tab','aria-selected':mainTab==='candidates',className:`main-nav-tab ${mainTab==='candidates'?'active':''}`,onClick:()=>setMainTab('candidates')},'Candidates',candidatesCount>0&&h('span',{className:'tab-badge'},candidatesCount)),
   h('button',{type:'button',role:'tab','aria-selected':mainTab==='alerts',className:`main-nav-tab ${mainTab==='alerts'?'active':''}`,onClick:()=>setMainTab('alerts')},'Alerts',alertsCount>0&&h('span',{className:`tab-badge ${alertsCount>0?'tab-badge-amber':''}`},alertsCount)),
   h('button',{type:'button',role:'tab','aria-selected':mainTab==='activity',className:`main-nav-tab ${mainTab==='activity'?'active':''}`,onClick:()=>setMainTab('activity')},'Activity',activityCount>0&&h('span',{className:'tab-badge'},activityCount)),
   h('button',{type:'button',role:'tab','aria-selected':mainTab==='feed',className:`main-nav-tab ${mainTab==='feed'?'active':''}`,onClick:()=>setMainTab('feed')},'Feed comparison',feedCount>0&&h('span',{className:'tab-badge'},feedCount)),
   h('button',{type:'button',role:'tab','aria-selected':mainTab==='rules',className:`main-nav-tab ${mainTab==='rules'?'active':''}`,onClick:()=>setMainTab('rules')},'Strategy rules'),
   h('button',{type:'button',role:'tab','aria-selected':mainTab==='all',className:`main-nav-tab ${mainTab==='all'?'active':''}`,onClick:()=>setMainTab('all')},'All tables')
  ),
  (mainTab==='positions'||mainTab==='all'||!live) && h('section',{className:'panel'},
   h('p',{className:'scroll-hint'},'On smaller screens, scroll tables sideways to see all columns. Keyboard: focus a table and use the arrow keys.'),
   h('div',{className:'section-head'},
    h('div',null,
     h('h2',null,'Positions & health'),
     h('p',{className:'muted'},'Health and stop coverage belong to the timestamped assessment. Price updates do not refresh health. No actions execute here.')
    ),
    h('div',{className:'filters'},
     h('input',{'aria-label':'Search positions',placeholder:'Search symbol',value:search,onChange:e=>setSearch(e.target.value)}),
     h('select',{'aria-label':'Filter position health',value:view,onChange:e=>setView(e.target.value)},
      h('option',{value:'all'},'All positions'),
      h('option',{value:'risk'},'Needs attention')
     )
    )
   ),
   table(['Symbol / quantity','Price','Market value','Total P/L','Health','Action','Stop coverage','Health as of'],positions.map(p=>[
    h('div',null,h('b',null,p.symbol),h('small',null,`${p.qty} shares · ${money(p.avg_entry_price)} entry`)),money(p.current_price),money(p.market_value),h('div',{className:Number(p.total_gain_loss)>=0?'green-text':'red-text'},money(p.total_gain_loss),h('small',null,pct(p.total_gain_loss_percent))),
    h('div',null,pill(`${p.position_health_state||'Unavailable'}${p.position_health_score==null?'':` · ${p.position_health_score}`}`,p.position_health_state==='Healthy'?'green':['At Risk','Critical','Unprotected'].includes(p.position_health_state)?'amber':'neutral'),live&&h('small',{className:p.position_health_current?'':'amber-text'},p.position_health_freshness||(p.position_health_as_of?'Assessment overdue':'Assessment missing')),h('details',null,h('summary',null,'Score details'),h('p',null,`Entry setup ${p.entry_setup_score??'n/a'} / Exit pressure ${p.position_health_exit_score??'n/a'}`),h('p',null,`Downside ${p.position_health_downside_score??'—'} / Trend ${p.position_health_trend_score??'—'} / Reward-risk ${p.position_health_reward_risk_score??'—'}`),h('p',null,(p.position_health_reasons||[]).map(healthReason).join(', ')||'No health warnings'),h('p',null,`Data when evaluated: ${p.position_health_data_fresh?'Fresh':'Stale'} / ${p.position_health_data_complete?'Complete':'Incomplete'}`),h(PositionHealthTriadMeter,{position:p}),h(RiskProximityMeter,{position:p}))),
    human(p.position_health_action),h('div',null,money(p.position_health_stop_price),h('small',null,`${p.position_health_stop_qty??0} / ${p.qty} shares`)),h('div',null,`Bar: ${stamp(p.position_health_as_of)}${p.position_health_bar_end?' — '+stamp(p.position_health_bar_end):''}`,live&&h('small',null,`${p.position_health_source||'No assessment'} | Evaluated ${stamp(p.position_health_evaluated_at)}`))
   ]),data.status!=='available'?'Positions unavailable until a valid report is loaded.':data.positions.length?'No positions match this filter.':'This report contains no open positions.')
  ),
  live && (mainTab==='orders'||mainTab==='all') && h(OrdersView,{data}),
  live && data.operations && (['watchers','candidates','alerts','activity','all'].includes(mainTab)) && h(OperationsView,{data:data.operations, section:mainTab, onOpenAddStock:()=>setShowAddStock(true)}),
  live && (mainTab==='feed'||mainTab==='all') && h(FeedComparisonView,{rows:data.feed_comparisons}),
  (mainTab==='rules'||mainTab==='all'||!live) && h(RulesInsightView),
  live && showAddStock && h(AddStockModal,{onClose:()=>setShowAddStock(false),onStockAdded:()=>refresh()}),
  h('footer',null,live?`Read-only | View updates every ${data.stream_interval_seconds||5} seconds | Times shown in your browser time zone`:'Read-only saved report | Refresh reloads the latest report file | All times shown in your browser time zone')
 );
}
ReactDOM.createRoot(document.getElementById('root')).render(h(App));
