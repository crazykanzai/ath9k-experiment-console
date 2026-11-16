'use strict';

let demoId = '', demoView = null, demoCatalog = null, demoRequestKey = '', demoRefreshing = false;
let demoStarting = false, demoCommandPending = false;
let demoManualSession='', demoManualMode='';
const demoClearAt=new Map();
const demoPausedTime=new Map();
let paintDemoChart=null;
function demoDisabled(id,value){const node=$(id),disabled=Boolean(value);if(node.disabled!==disabled)node.disabled=disabled;}
let demoCatalogAt=0,demoCatalogRequest=null,demoCatalogError='',demoHistorySignature='',demoRenderSignature='';
const demoTerminal = new Set(['completed', 'failed', 'stopped', 'interrupted']);
const demoStatuses = {preparing:'准备中', armed:'已就绪，等待T0', running:'运行中', restoring:'正在恢复', completed:'已完成', failed:'门禁停止', stopped:'已停止', interrupted:'控制台曾中断'};
function demoBackendCurrent(){
 const capabilities=boot?.capabilities;
 return preparationBackendCurrent()&&capabilities?.includes('unified_demo')&&capabilities.includes('demo_lifecycle_v3')&&capabilities.includes('per_sta_cw_pairs')&&capabilities.includes('auto_cw_profile')&&capabilities.includes('ucb_same_w_drop');
}
function demoUpgradeMessage(cleanup){
 return '当前后台尚未加载新版演示。'+(cleanup?'请先在环境准备页结束使用并清理，然后':'请')+
  '退出原控制台终端，再运行 start_console.bat。';
}
function demoEntry(){
 const status=demoCatalog?.preparation_status;
 const recovery=demoCatalog?.preparation_recovery_required;
 const active=Boolean(demoCatalog?.demo_busy??demoCatalog?.sessions?.some(r=>!demoTerminal.has(r.status)));
 if(boot&&!demoBackendCurrent()){
  const cleanup=Boolean(recovery||(status&&!['ended','failed_clean'].includes(status)));
  return {message:demoUpgradeMessage(cleanup),active,prepare:cleanup};
 }
 if(active)return {id:demoCatalog.ready_preparation,message:'本轮正在运行或恢复，请等待结束。',active};
 if(recovery)return {message:'请返回环境准备，核对并清理中断会话。',prepare:true};
 if(demoCatalog?.ready_preparation)return {id:demoCatalog.ready_preparation,message:'环境准备已就绪，可在同一次准备中继续启动演示。'};
 if(status==='ready')return {message:'正在等待后台时钟复核。复核通过后可直接启动。'};
 if(!demoCatalog)return {message:demoCatalogError?'无法核对演示状态，正在重试。':'正在核对当前准备会话。'};
 return {message:'请在环境准备页继续完成设备准备与时钟检查。',prepare:true};
}
function demoControls() {
 const entry=demoEntry();
 const active=entry.active;
 const access = $('real-demo-access');
 const unsupportedAccess=demoBackendCurrent()&&access.value==='rts'&&!boot.capabilities.includes('demo_access_modes');
 const reason=unsupportedAccess&&!active?'当前后台尚未加载 RTS/CTS。请选择 Basic，或清理会话并重启控制台。':entry.message;
 demoDisabled('real-demo-start',!entry.id || unsupportedAccess || active || demoStarting);
 $('real-demo-start').title=reason;
 $('real-demo-ready').textContent=reason;
 $('real-demo-prepare-action').hidden=!entry.prepare||active;
 $('real-demo-prepare-action').querySelector('button').textContent=demoBackendCurrent()?'返回环境准备':'前往环境准备清理会话';
 demoDisabled('real-demo-access',active||demoStarting);
 for(const field of demoCwFields)demoDisabled('real-demo-initial-'+field,Boolean(active||demoStarting));
 for(const n of demoStaNames){
  for(const field of demoCwFields)demoDisabled(`real-demo-initial-${n}-${field}`,active||demoStarting);
  demoDisabled('real-demo-upload-'+n,active||demoStarting);
 }
 demoDisabled('real-demo-history',Boolean(active));
 demoDisabled('real-demo-open',Boolean(active));
 demoDisabled('real-demo-duration',active||demoStarting);
 const resumable=demoView&&demoTerminal.has(demoView.status)&&demoView.restoration?.confirmed;
 demoDisabled('real-demo-resume',!resumable||!entry.id||active||demoStarting);
}

