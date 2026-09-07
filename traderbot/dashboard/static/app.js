const h = React.createElement;
let printDetails = [];
window.addEventListener('beforeprint',()=>{printDetails=Array.from(document.querySelectorAll('details:not([open])'));printDetails.forEach(detail=>detail.open=true);});
window.addEventListener('afterprint',()=>{printDetails.forEach(detail=>detail.open=false);printDetails=[];});
const money = v => v == null ? '—' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(Number(v));
const pct = v => v == null ? '—' : `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`;
const stamp = v => v ? new Date(v).toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}) : 'Not available';
const human = v => String(v || 'Unknown').replaceAll('_',' ');
function pill(text, tone='neutral') { return h('span',{className:`pill ${tone}`},text); }
function table(headers, rows, empty) {return h('div',{className:'table-wrap',tabIndex:0,role:'region','aria-label':`${headers.join(', ')} table; scroll horizontally for more columns`},h('table',null,h('thead',null,h('tr',null,...headers.map(x=>h('th',{key:x,scope:'col'},x)))),h('tbody',null,...(rows.length ? rows.map((row,i)=>h('tr',{key:i},...row.map((cell,j)=>h('td',{key:j},cell)))) : [h('tr',{key:'empty'},h('td',{colSpan:headers.length,className:'empty'},empty))]))));}
function OperationsView({data}) {
 const [symbol,setSymbol]=React.useState(''),[attention,setAttention]=React.useState(false);
 const [showAll,setShowAll]=React.useState(false);
 const limit=showAll?undefined:10;
 const matches=row=>row.symbol.toLowerCase().includes(symbol.trim().toLowerCase());
 return h('section',null,
  h('section',{className:'panel'},h('div',{className:'section-head'},h('h2',null,'Watcher operations'),h('input',{'aria-label':'Filter operations by symbol',placeholder:'Filter by symbol',value:symbol,onChange:e=>setSymbol(e.target.value)})),
   h('p',{className:'muted'},`Files checked: ${stamp(data.as_of)} | Supervisor process: ${data.process_status}`),h('p',{className:'muted'},data.note),
   h('div',{className:'summary-strip'},...Object.entries(data.counts).map(([key,count])=>h('span',{key,className:['Overdue','Recent errors','Source unavailable'].includes(key)&&count?'amber-text':''},`${count} ${key.toLowerCase()}`))),
   h('div',{className:'filters'},h('label',{className:'muted'},h('input',{type:'checkbox',checked:attention,onChange:e=>setAttention(e.target.checked)}),' Show watchers needing attention'),h('label',{className:'muted'},h('input',{type:'checkbox',checked:showAll,onChange:e=>setShowAll(e.target.checked)}),' Show all operational rows')),
   h('p',{className:'muted'},'Tables show up to 10 rows initially. Filter by symbol or show all rows to inspect more.'),
   table(['Symbol','Activity status','Last activity','Next expected'],data.watchers.filter(matches).filter(w=>!attention||!['Within schedule','Disabled'].includes(w.status)).slice(0,limit).map(w=>[w.symbol,pill(w.status,w.status==='Within schedule'?'green':w.status==='Disabled'?'neutral':'amber'),stamp(w.last_activity),stamp(w.next_expected)]),'No watchers match, or watcher configuration is unavailable.'),
   data.issues.length>0&&h('details',null,h('summary',null,`${data.issues.length} source issues`),...data.issues.map((issue,i)=>h('p',{key:i},`${issue.source}: ${issue.message}`)))),
  h('section',{className:'panel'},h('h2',null,'Entry candidates'),h('p',{className:'muted'},'Recorded setup assessments for configured candidates not currently held. Scores do not imply an order will be placed.'),
   table(['Symbol','Score','Model','Status','Assessment time','Source'],data.candidates.filter(matches).slice(0,limit).map(c=>[c.symbol,c.score??'—',human(c.model),h('div',null,human(c.status),h('details',null,h('summary',null,'Blockers'),h('p',null,c.blockers.map(human).join(', ')||'No recorded blockers'))),stamp(c.as_of),c.source_status]),'No candidate assessments match this filter.')),
  h('section',{className:'panel'},h('h2',null,'Alert history'),table(['Time','Symbol','Source','Event','Detail'],data.alerts.filter(matches).slice(0,limit).map(a=>[stamp(a.timestamp),a.symbol,a.source,pill(human(a.kind),'amber'),human(a.detail)]),'No alerts in the recent log window. Missing sources are listed above.')),
  h('section',{className:'panel'},h('h2',null,'Recent watcher activity'),table(['Time','Symbol','Event','Failures','Next run'],data.activity.filter(matches).slice(0,limit).map(a=>[stamp(a.timestamp),a.symbol,human(a.status),a.failures??'—',a.next_run_seconds==null?'—':`${a.next_run_seconds}s`]),'No activity in the recent log window.'))
 );
}
function OrdersView({data}) {
 const [symbol,setSymbol]=React.useState(''),[status,setStatus]=React.useState('all');
 const orders=data.orders.filter(o=>(o.symbol||'').toLowerCase().includes(symbol.trim().toLowerCase())&&(status==='all'||o.status===status));
 return h('section',{className:'panel'},h('div',{className:'section-head'},h('h2',null,'Recent orders'),h('div',{className:'filters'},h('input',{'aria-label':'Filter orders by symbol',value:symbol,placeholder:'Order symbol',onChange:e=>setSymbol(e.target.value)}),h('select',{'aria-label':'Filter order status',value:status,onChange:e=>setStatus(e.target.value)},h('option',{value:'all'},'All statuses'),...Array.from(new Set(data.orders.map(o=>o.status).filter(Boolean))).map(s=>h('option',{key:s,value:s},human(s)))))),
  h('p',{className:'muted'},`Latest 20 orders | Fetched ${stamp(data.connection.orders.as_of)} | ${data.connection.orders.stale?'Stale / unavailable':'Current'}`),
  table(['Submitted','Symbol','Side','Quantity','Filled','Status'],orders.map(o=>[stamp(o.submitted_at),o.symbol,human(o.side),o.qty,o.filled_qty,pill(human(o.status))]),data.connection.orders.stale?'Orders unavailable or outdated.':'No recent orders match this filter.'));
}
function App() {
 const [streaming,setStreaming]=React.useState(false);
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
 return h('main',{'aria-busy':loading},
  h('header',null,h('div',null,h('div',{className:'eyebrow'},'TRADERBOT / PORTFOLIO REPORT'),h('h1',null,'Portfolio overview'),h('p',{className:'muted'},live?'Broker portfolio updates with independently timestamped health assessments.':'Your portfolio and protection, from the latest saved report.')),
   h('div',{className:'header-status'},pill(live?(streaming?'Updates connected':'Reconnecting updates'):'Saved snapshot',live&&!streaming?'amber':'neutral'),h('button',{onClick:refresh,disabled:loading},loading?'Loading...':live?'Refresh view':'Refresh report'))),
  h('div',{className:'notice',role:'status'},error?'Refresh failed. Showing the previously loaded snapshot.':live?`Broker refresh every 20 seconds | Last account update: ${stamp(data.connection.account.as_of)} | ${data.market.is_open===true?'Market open':data.market.is_open===false?'Market closed':'Market status unavailable'}`:data.warning||`Report date: ${data.report_date} | File saved: ${stamp(data.report_as_of)} | Values are not live.`),
  live && h('section',{className:'panel'},h('h2',null,'Data status'),...Object.entries(data.connection).filter(([key])=>key!=='calendar').map(([key,status])=>h('div',{className:'status-row',key},human(key),pill(status.stale?'Stale / unavailable':'Current',status.stale?'amber':'green'),stamp(status.as_of))),!streaming&&h('p',{className:'amber-text',role:'status'},'Update connection interrupted. Values may be outdated; reconnecting automatically.')),
  h('div',{className:'metrics'},...[
   ['Portfolio equity',money(a.equity),'Broker account value'],['Available cash',money(a.cash),'Cash balance'],['Buying power',money(a.buying_power),'Non-margin buying power'],['Day change',pct(a.day_gain_percent),a.day_gain==null?'Account daily return':money(a.day_gain)]
  ].map(([label,value,sub])=>h('section',{className:'metric',key:label},h('span',null,label),h('strong',null,value),h('small',null,sub)))),
  h('div',{className:'summary-strip'},h('span',null,`${data.positions.length} open positions`),h('span',null,`${money(total)} invested`),h('span',{className:risk?'amber-text':''},`${risk} positions need attention`),h('span',null,live?`Prices fetched: ${stamp(data.connection.positions.as_of)}`:`Report: ${data.report_date || "not available"}`)),
  h('section',{className:'panel'},h('p',{className:'scroll-hint'},'On smaller screens, scroll tables sideways to see all columns. Keyboard: focus a table and use the arrow keys.'),h('div',{className:'section-head'},h('div',null,h('h2',null,'Positions & health'),h('p',{className:'muted'},'Health and stop coverage belong to the timestamped assessment. Price updates do not refresh health. No actions execute here.')),
    h('div',{className:'filters'},h('input',{'aria-label':'Search positions',placeholder:'Search symbol',value:search,onChange:e=>setSearch(e.target.value)}),h('select',{'aria-label':'Filter position health',value:view,onChange:e=>setView(e.target.value)},h('option',{value:'all'},'All positions'),h('option',{value:'risk'},'Needs attention')))),
   table(['Symbol / quantity','Price','Market value','Total P/L','Health','Action','Stop coverage','Health as of'],positions.map(p=>[
    h('div',null,h('b',null,p.symbol),h('small',null,`${p.qty} shares · ${money(p.avg_entry_price)} entry`)),money(p.current_price),money(p.market_value),h('div',{className:Number(p.total_gain_loss)>=0?'green-text':'red-text'},money(p.total_gain_loss),h('small',null,pct(p.total_gain_loss_percent))),
    h('div',null,pill(`${p.position_health_state||'Unavailable'}${p.position_health_score==null?'':` · ${p.position_health_score}`}`,p.position_health_state==='Healthy'?'green':['At Risk','Critical','Unprotected'].includes(p.position_health_state)?'amber':'neutral'),live&&h('small',{className:p.position_health_current?'':'amber-text'},p.position_health_current?'Bar current':'Stale / missing assessment'),h('details',null,h('summary',null,'Score details'),h('p',null,`Downside ${p.position_health_downside_score??'—'} / Trend ${p.position_health_trend_score??'—'} / Reward-risk ${p.position_health_reward_risk_score??'—'}`),h('p',null,(p.position_health_reasons||[]).map(human).join(', ')||'No health warnings'),h('p',null,`At assessment time: ${p.position_health_data_fresh?'Fresh':'Stale'} / ${p.position_health_data_complete?'Complete':'Incomplete'}`))),
    human(p.position_health_action),h('div',null,money(p.position_health_stop_price),h('small',null,`${p.position_health_stop_qty??0} / ${p.qty} shares`)),h('div',null,stamp(p.position_health_as_of),live&&h('small',null,`${p.position_health_source||'No assessment'} | Evaluated ${stamp(p.position_health_evaluated_at)}`))
   ]),data.status!=='available'?'Positions unavailable until a valid report is loaded.':data.positions.length?'No positions match this filter.':'This report contains no open positions.')),
  live&&h(OrdersView,{data}),
  live&&data.operations&&h(OperationsView,{data:data.operations}),
  h('footer',null,live?'Read-only | Shared broker refresh every 20 seconds | Updates every 5 seconds | Times shown in your browser time zone':'Read-only saved report | Refresh reloads the latest report file | All times shown in your browser time zone')
 );
}
ReactDOM.createRoot(document.getElementById('root')).render(h(App));
