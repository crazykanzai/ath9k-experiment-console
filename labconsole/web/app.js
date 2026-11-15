'use strict';
const consoleScriptsReady=new Promise(resolve=>document.addEventListener('DOMContentLoaded',resolve,{once:true}));
let boot;
let noticeTimer, noticeRevision=0;
const $=id=>document.getElementById(id);
const staColors={STA1:'#285e90',STA2:'#b57532',STA3:'#3e8a75',STA4:'#8b69a6'};
function anumber(v,d=2){return v==null||!Number.isFinite(v)?'NA':v.toFixed(d);}
function show(page,refresh=true){
 clearNotice();
 document.querySelectorAll('.page').forEach(x=>x.hidden=x.id!==page);
 document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('selected',x.dataset.page===page));
 if(page==='real-demo')updateDemoPage(refresh);
 if(page==='check')fetchPreparation();
}
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function clearNotice(){
 clearTimeout(noticeTimer);
 $('message').hidden=true;
 $('message-text').textContent='';
 noticeRevision++;
}
function notice(text,error=false){
 clearNotice();
 $('message-text').textContent=text;
 $('message').className=error?'error':'';
 $('message').hidden=false;
 noticeTimer=setTimeout(clearNotice,8000);
}
async function api(path,data){const opts=data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Console-Token':boot.csrf},body:JSON.stringify(data)};const r=await fetch(path,opts);const v=await r.json();if(!r.ok)throw new Error(v.error||'请求失败');return v;}
function download(name,value){const u=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json;charset=utf-8'}));const a=el('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}
function fillPlatform(p){$('platform-monitor-enabled').checked=p.monitor_enabled!==false;boot.platform=structuredClone(p);$('platform-name').value=p.name;$('management-ip').value=p.controller.management_ip;$('ntp-source').value=p.controller.ntp_source;$('platform-json').value=JSON.stringify(p,null,2);$('nodes').replaceChildren();for(const node of p.nodes){const tr=el('tr');tr.dataset.role=node.role;tr.append(el('td',node.role));for(const field of ['address','port','username','credential_ref','interface_hint','remote_work_dir']){const td=el('td');const input=el('input');input.value=node[field]??'';input.dataset.field=field;input.setAttribute('aria-label',node.role+' '+field);if(field==='port'){input.type='number';input.min=1;input.max=65535;}td.append(input);tr.append(td);}$('nodes').append(tr);}}
function formPlatform(){const p=structuredClone(boot.platform);p.monitor_enabled=$('platform-monitor-enabled').checked;p.name=$('platform-name').value;p.controller.management_ip=$('management-ip').value.trim();p.controller.ntp_source=$('ntp-source').value.trim();p.nodes=[...$('nodes').rows].map(tr=>{const n={role:tr.dataset.role};for(const input of tr.querySelectorAll('input'))n[input.dataset.field]=input.dataset.field==='port'?Number(input.value):input.value;return n;});return p;}
async function guard(fn){
 clearNotice();
 const revision=noticeRevision;
 try{await fn();}catch(e){if(revision===noticeRevision)notice(e.message,true);}
}
$('dismiss-message').onclick=clearNotice;
document.querySelectorAll('[data-page]').forEach(x=>x.onclick=()=>show(x.dataset.page));
$('save-platform').onclick=()=>guard(async()=>{const v=await api('/api/platform',{platform:formPlatform()});fillPlatform(v.platform);notice('平台档案已保存，未连接节点。');});
$('export-platform').onclick=()=>guard(async()=>{const v=await api('/api/validate-platform',{platform:formPlatform()});download('platform.json',v.platform);});
$('apply-json').onclick=()=>guard(async()=>{fillPlatform(JSON.parse($('platform-json').value));notice('档案已填入表单，核对后保存。');});
$('import-platform').onchange=()=>guard(async()=>{const f=$('import-platform').files[0];if(f)fillPlatform(JSON.parse(await f.text()));});
$('check-local').onclick=()=>guard(async()=>{const v=await api('/api/check',{});$('checks').replaceChildren();for(const c of v.checks){const row=el('tr');row.append(el('td',c.name),el('td',c.status),el('td',c.detail));$('checks').append(row);}});
async function init(){boot=await api('/api/bootstrap');$('version').textContent=boot.version;$('workspace').textContent=boot.workspace;fillPlatform(boot.platform);await consoleScriptsReady;await initPreparation();initRealDemo();}
init().catch(e=>notice(e.message,true));
