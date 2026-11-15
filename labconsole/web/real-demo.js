'use strict';

let demoId = '', demoView = null, demoCatalog = null, demoRequestKey = '', demoRefreshing = false;
let demoStarting = false, demoCommandPending = false;
let demoManualSession='', demoManualMode='';
const demoClearAt=new Map();
function demoDisabled(id,value){const node=$(id),disabled=Boolean(value);if(node.disabled!==disabled)node.disabled=disabled;}
let demoCatalogAt=0,demoCatalogRequest=null,demoCatalogError='',demoHistorySignature='',demoRenderSignature='';
const demoTerminal = new Set(['completed', 'failed', 'stopped', 'interrupted']);
const demoStatuses = {preparing:'准备中', armed:'已就绪，等待T0', running:'运行中', restoring:'正在恢复', completed:'已完成', failed:'门禁停止', stopped:'已停止', interrupted:'控制台曾中断'};
function demoBackendCurrent(){
 return preparationBackendCurrent()&&boot?.capabilities?.includes('unified_demo')&&boot?.capabilities?.includes('throughput_ucb_v2')&&boot?.capabilities?.includes('per_sta_cw')&&boot?.capabilities?.includes('demo_lifecycle_v3')&&boot?.capabilities?.includes('ucb_hold_v3');
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
 demoDisabled('real-demo-initial',Boolean(active||demoStarting));
 for(const n of demoStaNames){
  demoDisabled('real-demo-initial-'+n,active||demoStarting);
  demoDisabled('real-demo-upload-'+n,active||demoStarting);
 }
 demoDisabled('real-demo-history',Boolean(active));
 demoDisabled('real-demo-open',Boolean(active));
 demoDisabled('real-demo-duration',active||demoStarting);
 const resumable=demoView&&demoTerminal.has(demoView.status)&&demoView.restoration?.confirmed;
 demoDisabled('real-demo-resume',!resumable||!entry.id||active||demoStarting);
}