function demoTimeWindow(sessionId, latest) {
 const span=Number($('real-demo-time-span').value);
 const cutoff=demoClearAt.get(sessionId)||0;
 const rightmost=Math.max(cutoff,latest-span);
 const paused=demoPausedTime.has(sessionId);
 const start=paused?Math.max(cutoff,Math.min(rightmost,demoPausedTime.get(sessionId))):rightmost;
 if(paused)demoPausedTime.set(sessionId,start);
 const stop=start+span;
 const slider=$('real-demo-time-start');
 if(Number(slider.min)!==cutoff)slider.min=String(cutoff);
 if(Number(slider.max)!==rightmost)slider.max=String(rightmost);
 if(Number(slider.value)!==start)slider.value=String(start);
 const label=`${start}–${stop} 秒`;
 slider.setAttribute('aria-valuetext',label);
 $('real-demo-time-range').textContent=`${label} · ${paused?'查看历史':'跟随最新'}`;
 demoDisabled('real-demo-time-start',rightmost===cutoff);
 demoDisabled('real-demo-time-follow',!paused);
 return {start,stop,cutoff};
}

function demoPlot(id, points, timing, series, timeline, viewport) {
 const canvas=$(id), ctx=canvas.getContext('2d'), width=canvas.width, height=canvas.height;
 const left=43, right=width-15, top=13, bottom=height-29;
 const {start:viewStart,stop:duration,cutoff}=viewport;
 const x=t=>left+(right-left)*(t-viewStart)/(duration-viewStart);
 points=points.filter(p=>p.second>cutoff&&p.second>=viewStart&&p.second<=duration);
 const values=points.flatMap(p=>series.map(s=>s.value(p))).filter(Number.isFinite);
 const max=Math.max(1,...values)*1.1, y=v=>bottom-(bottom-top)*v/max;
 ctx.clearRect(0,0,width,height);
 for(let k=timing.warmup_s,index=0;timing.segment_s&&k<Math.min(duration,timing.warmup_s+timing.measure_s);k+=timing.segment_s,index++){
  if(k+timing.segment_s<=viewStart)continue;
  const color=timeline?.color(index);
  if(color){
   ctx.fillStyle=color;
   ctx.fillRect(x(Math.max(k,viewStart)),top,x(Math.min(k+timing.segment_s,duration))-x(Math.max(k,viewStart)),bottom-top);
  }
  ctx.fillStyle='#e1e6eb';
  if(k+timing.guard_s>viewStart)ctx.fillRect(x(Math.max(k,viewStart)),top,x(Math.min(k+timing.guard_s,duration))-x(Math.max(k,viewStart)),bottom-top);
 }
 ctx.font='12px Segoe UI, sans-serif';
 for(let j=0;j<=4;j++){
  const value=max*j/4, yy=y(value);
  ctx.strokeStyle='#d6dde3';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();
  ctx.textAlign='right';ctx.fillStyle='#536273';ctx.fillText(value.toFixed(max<=10?1:0),left-5,yy+4);
 }
 const divisions=duration-viewStart===30?6:4;
 for(let j=0;j<=divisions;j++){
  const t=viewStart+(duration-viewStart)*j/divisions;ctx.textAlign='center';ctx.fillText(`${t}s`,x(t),height-7);
 }
 for(const line of series){
  let prior=null;
  ctx.strokeStyle=line.color;ctx.fillStyle=line.color;ctx.lineWidth=line.width||1.7;
  for(const p of points){
   const v=line.value(p);
   if(!Number.isFinite(v)){prior=null;continue;}
   const xx=x(p.second), yy=y(v);
   if(prior&&p.second===prior.second+1){ctx.beginPath();ctx.moveTo(prior.x,prior.y);ctx.lineTo(xx,yy);ctx.stroke();}
   else{ctx.beginPath();ctx.arc(xx,yy,2,0,2*Math.PI);ctx.fill();}
   prior={x:xx,y:yy,second:p.second};
  }
 }
 let labelRight=left-6;
 for(const [time,label] of timeline?.markers||[]){
  if(!Number.isFinite(time)||time>duration||time<viewStart||(cutoff>0&&time<=cutoff))continue;
  const mark=x(time);
  ctx.strokeStyle='#285e90';ctx.lineWidth=1.5;
  ctx.beginPath();ctx.moveTo(mark,top);ctx.lineTo(mark,bottom);ctx.stroke();
  const textWidth=ctx.measureText(label).width;
  const labelLeft=Math.min(mark+5,right-textWidth);
  if(labelLeft<labelRight+6)continue;
  ctx.fillStyle='#234c75';ctx.textAlign='left';ctx.fillText(label,labelLeft,top+14);
  labelRight=labelLeft+textWidth;
 }
}

