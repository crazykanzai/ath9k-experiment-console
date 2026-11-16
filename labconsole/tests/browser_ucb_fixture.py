"""Generate a local-only browser fixture; API calls are intercepted in memory.

The actual shipped HTML/CSS/JS is used. No console server or node is contacted.
Run this helper, then open its HTML with a headless browser for DOM assertions
and visual review. Mock throughput is explicitly labelled, never experiment data.
"""
import argparse
from pathlib import Path
import re


STUB = r"""
const $=id=>document.getElementById(id);
function el(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}
const staColors={STA1:'#285e90',STA2:'#b57532',STA3:'#3e8a75',STA4:'#8b69a6'};
function anumber(v,d=2){return Number.isFinite(v)?v.toFixed(d):'NA';}
const boot={capabilities:['unified_demo','demo_access_modes','throughput_ucb_v2','per_sta_cw','demo_lifecycle_v3','ucb_hold_v3','ucb_hold_w32','unified_10s','per_sta_cw_pairs','auto_cw_profile','ucb_same_w_drop']};
function preparationBackendCurrent(){return true;}
const guard=fn=>fn();
const sentRequests=[];
let fixture;
async function api(path,data){
 if(data){sentRequests.push({path,data});if(path==='/api/demo/start'&&fixture.status==='completed'){fixture={...fixture,id:'c'.repeat(32),status:'running',restoration:null};}return fixture;}
 if(path==='/api/demo/list')return {sessions:[],ready_preparation:'a'.repeat(32),preparation_status:'ready',demo_busy:false};
 return fixture;
}
window.confirm=()=>true;
window.addEventListener('error',e=>document.documentElement.dataset.qaError=e.message);
"""