function demoPlot(id, points, timing, series, adaptation, enabledAt, baseline, control) {
 const canvas=$(id), ctx=canvas.getContext('2d'), width=canvas.width, height=canvas.height;
 const left=43, right=width-15, top=13, bottom=height-29;
 const total=timing.warmup_s+timing.measure_s+timing.tail_s;
 const viewStart=demoClearAt.get(demoView?.id)||0;
 const duration=adaptation||control?Math.min(total,Math.max(viewStart+90,Math.ceil((points.at(-1)?.second||0)/30)*30)):total;
 const x=t=>left+(right-left)*(t-viewStart)/Math.max(1,duration-viewStart);
 points=points.filter(p=>p.second>viewStart);
 const values=points.flatMap(p=>series.map(s=>s.value(p))).filter(Number.isFinite);
 const max=Math.max(1,...values)*1.1, y=v=>bottom-(bottom-top)*v/max;
 ctx.clearRect(0,0,width,height);
 const phaseColors={explore:'#eaf2f9',fallback:'#e9f3eb',hold:'#e9f3eb',
                    review:'#eaf2f9',verify:'#eaf2f9',reference:'#fcf2df',switchback:'#fcf2df'};
 for(let k=timing.warmup_s,index=0;timing.segment_s&&k<Math.min(duration,timing.warmup_s+timing.measure_s);k+=timing.segment_s,index++){
  if(k+timing.segment_s<=viewStart)continue;
  const phase=adaptation?.blocks?.[index]?.phase;
  const color=control?.blocks?.[index]?.mode==='auto'?'#e9f3eb':phaseColors[phase];
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
 if(Number.isFinite(baseline)){
  ctx.strokeStyle='#8a601d';ctx.lineWidth=1.5;ctx.setLineDash([6,4]);
  ctx.beginPath();ctx.moveTo(left,y(baseline));ctx.lineTo(right,y(baseline));ctx.stroke();
  ctx.setLineDash([]);
 }
 for(let j=0;j<=4;j++){
  const t=viewStart+(duration-viewStart)*j/4;ctx.textAlign='center';ctx.fillText(`${Math.round(t)}s`,x(t),height-7);
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
 const actions={manual:'手动',adapt:'算法接管',takeback:'接回手动'};
 const markers=control?Object.entries(control.blocks).flatMap(([index,block])=>{
  const time=timing.warmup_s+Number(index)*timing.segment_s;
  const prior=control.blocks[Number(index)-1];
  const changed=block.W!==prior?.W||JSON.stringify(block.per_STA_W)!==JSON.stringify(prior?.per_STA_W);
  const phaseChange=block.hold_status?.phase&&block.hold_status.phase!==prior?.hold_status?.phase;
  const label=block.hold_status?.phase==='hold'?'保持':block.hold_status?'探索':'UCB';
  return (block.action||block.mode==='auto'&&(changed||phaseChange))&&points.at(-1)?.second>=time?[[time,`${actions[block.action]||label} ${demoWindows(block.W,block.per_STA_W)}`]]:[];
 }):[[enabledAt,'启用自适应']];
 for(const [time,label] of markers){
  if(!Number.isFinite(time)||time>duration||time<viewStart)continue;
  const mark=x(time);
  ctx.strokeStyle='#285e90';ctx.lineWidth=1.5;
  ctx.beginPath();ctx.moveTo(mark,top);ctx.lineTo(mark,bottom);ctx.stroke();
  ctx.fillStyle='#234c75';ctx.textAlign=mark>right-115?'right':'left';
  ctx.fillText(label,mark+(mark>right-115?-5:5),top+14);
 }
}

const demoStaNames=['STA1','STA2','STA3','STA4'];
const demoWindowValues=[8,16,32,64,128];
function demoWindowSelect(id,value){
 const select=el('select');select.id=id;
 for(const w of demoWindowValues){const option=el('option',String(w));option.value=String(w);select.append(option);}
 select.value=String(value);return select;
}
for(const n of demoStaNames){
 const tr=el('tr');tr.append(el('td',n));
 const check=el('input');check.type='checkbox';check.checked=true;check.id='real-demo-upload-'+n;check.setAttribute('aria-label',n+'参与上传');
 const cell=el('td');cell.append(check);tr.append(cell);
 const initial=demoWindowSelect('real-demo-initial-'+n,128);initial.setAttribute('aria-label',n+'初始W');
 const cw=el('td');cw.append(initial);tr.append(cw);$('real-demo-initial-stas').append(tr);
 const label=el('label',n+'目标 W');label.append(demoWindowSelect('real-demo-target-'+n,64));$('real-demo-target-stas').append(label);
 initial.onchange=check.onchange=()=>{demoRequestKey='';};
}
function demoPerSta(prefix){return Object.fromEntries(demoStaNames.map(n=>[n,Number($(prefix+n).value)]));}
function demoWindows(w, per) {
 if(w&&typeof w==='object'){per=w;w=null;}
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
 $('real-demo-algorithm-title').textContent=state.revision===3?'算法决策 · UCB探索与稳定保持（W8 / W16）':state.revision===2?'算法决策 · 滑动窗口UCB（W8 / W16）':'算法决策 · 历史滑动窗口UCB';
 const {warmup_s:warm,measure_s:measure,segment_s:step,guard_s:guard}=v.request;
 const latest=points.at(-1)?.second||0;
 const index=Math.min(measure/step-1,Math.max(0,Math.floor((latest-warm)/step)));
 const current=state.blocks[index];
 const mode=commands?.mode||current?.mode;
 if(mode==='manual'&&(demoManualSession!==v.id||demoManualMode!=='manual')){
  const settings=v.points.at(-1)?.per_STA_W||current?.per_STA_W||v.request.initial_per_STA_W;
  if(settings)for(const n of demoStaNames)$('real-demo-target-'+n).value=String(settings[n]);
 }
 demoManualSession=v.id;demoManualMode=mode;
 $('real-demo-manual-controls').hidden=!active;
 demoDisabled('real-demo-apply',demoCommandPending||!commands?.manual);
 for(const n of demoStaNames)demoDisabled('real-demo-target-'+n,demoCommandPending||!commands?.manual);
 $('real-demo-adapt').hidden=mode==='auto';
 demoDisabled('real-demo-adapt',demoCommandPending||!commands?.adapt);
 $('real-demo-takeback').hidden=mode!=='auto';
 demoDisabled('real-demo-takeback',demoCommandPending||!commands?.takeback);
 const pending=commands?.pending;
 const actionNames={manual:'应用手动 W',adapt:'算法接管',takeback:'接回手动'};
 $('real-demo-pending').textContent=pending?`第${pending.segment+1}段将${actionNames[pending.action]}${pending.W!=null?' '+demoWindows(pending.W):''}`
  :mode==='manual'&&!commands?.adapt?'有一段有效吞吐后可交给算法。':'操作在共同分段边界生效。';
 const last=v.points.at(-1);
 $('real-demo-adapt-status').hidden=false;
 const controlLabel=mode==='auto'?'算法控制':'手动控制';
 $('real-demo-adapt-status').textContent=v.status==='restoring'?'正在恢复实验前配置'
  :demoTerminal.has(v.status)?`本轮已结束 · 最后为${controlLabel}`
  :`${controlLabel}${last?` · 当前硬件 ${demoWindows(last.W,last.per_STA_W)}`:''} · 上传集合：${(v.request.active_senders||demoStaNames).join('、')}`;
 const decision=Object.entries(state.blocks).filter(([k,b])=>b.estimates&&warm+Number(k)*step<=latest).at(-1);
 $('real-demo-algorithm').hidden=!decision;
 const table=$('real-demo-estimates');table.replaceChildren();
 if(decision){
  const [k,block]=decision;
  $('real-demo-decision').textContent=`最近 ${state.window_segments} 个自动段 · 第 ${Number(k)+1} 段目标 ${demoWindows(block.W,block.per_STA_W)} · ${block.reason}`;
  if(block.hold_status){
   const h=block.hold_status;
   $('real-demo-decision').textContent=`第 ${Number(k)+1} 段 · ${h.phase==='hold'?'稳定保持':'候选探索'} ${demoWindows(block.W,block.per_STA_W)} · ${block.reason}`+
    (h.reference_Mbps!=null?` · 保持基准 ${anumber(h.reference_Mbps)} Mbps · 下降触发线 ${anumber(h.reference_Mbps*.9)} Mbps · 连续下降 ${h.drop_streak||0}/2 段`:'');
  }
  for(const r of block.estimates){
   const tr=el('tr');
   for(const value of ['W'+r.W,r.count,anumber(r.mean_Mbps),r.count?anumber(r.ucb_Mbps):'待探索'])tr.append(el('td',String(value)));
   table.append(tr);
  }
 }
 $('real-demo-comparison').hidden=true;
 return Object.entries(state.blocks).flatMap(([k,block])=>{
  const idx=Number(k), start=warm+idx*step, stop=start+step;
  if(start>=latest)return [];
  return [{index:idx,W:block.W,per_STA_W:block.per_STA_W,start_s:start,stop_s:stop,phase:block.mode==='manual'?'手动':'算法',
           total_Mbps:demoMean(points,start,stop),stable_Mbps:demoMean(points,start+guard,stop-state.score_tail_s)}];
 });
}

function renderContinuous(v,points,enabledAt) {
 const {activation,selection,blocks,provisional,decisions}=v.adaptation;
 const {warmup_s:warm,measure_s:measure,segment_s:step,guard_s:guard}=v.request;
 const latest=points.at(-1)?.second||0;
 const activeBlock=blocks[Math.max(0,Math.floor((latest-warm)/step))];
 const actualWindows=v.points.at(-1)?.per_STA_W||activeBlock?.W;
 const currentWindows=actualWindows?' · 当前 '+demoWindows(null,actualWindows):'';
 const phases={baseline:'固定基线',explore:'试候选',fallback:'恢复上一档',hold:'保持配置',
               review:'等待参考段',reference:'邻近基线',verify:'复测初选',switchback:'回切固定 W'};
 const status=$('real-demo-adapt-status');
 status.textContent=v.status==='restoring'?'正在恢复实验前配置':demoTerminal.has(v.status)?'本轮已结束':activation
  ? (phases[activeBlock?.phase]||'等待开始')+currentWindows
  : (v.adaptation_control?.message||'固定W运行中')+currentWindows;
 const start=k=>warm+k*step;
 const stable=k=>demoMean(points,start(k)+guard,start(k)+step-(v.adaptation.score_tail_s??1));
 const baseline=activation?activation.baseline_blocks.map(stable):[];
 const holdStart=selection?start(selection.hold_block):null;
 const holdElapsed=holdStart==null?0:Math.max(0,Math.min(latest,warm+measure)-holdStart);
 const completeHold=selection?Object.keys(blocks).map(Number).filter(k=>k>=selection.hold_block&&start(k)+step<=latest).map(stable):[];
 const mean=values=>values.length&&values.every(v=>v!=null)?values.reduce((a,b)=>a+b,0)/values.length:null;
 const baselineMean=mean(baseline), holdMean=mean(completeHold);
 const after=activation?demoMean(points,Math.ceil(enabledAt),Math.min(latest,warm+measure)):null;
 const gain=baselineMean>0&&holdMean!=null?100*(holdMean/baselineMean-1):null;
 const comparison=$('real-demo-comparison'), final=v.analysis?.comparison;
 comparison.hidden=!activation;
 const outcome=selection?(selection.candidate==='baseline'?'保持初始 W':'采用 '+demoWindows(null,selection.W))
  :provisional&&provisional.candidate!=='baseline'?'初选 '+demoWindows(null,v.adaptation.candidates[provisional.candidate])+'，等待回切复核':'正在比较';
 const pair=selection?.evidence;
 const decision=decisions?.at(-1);
 const shortW=key=>demoWindows(v.adaptation.candidates[key]?.STA1,null);
 const pairText=decision?` · ${shortW(decision.candidate)} ${anumber(decision.candidate_Mbps)} vs ${shortW(decision.previous_candidate)} ${anumber(decision.previous_Mbps)} Mbps，${decision.accepted?'保留候选':'恢复 '+shortW(decision.previous_candidate)}`
  :pair?.candidate_Mbps!=null&&pair?.baseline_Mbps!=null?` · 回切复测 ${anumber(pair.candidate_Mbps)} vs ${anumber(pair.baseline_Mbps)} Mbps`:'';
 comparison.textContent=`${outcome}${pairText} · 启用前基线 ${anumber(final?.baseline_stable_Mbps??baselineMean)} Mbps · 保持段 ${anumber(final?.selected_stable_Mbps??holdMean)} Mbps · 同轮阶段差 ${anumber(final?.relative_stage_difference_pct??gain)}% · 启用后含探索 ${anumber(final?.post_activation_Mbps??after)} Mbps${selection&&holdElapsed<v.adaptation.minimum_hold_s?` · 保持观察 ${holdElapsed}/${v.adaptation.minimum_hold_s} 秒`:''}`;
 return Object.entries(blocks).filter(([k])=>start(Number(k))<latest).map(([k,block])=>{
  const index=Number(k), from=start(index),to=from+step;
  return {index,W:null,per_STA_W:block.W,start_s:from,stop_s:to,
          total_Mbps:demoMean(points,from,to),stable_Mbps:stable(index),phase:phases[block.phase]};
 });
}

function renderDemo(v) {
 const changedSession=demoView?.id!==v.id;
 const signature=JSON.stringify(v);
 if(signature===demoRenderSignature)return;
 demoRenderSignature=signature;
 demoView=v;
 const cleared=demoClearAt.get(v.id);
 $('real-demo-clear-note').textContent=cleared?`仅隐藏截至本轮${cleared}秒的曲线；后续采样继续显示。原始数据、分段统计与学习记录保留。`:'清除只影响曲线显示，不删除数据，不重置算法。';
 const active=!demoTerminal.has(v.status)&&v.status!=='restoring';
 const restoration=v.restoration?` · ${v.restoration.confirmed?'恢复已确认':'恢复未确认'}`:'';
 const latest=v.points.at(-1);
 $('real-demo-status').textContent=`${demoStatuses[v.status]||v.status} · ${demoAccessName(v.request.access_mode)} · ${v.phase}${latest?' · '+latest.second+' 秒':''}${restoration}${v.error?' · '+v.error:''}`;
 if(v.air_evidence==='not_collected_MON15_disabled')$('real-demo-status').textContent+=' · MON15未启用，空口旁证未采集';
 demoDisabled('real-demo-stop',!active);
 $('real-demo-stop').title=active?'中止当前演示并恢复本轮配置。':v.status==='restoring'?'正在恢复，请等待结果。':'当前记录已结束，没有正在运行的演示可停止。';
 demoDisabled('real-demo-adapt',!active||!v.adaptation_control?.enabled);
 $('real-demo-adapt').title=v.adaptation_control?.message||'在固定基线完成后启用';
 $('real-demo-adapt-status').hidden=!v.adaptation;
 $('real-demo-data').hidden=false;
 demoDisabled('real-demo-apply',!active||v.request.mode!=='manual'||v.events.filter(e=>e.kind==='stage_plan').length>=2);
 $('real-demo-manual-controls').hidden=true;
 $('real-demo-algorithm').hidden=true;
 $('real-demo-pending').textContent=v.manual_pending?`W${v.manual_pending.W}已提交，等待第${v.manual_pending.segment+1}段`:$('real-demo-apply').disabled?'后续测量段已封存，不能继续修改。':'提前提交下一段W；在下一段边界生效。';
 $('real-demo-apply').title=$('real-demo-pending').textContent;
 if(changedSession&&[...$('real-demo-history').options].some(o=>o.value===v.id))$('real-demo-history').value=v.id;
 $('real-demo-export').hidden=false;
 $('real-demo-export').href='/api/demo/export?id='+encodeURIComponent(v.id);
 if(v.request.mode==='pilot'){
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
 const enabledAt=v.adaptation?.activation?(v.adaptation.activation.at_ns-v.T0_ns)/1e9:null;
 const recent=new Map(), history=[];
 for(const p of points){
  if(history.length&&p.second!==history.at(-1).second+1)history.length=0;
  if(!Number.isFinite(p.total_Mbps)){history.length=0;continue;}
  history.push(p);
  if(history.length>10)history.shift();
  if(history.length===10)recent.set(p.second,history.reduce((a,q)=>a+q.total_Mbps,0)/10);
 }
 const liveCurve=v.adaptation||v.control;
 const series=[{label:'聚合吞吐（每秒）',color:liveCurve?'#758799':'#1b2836',width:liveCurve?1.1:2.6,value:p=>p.total_Mbps},
  ...demoStaNames.map(n=>({label:n,color:staColors[n],width:1.4,value:p=>p.per_STA_Mbps?.[n]}))];
 if(liveCurve)series.push({label:'聚合吞吐（10秒滑动均值）',color:'#192631',width:2.6,value:p=>recent.get(p.second)});
 const baseline=v.analysis?.comparison?.baseline_stable_Mbps??v.adaptation?.activation?.baseline_Mbps;
 const legend=$('real-demo-legend');
 const keys=Number.isFinite(baseline)?[...series,{label:'启用前固定 W 基线',color:'#8a601d',dashed:true}]:series;
 if(changedSession||legend.children.length!==keys.length)legend.replaceChildren(...keys.map(line=>{
  const item=el('li'), swatch=el('span');
  swatch.style.borderColor=line.color;
  if(line.dashed)swatch.style.borderTopStyle='dashed';
  swatch.setAttribute('aria-hidden','true');
  item.append(swatch,line.label);return item;
 }));
 $('real-demo-chart-note').textContent=v.control?'白底为手动控制，绿底为算法控制，灰带为段首保护期。竖线标出手动应用和控制权切换的分段边界。':v.adaptation?.policy==='sequential'
  ?'蓝底是试候选，绿底是恢复或保持已采用的 W，灰带是段首保护期。评分取保护期后的12秒。'
  :v.adaptation?.policy==='switchback'?'蓝底是试候选及复测，浅黄是固定 W 对照，绿底是保持段。评分用段首保护期后的12秒；启用后总均值包含探索和保护期。'
  :v.adaptation?'蓝底为探索和复测，绿底为保持段，灰带为段首保护期。':'灰带为切换保护期，吞吐仍计入全程。';
 demoPlot('real-demo-rate',points,v.request,series,v.adaptation,enabledAt,baseline,v.control);
 const body=$('real-demo-segments');body.replaceChildren();
 let segments=v.analysis?.segments||[];
 if(v.control){
  segments=renderUnified(v,points,active);
 }else if(v.adaptation){
  segments=renderContinuous(v,points,enabledAt);
 }else{
  const comparison=v.analysis?.comparison;
  $('real-demo-comparison').hidden=!comparison;
  if(comparison)$('real-demo-comparison').textContent=`W${comparison.baseline_W}基线 ${anumber(comparison.baseline_stable_Mbps)} Mbps · 在线稳定段 ${anumber(comparison.selected_stable_Mbps)} Mbps · 含探索的全测量 ${anumber(comparison.whole_measurement_Mbps)} Mbps · 同轮阶段差 ${anumber(comparison.relative_stage_difference_pct)}%`;
 }
 for(const s of segments){
  const tr=el('tr');
  for(const value of [s.phase?`${s.index+1} · ${s.phase}`:s.index+1,demoWindows(s.W,s.per_STA_W),`${s.start_s}–${s.stop_s}`,anumber(s.total_Mbps),anumber(s.stable_Mbps)])tr.append(el('td',String(value)));
  body.append(tr);
 }
 $('real-demo-events').textContent=v.events.filter(e=>!['sample','eof'].includes(e.kind)).map(e=>{
  const sec=v.T0_ns?(e.receipt?.wall_ns-e.receipt?.planned_ns)/1e6:null;
  const read=e.receipt?.readback;
  const access=e.receipt?.settings;
  return `${e.node||'控制机'} ${e.kind} ${e.message||''}${e.W?' '+(typeof e.W==='object'?demoWindows(null,e.W):'W'+e.W):''}${read?' '+read.hw_cwmin+'/'+read.hw_cwmax:''}${access?' '+demoAccessName(e.receipt.access_mode)+'，RTS 阈值 '+access.rts+'，Basic override '+access.override:''}${Number.isFinite(sec)?' 读回相对计划 '+sec.toFixed(1)+'ms':''}`;
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
     const option=el('option',`${row.id.slice(0,8)} · ${demoModeName(row.request.mode)} · ${demoAccessName(row.request.access_mode)} · ${demoStatuses[row.status]||row.status}`);
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
function demoModeName(mode){return {unified:'手动与算法演示',continuous:'持续自适应演示',pilot:'切换检查',manual:'手动演示',auto:'自动调整',fixed:'固定对照'}[mode]||mode;}
function demoAccessName(mode){return mode==='rts'?'RTS/CTS':'Basic';}

$('real-demo-initial').onchange=()=>{demoRequestKey='';for(const n of demoStaNames)$('real-demo-initial-'+n).value=$('real-demo-initial').value;};
$('real-demo-duration').onchange=()=>{demoRequestKey='';};
$('real-demo-clear').onclick=()=>{if(!demoView)return;demoClearAt.set(demoView.id,demoView.points.at(-1)?.second||0);demoRenderSignature='';renderDemo(demoView);};
$('real-demo-show-all').onclick=()=>{if(!demoView)return;demoClearAt.delete(demoView.id);demoRenderSignature='';renderDemo(demoView);};
$('real-demo-resume').onclick=()=>guard(async()=>{
 if(!demoView?.restoration?.confirmed||!demoTerminal.has(demoView.status))throw new Error('请等待本轮停止并确认恢复。');
 const settings=demoView.points.at(-1)?.per_STA_W||demoView.request.initial_per_STA_W||Object.fromEntries(demoStaNames.map(n=>[n,demoView.request.initial_w]));
 for(const n of demoStaNames){$('real-demo-initial-'+n).value=String(settings[n]);$('real-demo-upload-'+n).checked=(demoView.request.active_senders||demoStaNames).includes(n);}
 $('real-demo-access').value=demoView.request.access_mode||'basic';demoRequestKey='';
 await $('real-demo-start').onclick();
});
$('real-demo-access').onchange=()=>{demoRequestKey='';demoControls();};
$('real-demo-open').onclick=()=>guard(async()=>{demoId=$('real-demo-history').value;await refreshDemo(true);});
$('real-demo-start').onclick=()=>guard(async()=>{
 const initial_w=Number($('real-demo-initial').value);
 const initial_per_STA_W=demoPerSta('real-demo-initial-');
 const active_senders=demoStaNames.filter(n=>$('real-demo-upload-'+n).checked);
 if(!active_senders.length)throw new Error('请至少选择一台上传STA。');
 const access_mode=$('real-demo-access').value;
 if(!window.confirm(`使用 ${demoAccessName(access_mode)} 启动 ${active_senders.join('、')} 的 UDP 业务？初始：${demoWindows(initial_per_STA_W)}。本轮上传集合和接入机制固定。`))return;
 demoStarting=true;demoControls();
 if(!demoRequestKey)demoRequestKey=crypto.randomUUID();
 try{
  await refreshDemoCatalog(true);
  const entry=demoEntry();
  if(!entry.id||entry.active)throw new Error(entry.message);
  const request={mode:'unified',initial_w,initial_per_STA_W,active_senders,measure_s:Number($('real-demo-duration').value),request_key:demoRequestKey,preparation_id:entry.id,authorize:true};
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
   if(action==='manual')request.W=demoView?.request.mode==='unified'?demoPerSta('real-demo-target-'):Number($('real-demo-target-STA1').value);
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