function demoControlTimeline(timing,control,latest){
 const actions={manual:'手动',adapt:'算法接管',takeback:'接回手动'};
 const markers=Object.entries(control.blocks).flatMap(([index,block])=>{
  const time=timing.warmup_s+Number(index)*timing.segment_s;
  if(latest<time)return [];
  const prior=control.blocks[Number(index)-1];
  const changed=block.W!==prior?.W||JSON.stringify(block.per_STA_CW||block.per_STA_W)!==JSON.stringify(prior?.per_STA_CW||prior?.per_STA_W);
  const phase=block.hold_status?.phase;
  const phaseChanged=phase&&phase!==prior?.hold_status?.phase;
  const automatic=block.mode==='auto';
  if(!block.action&&!(automatic&&(changed||phaseChanged)))return [];
  const windowLabel=demoWindows(block.W,block.per_STA_W,block.per_STA_CW);
  const action=actions[block.action]||(automatic&&control.revision<6?'UCB':'');
  const label=automatic&&control.revision>=6?windowLabel:`${action} ${windowLabel}`.trim();
  return [[time,label]];
 });
 return {
  markers,
  color:index=>control.blocks[index]?.mode==='auto'?'#e9f3eb':null,
  note:'白底为手动控制，绿底为算法控制，灰带为段首保护期。竖线标出手动应用和控制权切换的分段边界。'
 };
}

