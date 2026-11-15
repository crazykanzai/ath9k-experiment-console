'use strict';
let preparationID='',preparationData=null,preparationFetching=false,preparationKey='',preparationStoppingID='';
let preparationRecoveryID='',preparationEnteringDemo=false;
const preparationNames={checking:'正在连接',checked:'设备已连接',restoring_radio:'正在准备设备',radio_ready:'设备准备完成',syncing:'正在同步时间',holding:'正在检查时间稳定性',ready:'环境准备就绪',stopping:'正在结束使用',cleaning:'正在清理',ended:'使用已结束',failed_clean:'准备未通过，已结束',recovery_required:'需要核对中断记录'};
const preparationMessages={checking:'正在读取设备状态。',checked:'可以点击“准备设备”，应用已登记的实验配置。',restoring_radio:'正在配置实验网络并检查设备。',radio_ready:'请点击“同步时间并检查”。',syncing:'正在建立时间同步。',holding:'正在检查时间同步是否稳定，请等待进度完成。',ready:'请点击“进入现场演示”，并保持控制台运行。同一次准备可连续使用多轮。',stopping:'正在结束使用并清理。',cleaning:'正在恢复本次时间设置。',ended:'本次使用已结束；再次演示请重新连接设备并完成准备。',failed_clean:'请查看诊断记录中的具体原因。',recovery_required:'请在上方输入设备密码，点击“核对并清理中断会话”；完成前不能重新连接或开始演示。'};
function renderPreparationRecovery(){
 const needed=Boolean(preparationRecoveryID);
 $('prepare-recovery-actions').hidden=!needed;$('prepare-recover').disabled=!needed;
 if(needed){
  $('prepare-connect').disabled=true;$('prepare-restore').disabled=true;$('prepare-sync').disabled=true;
  $('prepare-connect').title='先核对并清理中断会话，不能重复新建连接。';
  const failure=preparationData?.id===preparationRecoveryID
    ?preparationData.error||preparationData.cleanup?.errors?.[0]:'';
  $('prepare-recovery-help').textContent=failure
    ?`会话 ${preparationRecoveryID.slice(0,8)} 核对未通过。${preparationErrorMessage(failure)}`
    :`会话 ${preparationRecoveryID.slice(0,8)} 待核对。请在上方输入设备登录密码，再点击“核对并清理中断会话”。`;
 }else $('prepare-connect').title='连接设备并新建准备会话。';
}
function preparationErrorMessage(error){
 if(!error)return '';
 if(error.includes('timed out'))return '设备连接超时，请检查有线连接和设备电源。';
 if(error.includes('Authentication failed'))return '设备登录失败，请核对登录密码。';
 if(error.includes('AP role/SSID/business IP not ready'))return preparationBackendCurrent()
  ?'AP 未启用实验无线网络或缺少业务 IP。请查看当前设备读回。'
  :'旧后台要求 AP 已启用实验无线网络并配置业务 IP。请关闭旧控制台终端并重新启动，再核对中断会话。';
 if(error.includes('device is not available')||error.includes('wireless device readiness timeout'))return `${error.match(/^(AP|STA[1-4]|MON15):/)?.[1]||'实验设备'}的无线接口尚未就绪，请查看诊断记录。`;
 if(error.includes('10013'))return '控制机授时端口不可用，请查看诊断记录和时间同步方式。';
 return error;
}
function preparationBackendCurrent(){return boot?.preparation_policy?.revision>=3;}
function renderPreparationTimingHelp(v){
 const timing=boot?.preparation_policy;
 const record=v?.timing;
 const help=$('prepare-timing-help');
 if(!preparationBackendCurrent()){
  help.textContent=v?.status==='recovery_required'
    ?'检测到旧后台。若旧版无法完成恢复，请先关闭控制台终端，再重新启动并核对中断会话。'
    :'请结束使用并确认清理，再重启控制台以加载新版流程。';
 }else if(record?.hold_s>0){
  help.textContent=`所选历史记录使用${record.settle_s}秒同步等待和${record.hold_s}秒稳定性观察。新会话在同步核验通过后直接就绪。`;
 }else{
  help.textContent=`配置授时后等待${timing.settle_s}秒，核验通过即可演示。后台每${timing.monitor_s}秒检查一次时钟。`;
 }
}
function renderPreparationProgress(v){
 const settle=v.timing.settle_s,hold=v.timing.hold_s,total=settle+hold;
 const running=['syncing','holding','ready'].includes(v.status)&&preparationStoppingID!==v.id;
 const settled=Math.min(settle,Math.max(0,v.completed_settle_s??(['holding','ready'].includes(v.status)?settle:0)));
 const observed=Math.min(hold,Math.max(0,v.observed_hold_s??v.completed_hold_s??0));
 $('prepare-progress').max=total;
 $('prepare-progress').value=running?settled+observed:0;
 $('prepare-readiness').textContent=preparationStoppingID===v.id&&['syncing','holding','ready'].includes(v.status)?'正在结束使用并清理。':v.preparation_ready?'设备与时间检查已通过，后台继续监测。':v.status==='holding'?`时间检查 ${settled+observed}/${total} 秒 · 稳定性观察 ${observed}/${hold} 秒（已核验 ${v.completed_hold_s}/${hold} 秒）`:v.status==='syncing'?(v.stage==='settling'?`时间检查 ${settled}/${total} 秒 · 同步等待 ${settled}/${settle} 秒`:`正在配置授时，随后等待${settle}秒并核验时钟。`):preparationMessages[v.status]||'';
}
function preparationDemoReady(v){
 return v?.status==='ready'&&v.preparation_ready&&preparationStoppingID!==v.id;
}
function renderPreparationDemoEntry(){
 const v=preparationData;
 const ready=preparationDemoReady(v);
 const entry=demoEntry();
 const canEnter=ready&&entry.id===v.id;
 $('prepare-ready-actions').hidden=!canEnter;
 $('prepare-enter-demo').disabled=!canEnter||preparationEnteringDemo;
 if(!ready)return;
 $('prepare-badge').textContent=canEnter?'环境准备就绪':'设备与时钟已通过';
 let message=entry.message;
 if(!demoBackendCurrent())message=demoUpgradeMessage(true);
 else if(canEnter&&!entry.active)message=preparationMessages.ready;
 else if(entry.id&&!canEnter)message='所选记录不是当前准备会话，请在诊断记录中打开当前会话。';
 $('prepare-state').textContent='设备与时间检查已通过。'+message;
}
function renderPreparation(v){
 preparationData=v;const active=!['ended','failed_clean','recovery_required'].includes(v.status);
 const recovering=v.status==='recovering';
 const cleaned=v.status==='failed_clean'&&v.cleanup?.confirmed&&v.events.some(event=>event.stage==='recovering');
 const title=recovering?'正在核对中断会话':cleaned?'中断会话已清理':preparationNames[v.status]||v.status;
 $('prepare-badge').textContent=v.status==='ready'&&!v.preparation_ready?'就绪已过期 · 等待核验':v.preparation_ready?'环境准备就绪':title;
 const detail=v.error|| (v.status==='recovery_required'?v.cleanup?.errors?.[0]:null);
 const next=!demoBackendCurrent()&&(['ended','failed_clean'].includes(v.status))&&v.cleanup?.confirmed
  ?demoUpgradeMessage(false)
  :v.status==='ready'?(preparationStoppingID===v.id?preparationMessages.stopping:'正在等待后台时钟复核。')
  :recovering?'正在检查设备和上次会话，请稍候。':cleaned?'可以点击“连接设备”开始本次准备。':preparationMessages[v.status]||'';
 $('prepare-state').textContent=`${title} · ${detail?preparationErrorMessage(detail):next}`;
 $('prepare-connect').disabled=active;$('prepare-restore').disabled=v.status!=='checked';$('prepare-sync').disabled=v.status!=='radio_ready';$('prepare-stop').disabled=!active||recovering;
 if(!demoBackendCurrent())$('prepare-connect').disabled=true;
 if(!preparationBackendCurrent())$('prepare-sync').disabled=true;
 if(v.status==='recovery_required')preparationRecoveryID=v.id;
 else if(preparationRecoveryID===v.id)preparationRecoveryID='';
 renderPreparationRecovery();
 renderPreparationTimingHelp(v);renderPreparationProgress(v);
 renderPreparationDemoEntry();
 if(recovering)$('prepare-readiness').textContent='正在读取六节点当前状态。';
 if(cleaned)$('prepare-readiness').textContent='旧会话的占用已释放。';
 if(v.ap_cidr&&!$('prepare-cidr').matches(':focus'))$('prepare-cidr').value=v.ap_cidr;
 $('prepare-export').hidden=false;$('prepare-export').href='/api/preparation/export?id='+encodeURIComponent(v.id);
 $('prepare-nodes').replaceChildren();$('prepare-device-summary').replaceChildren();for(const role of ['AP','STA1','STA2','STA3','STA4','MON15'].filter(n=>n!=='MON15'||v.platform?.monitor_enabled!==false)){
  const n=v.recovery_inspection?.[role]||v.radio_changes[role]?.after||v.nodes[role],c=v.clock_results[role],tr=el('tr');
  const mode=n?.info?.match(/type (\S+)/)?.[1]||'NA';
  const link=role==='AP'?mode:n?.link?.includes('Connected to')?'已关联':n?'未关联':'待检查';
  for(const x of [role,n?`${n.interface} / ${n.phy}`:'NA',link,n?`${n.srcversion.slice(0,8)} / ${n.parameter??'—'}`:'NA',n?.BE?`${n.BE.hw_aifs} / ${n.BE.hw_cwmin}—${n.BE.hw_cwmax}`:'—',anumber(c?.offset_ms,3),anumber(c?.uncertainty_ms,3),anumber(c?.envelope_ms,3)])tr.append(el('td',String(x)));
  $('prepare-nodes').append(tr);
  const summary=el('tr');
  const wireless=n?(role==='AP'?(mode==='AP'?'AP已启用':'等待准备'):n.link?.includes('Connected to')?'已连接实验网络':'等待连接'):'等待检查';
  const halted=['failed_clean','recovery_required'].includes(v.status);
  for(const text of [role,v.recovery_inspection?.[role]?'恢复检查时已连接':n?'检查时已连接':'等待检查',halted&&!v.recovery_inspection?.[role]?'准备中止，待重新检查':wireless])summary.append(el('td',text));
  $('prepare-device-summary').append(summary);
 }
 $('prepare-events').textContent=v.events.map(e=>`${new Date(e.at_ns/1e6).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})} ${e.stage} ${e.node||''} ${e.message}`).join('\n');
 $('prepare-error').textContent=[v.error,...(v.cleanup?.errors||[])].filter(Boolean).join('\n');
}
async function refreshPreparationList(){const v=await api('/api/preparation/list'),sel=$('prepare-sessions'),old=sel.value;preparationRecoveryID=v.sessions.find(s=>s.status==='recovery_required')?.id||'';renderPreparationRecovery();sel.replaceChildren(el('option','选择准备会话'));sel.options[0].value='';for(const s of v.sessions){const o=el('option',`${s.id.slice(0,8)} · ${preparationNames[s.status]||s.status}`);o.value=s.id;sel.append(o);}sel.value=preparationID||old;if(!preparationID){const current=v.sessions.find(s=>!['ended','failed_clean'].includes(s.status));if(current){preparationID=current.id;sel.value=current.id;await fetchPreparation();}}}
async function fetchPreparation(){
 if(!preparationID||preparationFetching)return;
 preparationFetching=true;
 try{
  const v=await api('/api/preparation?id='+encodeURIComponent(preparationID));
  if(v.id!==preparationID)return;
  renderPreparation(v);
  if(preparationDemoReady(v)&&demoBackendCurrent()){
   try{await refreshDemoCatalog();}catch{/* Both pages show the shared status on failure. */}
   renderPreparationDemoEntry();
  }
 }catch(e){
  preparationData=null;renderPreparationDemoEntry();
  $('prepare-state').textContent='无法读取准备状态：'+e.message+'。不要重复准备；恢复服务后先核对中断会话。';
  $('prepare-badge').textContent='状态未知';$('prepare-sync').disabled=true;$('prepare-restore').disabled=true;
 }finally{preparationFetching=false;}
}
$('prepare-enter-demo').onclick=()=>guard(async()=>{
 if(preparationEnteringDemo||!preparationDemoReady(preparationData))return;
 const identity=preparationData.id;
 preparationEnteringDemo=true;renderPreparationDemoEntry();
 try{
  await refreshDemoCatalog(true);
  if(!$('check').hidden&&preparationDemoReady(preparationData)&&preparationData.id===identity&&demoEntry().id===identity)show('real-demo',false);
 }finally{preparationEnteringDemo=false;renderPreparationDemoEntry();}
});
$('prepare-connect').onclick=()=>guard(async()=>{
 if(preparationRecoveryID)throw new Error('请先输入设备密码并点击“核对并清理中断会话”，完成后再连接设备。');
 if(!demoBackendCurrent())throw new Error(demoEntry().message);
 const password=$('prepare-password').value,sudo_password=$('prepare-sudo').value;
 $('prepare-password').value='';$('prepare-sudo').value='';$('prepare-connect').disabled=true;
 if(!preparationKey)preparationKey=crypto.randomUUID();
 try{const v=await api('/api/preparation/check',{password,sudo_password,request_key:preparationKey,ap_cidr:$('prepare-cidr').value.trim(),time_transport:$('prepare-time-transport').value});preparationID=v.id;preparationKey='';renderPreparation(v);await refreshPreparationList();notice('已启动六节点准备检查。');}catch(e){$('prepare-connect').disabled=false;throw e;}
});
for(const [id,action] of [['prepare-restore','restore'],['prepare-sync','sync'],['prepare-stop','stop']])$(id).onclick=()=>guard(async()=>{if(!preparationID)throw new Error('先连接并检查节点');if(action==='sync'&&!preparationBackendCurrent())throw new Error('后台仍是旧版，请清理后重启控制台。');$(id).disabled=true;if(action==='stop'){preparationStoppingID=preparationID;$('prepare-progress').value=0;$('prepare-readiness').textContent='正在结束使用并清理。';$('prepare-ready-actions').hidden=true;$('prepare-enter-demo').disabled=true;}try{renderPreparation(await api('/api/preparation/action',{id:preparationID,action,ap_cidr:$('prepare-cidr').value.trim(),allow_temporary_restore:action==='restore'}));}catch(e){if(action==='stop')preparationStoppingID='';throw e;}finally{await fetchPreparation();}});
$('prepare-open').onclick=()=>guard(async()=>{preparationID=$('prepare-sessions').value;if(!preparationID)throw new Error('请选择会话');await fetchPreparation();});
$('prepare-recover').onclick=()=>guard(async()=>{const identity=preparationRecoveryID;if(!identity)throw new Error('当前没有需要恢复的中断会话');const password=$('prepare-password').value,sudo_password=$('prepare-sudo').value;$('prepare-password').value='';$('prepare-sudo').value='';$('prepare-recover').disabled=true;preparationID=identity;try{renderPreparation(await api('/api/preparation/recover',{id:identity,password,sudo_password}));}finally{await fetchPreparation();}});
async function initPreparation(){
 renderPreparationTimingHelp();
 await refreshPreparationList();
 if(!demoBackendCurrent()){
  $('prepare-connect').disabled=true;
  if(!preparationID)$('prepare-state').textContent=demoUpgradeMessage(false);
 }
 setInterval(()=>{if(!$('check').hidden)fetchPreparation();},1000);
 setInterval(()=>{if(!$('check').hidden)refreshPreparationList().catch(()=>{});},4000);
}
