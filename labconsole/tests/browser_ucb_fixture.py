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
const boot={capabilities:['unified_demo','demo_access_modes','throughput_ucb_v2','per_sta_cw','demo_lifecycle_v3','ucb_hold_v3']};
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
 for(const id of ['real-demo-initial',...demoStaNames.flatMap(n=>['real-demo-initial-'+n,'real-demo-target-'+n])]){
  assert([...$(id).options].map(x=>x.value).join(',')==='8,16,32,64,128','missing full manual range '+id);
 }
 $('real-demo-initial').value='32';$('real-demo-initial').onchange();
 assert(demoStaNames.every(n=>$('real-demo-initial-'+n).value==='32'),'initial bulk fill');
 $('real-demo-initial-STA1').value='8';$('real-demo-upload-STA4').checked=false;
 fixture={id:'b'.repeat(32),status:'running',phase:'OFFLINE FIXTURE',request:{mode:'unified',access_mode:'basic',
  initial_w:32,initial_per_STA_W:{STA1:8,STA2:32,STA3:32,STA4:32},active_senders:['STA1','STA2','STA3'],
  warmup_s:5,measure_s:600,tail_s:5,segment_s:20,guard_s:3},events:[],points:[],
  control:{revision:2,window_segments:6,score_tail_s:5,blocks:{0:{mode:'manual',W:null,per_STA_W:{STA1:8,STA2:32,STA3:32,STA4:32}}}},
  control_actions:{mode:'manual',manual:true,adapt:true,takeback:false,pending:null}};
 for(let second=1;second<=65;second++)fixture.points.push({second,total_Mbps:10,per_STA_Mbps:{STA1:4,STA2:3,STA3:3,STA4:0},
  W:null,per_STA_W:{STA1:8,STA2:32,STA3:32,STA4:32}});
 await $('real-demo-start').onclick();
 let start=sentRequests.find(r=>r.path==='/api/demo/start').data;
 assert(start.initial_per_STA_W.STA1===8&&start.initial_per_STA_W.STA2===32,'initial map serialization');
 assert(start.active_senders.join(',')==='STA1,STA2,STA3','three uploaders');
 for(const n of demoStaNames)$('real-demo-target-'+n).value='128';$('real-demo-target-STA2').value='16';
 await $('real-demo-apply').onclick();
 let manual=sentRequests.at(-1).data;
 assert(manual.action==='manual'&&manual.W.STA1===128&&manual.W.STA2===16&&manual.W.STA4===128,'manual map serialization');
 assert(!('active_senders' in manual),'manual policy command leaked sender set');
 // Repeated polls must not rewrite the disabled attribute of native selects.
 const disabled=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'disabled');let disabledWrites=0;
 for(const id of ['real-demo-access','real-demo-initial',...demoStaNames.flatMap(n=>['real-demo-initial-'+n,'real-demo-target-'+n])]){
  Object.defineProperty($(id),'disabled',{get(){return disabled.get.call(this);},set(v){disabledWrites++;disabled.set.call(this,v);},configurable:true});
 }
 demoControls();renderUnified(fixture,fixture.points,true);demoControls();renderUnified(fixture,fixture.points,true);
 assert(disabledWrites===0,'polling rewrote a select disabled property');
 const pointCount=fixture.points.length;
 $('real-demo-clear').onclick();assert(demoClearAt.get(fixture.id)===65,'clear cutoff');
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
 assert(demoStaNames.every(n=>$('real-demo-target-'+n).disabled),'auto must own CW controls');
 assert(!$('real-demo-takeback').disabled,'takeback unavailable');
 assert(!$('real-demo-adapt-status').textContent.includes('null'),'mixed W rendered as null');
 assert(!document.body.innerText.includes('[object Object]'),'object rendered as text');
 await $('real-demo-takeback').onclick();
 assert(sentRequests.at(-1).data.action==='takeback','takeback command');
 const oldId=fixture.id;fixture.status='completed';fixture.restoration={confirmed:true};demoRenderSignature='';renderDemo(fixture);demoControls();
 assert(!$('real-demo-resume').disabled,'confirmed recovery must allow resume on demo page');
 await $('real-demo-resume').onclick();
 const resumed=sentRequests.at(-1);
 assert(resumed.path==='/api/demo/start'&&resumed.data.preparation_id==='a'.repeat(32),'resume did not reuse preparation');
 assert(resumed.data.initial_per_STA_W.STA2===16&&fixture.id!==oldId,'resume did not retain last CW or independent run');
 fixture.control.revision=3;
 fixture.control.blocks[2]={...fixture.control.blocks[2],reason:'吞吐保持稳定，保持当前 W',
   hold_status:{phase:'hold',reference_Mbps:12.5,drop_streak:0},
   estimates:[{W:8,count:2,mean_Mbps:12.3,ucb_Mbps:13.477},{W:16,count:2,mean_Mbps:12.5,ucb_Mbps:13.677}]};
 demoRenderSignature='';renderDemo(fixture);
 assert($('real-demo-decision').textContent.includes('保持基准 12.50'),'hold reference missing');
 assert($('real-demo-algorithm-title').textContent.includes('稳定保持'),'hold strategy title missing');
 if(FINAL_MODE==='manual'){
  fixture.control_actions={mode:'manual',manual:true,adapt:true,takeback:false,pending:null};
  fixture.control.blocks[2]={mode:'manual',W:null,per_STA_W:{STA1:8,STA2:16,STA3:64,STA4:128}};
  fixture.points.at(-1).W=null;fixture.points.at(-1).per_STA_W=fixture.control.blocks[2].per_STA_W;
  demoRenderSignature='';renderDemo(fixture);
  assert($('real-demo-target-STA4').value==='128','takeback did not preload hardware settings');
 }
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