const demoStaNames=['STA1','STA2','STA3','STA4'];
const demoCwFields=['cwmin','cwmax'];
const demoCwNames={cwmin:'CWmin',cwmax:'CWmax'};
const demoCwDefaults={cwmin:127,cwmax:1023};
function demoCwSelect(id,field,description){
 const select=el('select');select.id=id;
 select.setAttribute('aria-label',description+' '+demoCwNames[field]);
 for(let k=1;k<=10;k++){
  const value=String(2**k-1), option=el('option',value);option.value=value;select.append(option);
 }
 select.value=String(demoCwDefaults[field]);return select;
}
for(const field of demoCwFields){
 const label=el('label','统一初始 '+demoCwNames[field]);
 const input=demoCwSelect('real-demo-initial-'+field,field,'统一初始');
 input.onchange=()=>{demoRequestKey='';for(const n of demoStaNames)$(`real-demo-initial-${n}-${field}`).value=input.value;};
 label.append(input);$('real-demo-initial-field').append(label);
}
for(const n of demoStaNames){
 const tr=el('tr');tr.append(el('td',n));
 const check=el('input');check.type='checkbox';check.checked=true;check.id='real-demo-upload-'+n;check.setAttribute('aria-label',n+'参与上传');
 const cell=el('td');cell.append(check);tr.append(cell);
 const target=el('div');target.className='cw-pair-grid';
 for(const field of demoCwFields){
  const initial=demoCwSelect(`real-demo-initial-${n}-${field}`,field,n+' 初始');
  initial.onchange=()=>{demoRequestKey='';};
  const cw=el('td');cw.append(initial);tr.append(cw);
  const label=el('label',n+' '+demoCwNames[field]);
  label.append(demoCwSelect(`real-demo-target-${n}-${field}`,field,n+' 目标'));target.append(label);
 }
 $('real-demo-initial-stas').append(tr);$('real-demo-target-stas').append(target);
 check.onchange=()=>{demoRequestKey='';};
}
function demoPerStaCw(prefix){
 return Object.fromEntries(demoStaNames.map(n=>{
  const cw=Object.fromEntries(demoCwFields.map(field=>[field,Number($(`${prefix}${n}-${field}`).value)]));
  if(cw.cwmin>cw.cwmax)throw new Error(`${n} 的 CWmin 不能大于 CWmax`);
  return [n,cw];
 }));
}
function demoFillCw(prefix, pairs, windows){
 for(const n of demoStaNames){
  const cw=pairs?.[n]||(windows?.[n]!=null?{cwmin:windows[n]-1,cwmax:windows[n]*8-1}:null);
  if(cw)for(const field of demoCwFields)$(`${prefix}${n}-${field}`).value=String(cw[field]);
 }
}
function demoWindows(w, per, pairs) {
 if(w&&typeof w==='object'){per=w;w=null;}
 if(pairs){
  const values=demoStaNames.map(n=>`${pairs[n].cwmin}/${pairs[n].cwmax}`);
  return values.every(value=>value===values[0])?'CW '+values[0]:demoStaNames.map((n,i)=>`${n} ${values[i]}`).join(' · ');
 }
 if(w!=null)return 'W'+w;
 return per?demoStaNames.map(n=>n+' W'+per[n]).join(' / '):'NA';
}

function demoMean(points, start, stop) {
 if(stop<=start)return null;
 const valid=points.filter(p=>start<p.second&&p.second<=stop&&Number.isFinite(p.total_Mbps));
 return valid.length===stop-start?valid.reduce((sum,p)=>sum+p.total_Mbps,0)/valid.length:null;
}