CHECKS = r"""
(async()=>{
 const assert=(ok,message)=>{if(!ok)throw new Error(message);};
 assert($('prepare-time-transport').value==='ap_relay','AP relay must be selected by default');
 assert([...$('prepare-time-transport').options].some(x=>x.value==='direct'),'direct timing option missing');
 assert(demoBackendCurrent(),'10s backend must be accepted');
 boot.capabilities.pop();
 assert(!demoBackendCurrent(),'20s backend requires restart');
 boot.capabilities.push('fair_cw_ucb');
 assert(!demoBackendCurrent(),'running per-STA prototype backend requires restart');
 boot.capabilities.pop();
 boot.capabilities.push('ucb_same_w_drop');
 const cwSelects=demoCwFields.flatMap(field=>['real-demo-initial-'+field,...demoStaNames.flatMap(n=>[`real-demo-initial-${n}-${field}`,`real-demo-target-${n}-${field}`])]);
 for(const id of cwSelects){
  assert([...$(id).options].map(x=>x.value).join(',')==='1,3,7,15,31,63,127,255,511,1023','missing driver values '+id);
 }
 $('real-demo-initial-cwmin').value='31';$('real-demo-initial-cwmin').onchange();
 $('real-demo-initial-cwmax').value='511';$('real-demo-initial-cwmax').onchange();
 assert(demoStaNames.every(n=>$(`real-demo-initial-${n}-cwmin`).value==='31'&&$(`real-demo-initial-${n}-cwmax`).value==='511'),'independent initial bulk fill');
 $('real-demo-initial-STA1-cwmin').value='7';$('real-demo-initial-STA1-cwmax').value='31';$('real-demo-upload-STA4').checked=false;
 fixture={id:'b'.repeat(32),status:'running',phase:'OFFLINE FIXTURE',request:{mode:'unified',access_mode:'basic',
  initial_w:32,initial_per_STA_W:{STA1:8,STA2:32,STA3:32,STA4:32},active_senders:['STA1','STA2','STA3'],
  warmup_s:5,measure_s:600,tail_s:5,segment_s:20,guard_s:3},events:[],points:[],
  control:{revision:2,window_segments:6,score_tail_s:5,blocks:{0:{mode:'manual',W:null,per_STA_W:{STA1:8,STA2:32,STA3:32,STA4:32}}}},
  control_actions:{mode:'manual',manual:true,adapt:true,takeback:false,pending:null}};
 for(let second=1;second<=65;second++)fixture.points.push({second,total_Mbps:10,per_STA_Mbps:{STA1:4,STA2:3,STA3:3,STA4:0},
  W:null,per_STA_W:{STA1:8,STA2:32,STA3:32,STA4:32}});
 await $('real-demo-start').onclick();
 let start=sentRequests.find(r=>r.path==='/api/demo/start').data;
 assert(start.initial_per_STA_CW.STA1.cwmin===7&&start.initial_per_STA_CW.STA1.cwmax===31&&start.initial_per_STA_CW.STA2.cwmax===511,'initial pair serialization');
 assert(start.active_senders.join(',')==='STA1,STA2,STA3','three uploaders');
 $('real-demo-target-STA2-cwmin').value='15';$('real-demo-target-STA2-cwmax').value='511';
 await $('real-demo-apply').onclick();
 let manual=sentRequests.at(-1).data;
 assert(manual.action==='manual'&&manual.CW.STA2.cwmin===15&&manual.CW.STA2.cwmax===511&&manual.CW.STA1.cwmax===63,'manual pair serialization');
 assert(!('active_senders' in manual),'manual policy command leaked sender set');
 // Repeated polls must not rewrite the disabled attribute of native selects.
 const disabled=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'disabled');let disabledWrites=0;
 for(const id of ['real-demo-access',...cwSelects]){
  Object.defineProperty($(id),'disabled',{get(){return disabled.get.call(this);},set(v){disabledWrites++;disabled.set.call(this,v);},configurable:true});
 }
 demoControls();renderUnified(fixture,fixture.points,true);demoControls();renderUnified(fixture,fixture.points,true);
 assert(disabledWrites===0,'polling rewrote a select disabled property');
 const pointCount=fixture.points.length;
 const span=$('real-demo-time-span'), slider=$('real-demo-time-start'), range=$('real-demo-time-range');
 span.value='30';span.onchange();
 assert(range.textContent==='35–65 秒 · 跟随最新'&&slider.max==='35','default following at fixed 30s scale');
 slider.value='12';slider.dispatchEvent(new Event('input'));
 assert(range.textContent==='12–42 秒 · 查看历史','slider did not move the plotted window');
 fixture.points.push({...fixture.points.at(-1),second:75});
 renderDemo(fixture);
 assert(range.textContent==='12–42 秒 · 查看历史'&&slider.max==='45','new live data moved the paused window');
 $('real-demo-time-follow').onclick();
 assert(range.textContent==='45–75 秒 · 跟随最新','follow did not restore the latest fixed window');
 fixture.points.length=pointCount;
 span.value='60';renderDemo(fixture);
 $('real-demo-clear').onclick();assert(demoClearAt.get(fixture.id)===65,'clear cutoff');
 assert(range.textContent==='65–125 秒 · 跟随最新'&&slider.disabled,'cleared plot must retain the selected scale');
 assert(fixture.points.length===pointCount,'curve clear destroyed data');
 $('real-demo-show-all').onclick();assert(!demoClearAt.has(fixture.id),'restore curve');
 await $('real-demo-adapt').onclick();
 assert(sentRequests.at(-1).data.action==='adapt'&&!('active_senders' in sentRequests.at(-1).data),'takeover leaked sender set');
 fixture.control.blocks[1]={mode:'auto',action:'adapt',W:8,per_STA_W:Object.fromEntries(demoStaNames.map(n=>[n,8])),
  reason:'探索近期没有有效样本的 W',estimates:[{W:8,count:0,mean_Mbps:null,ucb_Mbps:null},{W:16,count:0,mean_Mbps:null,ucb_Mbps:null}]};
 fixture.control.blocks[2]={mode:'auto',W:16,per_STA_W:Object.fromEntries(demoStaNames.map(n=>[n,16])),
  reason:'探索近期没有有效样本的 W',estimates:[{W:8,count:1,mean_Mbps:10,ucb_Mbps:11.2},{W:16,count:0,mean_Mbps:null,ucb_Mbps:null}]};
 fixture.control_actions={mode:'auto',manual:false,adapt:false,takeback:true,pending:null};
 fixture.points.forEach(p=>{if(p.second>=25){p.W=p.second<45?8:16;p.per_STA_W=Object.fromEntries(demoStaNames.map(n=>[n,p.W]));}});
 demoRenderSignature='';renderDemo(fixture);
 assert(!$('#never-exists'),'unreachable');
})().catch(e=>{document.documentElement.dataset.qaStatus='FAIL';document.documentElement.dataset.qaError=e.stack;});
"""
# The final assertions are substituted separately for the requested screenshot.
FINISH = r"""
 assert($('real-demo-estimates').rows.length===2,'two-arm table');
 assert(demoStaNames.every(n=>demoCwFields.every(field=>$(`real-demo-target-${n}-${field}`).disabled)),'auto must own CW controls');
 assert(!$('real-demo-takeback').disabled,'takeback unavailable');
 assert(!$('real-demo-adapt-status').textContent.includes('null'),'mixed W rendered as null');
 assert(!document.body.innerText.includes('[object Object]'),'object rendered as text');
 await $('real-demo-takeback').onclick();
 assert(sentRequests.at(-1).data.action==='takeback','takeback command');
 const oldId=fixture.id;fixture.status='completed';fixture.restoration={confirmed:true};demoRenderSignature='';
 fixture.points.at(-1).per_STA_CW=Object.fromEntries(demoStaNames.map(n=>[n,{cwmin:15,cwmax:n==='STA2'?511:63}]));
 renderDemo(fixture);demoControls();
 assert(!$('real-demo-resume').disabled,'confirmed recovery must allow resume on demo page');
 await $('real-demo-resume').onclick();
 const resumed=sentRequests.at(-1);
 assert(resumed.path==='/api/demo/start'&&resumed.data.preparation_id==='a'.repeat(32),'resume did not reuse preparation');
 assert(resumed.data.initial_per_STA_CW.STA2.cwmin===15&&resumed.data.initial_per_STA_CW.STA2.cwmax===511&&fixture.id!==oldId,'resume did not retain last CW pair or independent run');
 fixture.control.revision=3;
 fixture.control.blocks[2]={...fixture.control.blocks[2],reason:'吞吐保持稳定，保持当前 W',
   hold_status:{phase:'hold',reference_Mbps:12.5,drop_streak:0},
   estimates:[{W:8,count:2,mean_Mbps:12.3,ucb_Mbps:13.477},{W:16,count:2,mean_Mbps:12.5,ucb_Mbps:13.677}]};
 demoRenderSignature='';renderDemo(fixture);
 assert($('real-demo-decision').textContent.includes('吞吐保持稳定，保持当前 W'),'recorded reason missing');
 assert($('real-demo-algorithm-title').textContent.includes('历史记录'),'historical decision title missing');
 fixture.control.revision=4;
 fixture.control.hold_rules={trial_limit:4,confirm_limit:2};
 fixture.control.blocks[2]={mode:'auto',W:null,per_STA_W:{STA1:8,STA2:16,STA3:16,STA4:16},
   reason:'按 UCB 评分分配一个额外试探段',previous_metrics:{jain:.95,min_share:.22,required_share:1/6,fair:true},
   hold_status:{phase:'trial',trials:4,confirmations:0},estimates:[
    {per_STA_W:{STA1:8,STA2:16,STA3:16,STA4:16},count:2,mean_Mbps:12.5,ucb_Mbps:13.7,status:'公平达标'},
    {per_STA_W:{STA1:16,STA2:8,STA3:16,STA4:16},count:1,mean_Mbps:11.1,ucb_Mbps:12.7,status:'公平达标'}]};
 demoRenderSignature='';renderDemo(fixture);
 assert($('real-demo-decision').textContent.includes('按 UCB 评分分配一个额外试探段'),'recorded fairness decision missing');
 assert($('real-demo-estimates').textContent.includes('STA1 W8 / STA2 W16')&&$('real-demo-estimates').textContent.includes('公平达标'),'per-STA estimate missing');
 fixture.control.revision=5;
 fixture.request.segment_s=10;
 fixture.request.guard_s=2;
 fixture.control.arms=[8,16,32];
 fixture.control.blocks[2]={mode:'auto',W:32,per_STA_W:Object.fromEntries(demoStaNames.map(n=>[n,32])),
   reason:'探索候选，每档收集两个有效段',hold_status:{phase:'explore'},
   estimates:[{W:8,count:1,mean_Mbps:10,ucb_Mbps:11.2},{W:16,count:1,mean_Mbps:11,ucb_Mbps:12.2},{W:32,count:0}]};
 demoRenderSignature='';renderDemo(fixture);
 assert($('real-demo-estimates').rows.length===3&&$('real-demo-estimates').rows[2].cells[0].textContent==='W32','W32 estimate missing');
 const shortSegment=renderUnified(fixture,fixture.points,true).find(row=>row.index===2);
 assert(shortSegment.start_s===25&&shortSegment.stop_s===35&&Number.isFinite(shortSegment.stable_Mbps),'10s segment with 3s score was not rendered');
 fixture.control.revision=6;
 fixture.events=[{kind:'cw_verified',node:'STA1',receipt:{index:2,readback:{hw_cwmin:'31',hw_cwmax:'255'}}}];
 demoRenderSignature='';renderDemo(fixture);
 assert($('real-demo-decision').textContent.includes('W32')&&$('real-demo-segments').textContent.includes('W32'),'automatic stage must show W32');
 assert($('real-demo-events').textContent.includes('W32')&&!$('real-demo-events').textContent.includes('31/255'),'automatic hardware event should show W32');
 fixture.control.revision=7;
 fixture.control.blocks[2].hold_status.hold_comparison={W:32,previous_Mbps:12,observed_Mbps:11.4,threshold_Mbps:11.4};
 renderDemo(fixture);
 assert($('real-demo-algorithm-title').textContent.includes('W8 / W16 / W32'),'W32 strategy title missing');
 assert($('real-demo-decision').textContent.includes('W32 同档比较 12.00 → 11.40')&&!$('real-demo-decision').textContent.includes('/2 段'),'new hold must show one same-W comparison');
 if(FINAL_MODE==='manual'){
  fixture.control_actions={mode:'manual',manual:true,adapt:true,takeback:false,pending:null};
  const settings={STA1:{cwmin:7,cwmax:31},STA2:{cwmin:15,cwmax:511},STA3:{cwmin:63,cwmax:127},STA4:{cwmin:127,cwmax:255}};
  fixture.control.blocks[2]={mode:'manual',W:null,per_STA_W:{STA1:8,STA2:16,STA3:64,STA4:128},per_STA_CW:settings};
  fixture.points.at(-1).W=null;fixture.points.at(-1).per_STA_W=fixture.control.blocks[2].per_STA_W;
  fixture.points.at(-1).per_STA_CW=settings;
  demoRenderSignature='';renderDemo(fixture);
  assert($('real-demo-target-STA4-cwmin').value==='127'&&$('real-demo-target-STA4-cwmax').value==='255','takeback did not preload CW pair');
  fixture.control.blocks[2]={mode:'manual',W:32,per_STA_W:Object.fromEntries(demoStaNames.map(n=>[n,32]))};
  fixture.points.at(-1).second=31;
  fixture.request.initial_per_STA_CW=settings;
  demoManualMode='auto';renderUnified(fixture,fixture.points,true);
  assert($('real-demo-target-STA4-cwmin').value==='31'&&$('real-demo-target-STA4-cwmax').value==='255','takeback must use applied W profile instead of old manual pair');
 }
 renderDemo({...fixture,id:'d'.repeat(32),status:'completed',request:{...fixture.request,mode:'continuous'},control:null});
 assert(!$('real-demo-data').hidden&&$('real-demo-algorithm').hidden,'legacy record could not open without a control plan');
 assert($('real-demo-export').href.endsWith('d'.repeat(32)),'legacy export missing');
 renderDemo(fixture);
 document.querySelectorAll('.page').forEach(n=>n.hidden=n.id!=='real-demo');
 $('real-demo-ready').textContent='离线界面验证 · 合成占位数据 · 未连接节点、未打流';
 document.documentElement.dataset.qaStatus='PASS';
"""


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    web=Path(__file__).resolve().parents[1]/'web'
    html=(web/'index.html').read_text(encoding='utf-8')
    html=re.sub(r'<script[^>]*src="[^"]+"[^>]*></script>','',html)
    html=html.replace('<link rel="stylesheet" href="/style.css">','<style>'+(web/'style.css').read_text(encoding='utf-8')+'</style>')
    script=(web/'real-demo.js').read_text(encoding='utf-8')
    checks=CHECKS.replace(" assert(!$('#never-exists'),'unreachable');",FINISH)
    for mode in ('manual','auto'):
        page=html.replace('</body>','<script>'+STUB+'</script><script>'+script+'</script><script>const FINAL_MODE='+repr(mode)+';'+checks+'</script></body>')
        (args.output/(mode+'.html')).write_text(page,encoding='utf-8')
    print('Generated two offline browser fixtures; no node access.')


if __name__=='__main__':main()