function renderUnified(v,points,active) {
 const state=v.control, commands=v.control_actions;
 $('real-demo-algorithm-title').textContent=state.revision===7
  ?demoHoldTitle(state.arms)
  :'算法决策 · 历史记录';
 const {warmup_s:warm,measure_s:measure,segment_s:step,guard_s:guard}=v.request;
 const latest=points.at(-1)?.second||0;
 const index=Math.min(measure/step-1,Math.max(0,Math.floor((latest-warm)/step)));
 const current=state.blocks[index];
 const mode=commands?.mode||current?.mode;
 if(mode==='manual'&&(demoManualSession!==v.id||demoManualMode!=='manual')){
  const last=v.points.at(-1);
  const settings=current?.per_STA_W?current:last?.per_STA_W?last:null;
  demoFillCw('real-demo-target-',settings?settings.per_STA_CW:v.request.initial_per_STA_CW,
             settings?.per_STA_W||v.request.initial_per_STA_W);
 }
 demoManualSession=v.id;demoManualMode=mode;
 $('real-demo-manual-controls').hidden=!active;
 demoDisabled('real-demo-apply',demoCommandPending||!commands?.manual);
 for(const n of demoStaNames)for(const field of demoCwFields)demoDisabled(`real-demo-target-${n}-${field}`,demoCommandPending||!commands?.manual);
 $('real-demo-adapt').hidden=mode==='auto';
 demoDisabled('real-demo-adapt',demoCommandPending||!commands?.adapt);
 $('real-demo-takeback').hidden=mode!=='auto';
 demoDisabled('real-demo-takeback',demoCommandPending||!commands?.takeback);
 const pending=commands?.pending;
 const actionNames={manual:'应用手动 CW',adapt:'算法接管',takeback:'接回手动'};
 $('real-demo-pending').textContent=pending?`第${pending.segment+1}段将${actionNames[pending.action]}${pending.CW||pending.W!=null?' '+demoWindows(pending.W,null,pending.CW):''}`
  :mode==='manual'&&!commands?.adapt?'有一段有效吞吐后可交给算法。':'操作在共同分段边界生效。';
 const last=v.points.at(-1);
 $('real-demo-adapt-status').hidden=false;
 const controlLabel=mode==='auto'?'算法控制':'手动控制';
 $('real-demo-adapt-status').textContent=v.status==='restoring'?'正在恢复实验前配置'
  :demoTerminal.has(v.status)?`本轮已结束 · 最后为${controlLabel}`
  :`${controlLabel}${last?` · 当前硬件 ${demoWindows(last.W,last.per_STA_W,last.per_STA_CW)}`:''} · 上传集合：${(v.request.active_senders||demoStaNames).join('、')}`;
 const decision=Object.entries(state.blocks).filter(([k,b])=>b.estimates&&warm+Number(k)*step<=latest).at(-1);
 $('real-demo-algorithm').hidden=!decision;
 const table=$('real-demo-estimates');table.replaceChildren();
 if(decision){
  const [k,block]=decision;
  const index=Number(k)+1;
  const setting=demoWindows(block.W,block.per_STA_W,block.per_STA_CW);
  $('real-demo-decision').textContent=state.revision===7
   ?demoCurrentDecision(state,block,index,setting)
   :demoTargetDecision(state,block,index,setting);
  for(const r of block.estimates){
   const tr=el('tr');
   for(const value of [demoWindows(r.W,r.per_STA_W),r.count,anumber(r.mean_Mbps),r.count?anumber(r.ucb_Mbps):'待探索',r.status||'—'])tr.append(el('td',String(value)));
   table.append(tr);
  }
 }
 return Object.entries(state.blocks).flatMap(([k,block])=>{
  const idx=Number(k), start=warm+idx*step, stop=start+step;
  if(start>=latest)return [];
  return [{index:idx,W:block.W,per_STA_W:block.per_STA_W,per_STA_CW:block.per_STA_CW,start_s:start,stop_s:stop,phase:block.mode==='manual'?'手动':'算法',
           total_Mbps:demoMean(points,start,stop),stable_Mbps:demoMean(points,start+guard,stop-state.score_tail_s)}];
 });
}

function demoHoldTitle(arms){
 return '算法决策 · UCB探索与稳定保持（'+arms.map(w=>'W'+w).join(' / ')+'）';
}

function demoHoldDecision(index,block,setting,details){
 const phase=block.hold_status.phase==='hold'?'稳定保持':'候选探索';
 return '第 '+index+' 段 · '+phase+' '+setting+' · '+block.reason+details;
}

function demoTargetDecision(state,block,index,setting){
 const lookback=state.window_segments!=null?`最近 ${state.window_segments} 个自动段 · `:'';
 return `${lookback}第 ${index} 段目标 ${setting} · ${block.reason}`;
}

function demoCurrentDecision(state,block,index,setting){
 const hold=block.hold_status;
 if(!hold)return demoTargetDecision(state,block,index,setting);
 const c=hold.hold_comparison;
 const details=c?' · '+demoWindows(c.W)+' 同档比较 '+anumber(c.previous_Mbps)+' → '+anumber(c.observed_Mbps)+' Mbps · 触发线 '+anumber(c.threshold_Mbps)+' Mbps'
  :hold.phase==='hold'?' · 最近有效同档段 '+anumber(hold.last_same_w_Mbps)+' Mbps':'';
 return demoHoldDecision(index,block,setting,details);
}

function renderDemo(v) {
 const changedSession=demoView?.id!==v.id;
 const signature=JSON.stringify(v);
 if(signature===demoRenderSignature)return;
 demoRenderSignature=signature;
 demoView=v;
 const cleared=demoClearAt.get(v.id);
 $('real-demo-clear-note').textContent=cleared?`仅隐藏截至本轮${cleared}秒的曲线；后续采样继续显示。原始数据、分段统计与学习记录保留。`:'';
 const active=!demoTerminal.has(v.status)&&v.status!=='restoring';
 const restoration=v.restoration?` · ${v.restoration.confirmed?'恢复已确认':'恢复未确认'}`:'';
 const latest=v.points.at(-1);
 $('real-demo-status').textContent=`${demoStatuses[v.status]||v.status} · ${demoAccessName(v.request.access_mode)} · ${v.phase}${latest?' · '+latest.second+' 秒':''}${restoration}${v.error?' · '+v.error:''}`;
 demoDisabled('real-demo-stop',!active);
 $('real-demo-stop').title=active?'中止当前演示并恢复本轮配置。':v.status==='restoring'?'正在恢复，请等待结果。':'当前记录已结束，没有正在运行的演示可停止。';
 $('real-demo-data').hidden=false;
 if(!v.control){
  $('real-demo-adapt-status').hidden=true;
  $('real-demo-manual-controls').hidden=true;
  $('real-demo-algorithm').hidden=true;
 }
 if(changedSession&&[...$('real-demo-history').options].some(o=>o.value===v.id))$('real-demo-history').value=v.id;
 $('real-demo-export').hidden=false;
 $('real-demo-export').href='/api/demo/export?id='+encodeURIComponent(v.id);
 if(v.request.mode==='pilot'){
  paintDemoChart=null;
  $('real-demo-data').hidden=true;
  return;
 }
 const points=v.points.map(p=>({...p}));
 if(v.analysis?.ap_payload_bytes_by_second){
  const known=new Map(points.map(p=>[p.second,p]));
  const bytes=v.analysis.ap_payload_bytes_by_second;
  points.length=0;
  bytes.STA1.forEach((_,k)=>{
   const per=Object.fromEntries(Object.keys(bytes).map(n=>[n,bytes[n][k]*8/1e6]));
   points.push({...known.get(k+1),second:k+1,per_STA_Mbps:per,total_Mbps:Object.values(per).reduce((a,b)=>a+b,0)});
  });
 }
 const recent=new Map(), history=[];
 for(const p of points){
  if(history.length&&p.second!==history.at(-1).second+1)history.length=0;
  if(!Number.isFinite(p.total_Mbps)){history.length=0;continue;}
  history.push(p);
  if(history.length>10)history.shift();
  if(history.length===10)recent.set(p.second,history.reduce((a,q)=>a+q.total_Mbps,0)/10);
 }
 const showSmoothed=Boolean(v.adaptation||v.control);
 const series=[{label:'聚合吞吐（每秒）',color:showSmoothed?'#758799':'#1b2836',width:showSmoothed?1.1:2.6,value:p=>p.total_Mbps},
  ...demoStaNames.map(n=>({label:n,color:staColors[n],width:1.4,value:p=>p.per_STA_Mbps?.[n]}))];
 if(showSmoothed)series.push({label:'聚合吞吐（10秒滑动均值）',color:'#192631',width:2.6,value:p=>recent.get(p.second)});
 const timeline=v.control
  ?demoControlTimeline(v.request,v.control,points.at(-1)?.second||0)
  :null;
 const legend=$('real-demo-legend');
 if(changedSession||legend.children.length!==series.length)legend.replaceChildren(...series.map(line=>{
  const item=el('li'), swatch=el('span');
  swatch.style.borderColor=line.color;
  swatch.setAttribute('aria-hidden','true');
  item.append(swatch,line.label);return item;
 }));
 $('real-demo-chart-note').textContent=timeline?.note||'灰带为段首保护期。';
 paintDemoChart=()=>{
  const viewport=demoTimeWindow(v.id,points.at(-1)?.second||0);
  demoPlot('real-demo-rate',points,v.request,series,timeline,viewport);
 };
 paintDemoChart();
 const body=$('real-demo-segments');body.replaceChildren();
 const segments=v.control
  ?renderUnified(v,points,active)
  :v.analysis?.segments||[];
 for(const s of segments){
  const tr=el('tr');
  for(const value of [s.phase?`${s.index+1} · ${s.phase}`:s.index+1,demoWindows(s.W,s.per_STA_W,s.per_STA_CW),`${s.start_s}–${s.stop_s}`,anumber(s.total_Mbps),anumber(s.stable_Mbps)])tr.append(el('td',String(value)));
  body.append(tr);
 }
 $('real-demo-events').textContent=v.events.filter(e=>!['sample','eof'].includes(e.kind)).map(e=>{
  const sec=v.T0_ns?(e.receipt?.wall_ns-e.receipt?.planned_ns)/1e6:null;
  const read=e.receipt?.readback;
  const access=e.receipt?.settings;
  const block=v.control?.blocks[e.index??e.receipt?.index];
  const automatic=v.control?.revision>=6&&block?.mode==='auto'&&Number.isInteger(block.W);
  const expected=automatic?demoWindows(block.W):e.CW?demoWindows(null,null,e.CW):e.W?demoWindows(e.W):'';
  return `${e.node||'控制机'} ${e.kind} ${e.message||''}${expected?' '+expected:''}${read&&!automatic?' '+read.hw_cwmin+'/'+read.hw_cwmax:''}${access?' '+demoAccessName(e.receipt.access_mode)+'，RTS 阈值 '+access.rts+'，Basic override '+access.override:''}${Number.isFinite(sec)?' 读回相对计划 '+sec.toFixed(1)+'ms':''}`;
 }).join('\n');
}

async function refreshDemoCatalog(force=false){
 if(!demoCatalogRequest&&(force||!demoCatalog||Date.now()-demoCatalogAt>=3000)){
  demoCatalogRequest=api('/api/demo/list').then(value=>{
   demoCatalog=value;demoCatalogAt=Date.now();demoCatalogError='';
  }).catch(error=>{
   demoCatalog=null;demoCatalogError=error.message;throw error;
  }).finally(()=>{demoCatalogRequest=null;});
 }
 await demoCatalogRequest;
}
async function refreshDemo(force=false) {
 if(demoRefreshing)return;
 demoRefreshing=true;
 try{
  const previous=demoCatalog;
  await refreshDemoCatalog(force);
  if(force||previous!==demoCatalog||!demoHistorySignature){
   const active=demoCatalog.sessions.find(v=>!demoTerminal.has(v.status));
   if(active)demoId=active.id;
   else if(!demoId)demoId=demoCatalog.sessions.find(v=>v.request.mode!=='pilot')?.id||'';
   const signature=JSON.stringify(demoCatalog.sessions.map(r=>[r.id,r.status,r.request.mode]));
   const select=$('real-demo-history'),old=select.value;
   if(signature!==demoHistorySignature){
    demoHistorySignature=signature;
    select.replaceChildren(el('option','选择演示记录'));select.options[0].value='';
    for(const row of demoCatalog.sessions){
     const kind=row.request.mode==='unified'?'手动与算法演示':'旧版演示';
     const option=el('option',`${row.id.slice(0,8)} · ${kind} · ${demoAccessName(row.request.access_mode)} · ${demoStatuses[row.status]||row.status}`);
     option.value=row.id;select.append(option);
    }
    select.value=demoId||old;
   }
  }
  demoControls();
  if(demoId&&(force||demoView?.id!==demoId||!demoTerminal.has(demoView?.status)))renderDemo(await api('/api/demo?id='+encodeURIComponent(demoId)));
 }finally{demoRefreshing=false;}
}
function updateDemoPage(force=false){
 demoControls();
 refreshDemo(force).catch(e=>{
  demoControls();
  demoDisabled('real-demo-start',true);
  $('real-demo-status').textContent='无法取得现场状态：'+e.message;
 });
}
function demoAccessName(mode){return mode==='rts'?'RTS/CTS':'Basic';}

$('real-demo-duration').onchange=()=>{demoRequestKey='';};
$('real-demo-time-span').onchange=()=>paintDemoChart?.();
$('real-demo-time-start').oninput=e=>{
 if(!demoView)return;
 demoPausedTime.set(demoView.id,Number(e.target.value));paintDemoChart?.();
};
$('real-demo-time-follow').onclick=()=>{
 if(!demoView)return;
 demoPausedTime.delete(demoView.id);paintDemoChart?.();
};
$('real-demo-clear').onclick=()=>{if(!demoView)return;demoClearAt.set(demoView.id,demoView.points.at(-1)?.second||0);demoPausedTime.delete(demoView.id);demoRenderSignature='';renderDemo(demoView);};
$('real-demo-show-all').onclick=()=>{if(!demoView)return;demoClearAt.delete(demoView.id);demoPausedTime.set(demoView.id,0);demoRenderSignature='';renderDemo(demoView);};
$('real-demo-resume').onclick=()=>guard(async()=>{
 if(!demoView?.restoration?.confirmed||!demoTerminal.has(demoView.status))throw new Error('请等待本轮停止并确认恢复。');
 const last=demoView.points.at(-1), previous=demoView.request;
 demoFillCw('real-demo-initial-',last?.per_STA_CW||(!last?.per_STA_W?previous.initial_per_STA_CW:null),
            last?.per_STA_W||previous.initial_per_STA_W||Object.fromEntries(demoStaNames.map(n=>[n,previous.initial_w])));
 for(const n of demoStaNames)$('real-demo-upload-'+n).checked=(previous.active_senders||demoStaNames).includes(n);
 $('real-demo-access').value=demoView.request.access_mode||'basic';demoRequestKey='';
 await $('real-demo-start').onclick();
});
$('real-demo-access').onchange=()=>{demoRequestKey='';demoControls();};
$('real-demo-open').onclick=()=>guard(async()=>{demoId=$('real-demo-history').value;await refreshDemo(true);});
$('real-demo-start').onclick=()=>guard(async()=>{
 const initial_per_STA_CW=demoPerStaCw('real-demo-initial-');
 const active_senders=demoStaNames.filter(n=>$('real-demo-upload-'+n).checked);
 if(!active_senders.length)throw new Error('请至少选择一台上传STA。');
 const access_mode=$('real-demo-access').value;
 if(!window.confirm(`使用 ${demoAccessName(access_mode)} 启动 ${active_senders.join('、')} 的 UDP 业务？初始 ${demoWindows(null,null,initial_per_STA_CW)}。本轮上传集合和接入机制固定。`))return;
 demoStarting=true;demoControls();
 if(!demoRequestKey)demoRequestKey=crypto.randomUUID();
 try{
  await refreshDemoCatalog(true);
  const entry=demoEntry();
  if(!entry.id||entry.active)throw new Error(entry.message);
  const request={mode:'unified',initial_per_STA_CW,active_senders,measure_s:Number($('real-demo-duration').value),request_key:demoRequestKey,preparation_id:entry.id,authorize:true};
  if(boot?.capabilities?.includes('demo_access_modes'))request.access_mode=access_mode;
  const value=await api('/api/demo/start',request);
  demoId=value.id;demoRequestKey='';renderDemo(value);
 }finally{demoStarting=false;await refreshDemo(true);demoControls();}
});

for(const [id,action] of [['real-demo-stop','stop'],['real-demo-apply','manual'],['real-demo-adapt','adapt'],['real-demo-takeback','takeback']]){
 $(id).onclick=()=>guard(async()=>{
  if(demoCommandPending&&action!=='stop')return;
  demoCommandPending=true;
  $(id).disabled=true;
  try{
   const request={id:demoId,action};
   if(action==='manual')request.CW=demoPerStaCw('real-demo-target-');
   const value=await api('/api/demo/command',request);
   demoCommandPending=false;demoRenderSignature='';renderDemo(value);
  }finally{demoCommandPending=false;demoRenderSignature='';await refreshDemo(true);}
 });
}

function initRealDemo(){
 setInterval(()=>{
  if(!document.hidden&&!$('real-demo').hidden)updateDemoPage();
 },1000);
}
