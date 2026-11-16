"""Explicit web preparation sessions, no traffic or automatic experiment launch."""
import copy
import concurrent.futures
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shlex
import threading
import time
import uuid
from .config import validate_platform, enabled_roles
from .clock_probe import clock_probe
from .storage import atomic_json
from .time_reference import TimeReference

ROLES=('AP','STA1','STA2','STA3','STA4','MON15')
HELPER_SHA='42c6d0a0bcaf5f0bd944d0c92d02d60135d9d215a518ec25b6ab3289b3de48b7'
SNTP_REQUIRED='NTPSynchronized=yes'
DEFAULT_TIME_TRANSPORT='ap_relay'


def business_cidr(value,platform):
    if not value:return ''
    addr=ipaddress.IPv4Interface(value)
    management=[platform['controller']['management_ip'],*[n['address'] for n in platform['nodes']]]
    if not addr.ip.is_private or addr.network.prefixlen>29 or any(ipaddress.ip_address(x) in addr.network for x in management):
        raise ValueError('AP业务CIDR须为原有效私有子网，且不能覆盖有线管理地址')
    return str(addr)


class SSHPreparation:
    def __init__(self,platform,password,sudo_password):
        helper=Path(__file__).resolve().parents[1]/'experiments/reg_access.py'
        if not helper.is_file() or hashlib.sha256(helper.read_bytes()).hexdigest()!=HELPER_SHA:
            raise ValueError('现场准备需要完整仓库中的已登记共同锁读取助手；离线功能不受影响')
        self.platform=platform;self.password=password;self.sudo_password=sudo_password
        self.clients={};self.reference=None;self.lock=threading.Lock()
        self.relay=None;self.relay_error=None;self.relay_stop=threading.Event();self.relay_thread=None
        self.code=Path(__file__).with_name('prepare_node.py').read_text(encoding='utf-8')

    def client(self,role):
        if role not in enabled_roles(self.platform):raise ValueError('本准备会话未启用 '+role)
        with self.lock:
            if role not in self.clients:
                import paramiko
                n=next(n for n in self.platform['nodes'] if n['role']==role)
                c=paramiko.SSHClient();c.load_system_host_keys();c.set_missing_host_key_policy(paramiko.RejectPolicy())
                c.connect(n['address'],port=n['port'],username=n['username'],password=self.password or None,
                          timeout=5,auth_timeout=5,banner_timeout=5)
                self.clients[role]=c
            return self.clients[role]

    def call(self,role,op,**args):
        node=next(n for n in self.platform['nodes'] if n['role']==role)
        request={'role':role,'op':op,'controller_ip':self.platform['controller']['management_ip'],
                 'time_source':self.platform['controller']['ntp_source'],
                 'interface_hint':node.get('interface_hint',''),'helper_sha':HELPER_SHA,**args}
        c=self.client(role)
        stdin,out,err=c.exec_command('sudo -S -k -p "" python3 -u -c '+shlex.quote(self.code),timeout=150 if op=='radio' else 20)
        stdin.write(self.sudo_password+'\n');stdin.flush()
        if out.readline().strip()!='ATH9K_PREP_READY':
            raise RuntimeError(role+' root helper not ready: '+err.read().decode(errors='replace')[:150])
        stdin.write(json.dumps(request)+'\n');stdin.flush();stdin.channel.shutdown_write()
        raw=out.read().decode(errors='replace');error=err.read().decode(errors='replace')
        code=out.channel.recv_exit_status()
        try:result=json.loads(raw)
        except ValueError:raise RuntimeError(role+' helper response invalid: '+error[:160])
        if code or not result.get('ok'):raise RuntimeError(role+': '+result.get('error',error[:160]))
        return result['value']

    def clock(self,role):
        offset,uncertainty=clock_probe(self.client(role))
        status=self.call(role,'clock_status')
        return {'offset_ms':offset/1e6,'uncertainty_ms':uncertainty,
                'envelope_ms':abs(offset)/1e6+uncertainty,**status}

    def start_reference(self):
        relay=self.platform['controller'].get('time_transport','direct')=='ap_relay'
        addresses=[n['address'] for n in self.platform['nodes'] if n['role'] in enabled_roles(self.platform)]
        self.reference=TimeReference(self.platform['controller']['management_ip'],addresses,port=1123 if relay else 123)
        if relay:
            code=Path(__file__).with_name('ntp_relay.py').read_text(encoding='utf-8')
            args=[self.platform['controller']['management_ip'],self.platform['controller']['ntp_source'],*addresses]
            self.relay=self.client('AP').exec_command('sudo -S -k -p "" python3 -u -c '+shlex.quote(code)+' '+shlex.join(args),timeout=8)
            stdin,out,err=self.relay;stdin.write(self.sudo_password+'\n');stdin.flush()
            if out.readline().strip()!='ATH9K_RELAY_READY':raise RuntimeError('AP中继未就绪：'+err.read().decode(errors='replace')[:160])
            def heartbeat():
                try:
                    while not self.relay_stop.is_set():
                        if out.channel.exit_status_ready():raise RuntimeError('AP时间中继提前退出')
                        stdin.write('heartbeat\n');stdin.flush();self.relay_stop.wait(2)
                except Exception as exc:self.relay_error=str(exc)
            self.relay_thread=threading.Thread(target=heartbeat,daemon=True);self.relay_thread.start()

    def reference_ok(self):return self.reference is not None and self.reference.error is None and self.relay_error is None

    def close(self):
        error=None;self.relay_stop.set()
        if self.relay_thread:self.relay_thread.join(3)
        if self.relay:
            try:
                stdin,out,err=self.relay;stdin.write('quit\n');stdin.flush();stdin.channel.shutdown_write()
                value=out.read().decode(errors='replace')
                if 'ATH9K_RELAY_CLOSED' not in value or out.channel.recv_exit_status()!=0:raise RuntimeError('AP时间中继退出未确认')
            except Exception as exc:error=str(exc)
        if self.reference:self.reference.close()
        for c in self.clients.values():c.close()
        self.password=None;self.sudo_password=None
        if error:raise RuntimeError(error)


def check_inventory(rows):
    errors=[]
    for role,v in rows.items():
        if v.get('busy') or v.get('trace_busy'):errors.append(role+' existing business/capture/trace; not stopped')
        if v.get('services','').splitlines()!=['active']*3:errors.append(role+' required existing services not active')
        if 'dev '+v['interface']+' ' in v['route']:errors.append(role+' management is wireless')
        if role.startswith('STA'):
            if 'running=0' not in v['stats_header']:errors.append(role+' driver statistics busy')
            if 'hol_abi=1 basic_retry_abi=2' not in v['stats_header']:errors.append(role+' statistics ABI mismatch')
            if v['candidate_sha']!='1f02b2ee31ddcb5eff3b6cc515405229c3071147a4127b15516e1891d12d7019':errors.append(role+' candidate hash')
            if v['original_sha']!=v['rollback_sha'] or v['original_sha']!='270d53c6d955b7451983b6b97f0627b98b596a44e582164fe4313ae8c1a9dee1':errors.append(role+' original/rollback hash')
            if not v['candidate_info'].startswith(v['kernel']+' '):errors.append(role+' kernel mismatch')
            if v['srcversion'] not in ('0AB85B473A0B1B032BFECCD','C6E744C117E5F6ABADF932F'):errors.append(role+' unexpected module')
        if role!='AP' and len(v['profile_uuids'])!=1:errors.append(role+' connection profile ambiguous')
    if errors:raise RuntimeError('; '.join(errors))


def check_radio(rows):
    check_inventory(rows)
    ap=rows['AP'];mac=re.search(r'\baddr ([0-9a-f:]{17})',ap['info'])
    if 'type AP' not in ap['info'] or 'ssid ath9k_test' not in ap['info'] or not mac or not ap['ips']:
        raise RuntimeError('AP role/SSID/business IP not ready')
    for role,v in rows.items():
        if 'channel 1 (2412 MHz), width: 20 MHz (no HT)' not in v['info']:raise RuntimeError(role+' channel/width gate')
        if role=='AP':continue
        if 'Connected to '+mac[1] not in v['link']:raise RuntimeError(role+' not associated to registered AP')
        if role.startswith('STA'):
            if not v['ips']:raise RuntimeError(role+' business IPv4 missing')
            be=v['BE'];fifo=(int(v['reg']['values']['0x30'],16)&0x3f0)>>4
            if int(v['reg']['values']['0x1070'],16)/44!=9 or int(v['reg']['values']['0x1030'],16)/44+2!=10:
                raise RuntimeError(role+' physical slot/SIFS gate')
            if v['srcversion']!='0AB85B473A0B1B032BFECCD' or v['parameter']!='20' or fifo!=20:raise RuntimeError(role+' FIFO20 runtime gate')
            if v['mask']!='0x100' or 'Power save: off' not in v['power_save']:raise RuntimeError(role+' rate mask/power save gate')
            if (be.get('hw_aifs'),be.get('hw_cwmin'),be.get('hw_cwmax'),be.get('status_name'))!=('3','15','1023','verified'):raise RuntimeError(role+' BE baseline gate')
    return mac[1]


# Demo preparation only. Formal acquisition windows keep their own timing.
DEMO_TIMING = (30,0,0,10)  # NTP settle, optional hold, hold sampling, background monitor


class PreparationHub:
    @property
    def roles(self):
        return enabled_roles(self.active.get('platform',{}) if self.active else {})

    def __init__(self,store,backend_factory=SSHPreparation,wait=None,timing=None):
        self.store=store;self.root=store.root/'preparations';self.root.mkdir(exist_ok=True)
        self.factory=backend_factory;self.wait=wait;self.timing=timing or DEMO_TIMING
        self.lock=threading.RLock();self.active=None;self.backend=None;self.thread=None
        self.cancel=threading.Event();self.closed=False
        self.demo_owner=None;self.demo_abort=None;self.demo_done=threading.Event();self.demo_done.set()
        for p in self.root.glob('*/session.json'):
            if p.is_symlink() or p.parent.is_symlink():continue
            s=json.loads(p.read_text(encoding='utf-8'))
            if s['status'] not in ('ended','failed_clean','recovery_required'):
                if s.get('demo_owner'):
                    s['demo_recovery_required']=True
                s.update(status='recovery_required',preparation_ready=False,
                         error='上次控制台中断，准备状态不可沿用。重新输入凭据后清理本会话时间覆盖。')
                atomic_json(p,s)

    def _save(self):
        self.active['heartbeat_ns']=time.time_ns()
        with self.lock:atomic_json(self.root/self.active['id']/'session.json',self.active)

    def event(self,stage,message,**data):
        with self.lock:
            self.active['stage']=stage
            self.active['events'].append({'at_ns':time.time_ns(),'stage':stage,'message':message,**data})
            self._save()

    def _reserve(self,identity,platform):
        with self.store.transaction() as db:
            endpoints=[n['address']+':'+str(n['port']) for n in platform['nodes'] if n['role'] in enabled_roles(platform)]
            if any(db.execute('SELECT 1 FROM reservations WHERE endpoint=?',(e,)).fetchone() for e in endpoints):
                raise ValueError('平台已有任务或中断准备会话占用；先结束/恢复，不能抢占')
            for e in endpoints:db.execute('INSERT INTO reservations VALUES (?,?)',(e,'prepare:'+identity))

    def _release(self):
        with self.store.transaction() as db:db.execute('DELETE FROM reservations WHERE job=?',('prepare:'+self.active['id'],))

    def create(self,platform,password,sudo_password,key,cidr='',time_transport=DEFAULT_TIME_TRANSPORT):
        platform=validate_platform(platform)
        if time_transport not in ('direct','ap_relay'):raise ValueError('未知授时路径')
        platform['controller']['time_transport']=time_transport
        if time_transport=='ap_relay':platform['controller']['ntp_source']=next(n['address'] for n in platform['nodes'] if n['role']=='AP')
        if platform['driver']['expected_srcversion']!='0AB85B473A0B1B032BFECCD' or platform['driver']['expected_module_sha256']!='1f02b2ee31ddcb5eff3b6cc515405229c3071147a4127b15516e1891d12d7019':
            raise ValueError('当前准备仅支持已验收FIFO-only资源身份，不能替换为未登记驱动')
        if not all(ipaddress.ip_address(n['address']).is_private for n in platform['nodes']):raise ValueError('现场准备仅接受私有有线地址')
        if not isinstance(key,str) or not 8<=len(key)<=100:raise ValueError('请求标识无效')
        if any(not isinstance(v,str) or len(v)>1024 for v in (password,sudo_password)):raise ValueError('凭据格式无效')
        if len({n['credential_ref'] for n in platform['nodes'] if n['role'] in enabled_roles(platform)})!=1:raise ValueError('本准备流程要求统一SSH凭据引用；不同凭据需先扩展')
        if time_transport=='direct' and platform['controller']['ntp_source']!=platform['controller']['management_ip']:raise ValueError('直接授时要求控制机作为临时时间参考')
        cidr=business_cidr(cidr,platform)
        with self.lock:
            if self.closed:raise ValueError('准备服务已关闭')
            for meta in self.listing():
                if meta['request_key']==key:return self.get(meta['id'])
            if self.thread and self.thread.is_alive():raise ValueError('准备会话仍在运行')
            identity=uuid.uuid4().hex;self._reserve(identity,platform)
            try:self.backend=self.factory(platform,password,sudo_password or password)
            except Exception:
                self.active={'id':identity};self._release();raise
            self.cancel=threading.Event()
            self.active={'id':identity,'request_key':key,'platform':platform,'source':'live_preparation',
               'status':'checking','stage':'connecting','created_ns':time.time_ns(),'events':[],
               'nodes':{},'radio_changes':{},'clock_intents':[],'clock_results':{},'cleanup':{},
               'ap_cidr':cidr,'preparation_ready':False,'experiment_ready':False,'traffic_started':False,
               'readiness_scope':'无线/FIFO/时钟准备；不代表带流、空口、饱和或采集验收',
               'timing':{'settle_s':self.timing[0],'hold_s':self.timing[1],
                         'sample_s':self.timing[2],'monitor_s':self.timing[3]},
               'completed_settle_s':0,'observed_hold_s':0,'completed_hold_s':0,'last_check_ns':None,'error':None}
            self._save();self._launch(self._check)
            return self.get(identity)

    def _launch(self,fn):
        def run():
            try:fn()
            except Exception as exc:
                cancelled=self.cancel.is_set() and str(exc)=='用户结束准备'
                if not cancelled:self.active['error']=str(exc)
                self.event('cancelled' if cancelled else 'failure',str(exc));self._cleanup(failed=not cancelled)
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start()

    def _all(self,op):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures={role:pool.submit(self.backend.call,role,op) for role in self.roles}
            rows={};errors={}
            for role,f in futures.items():
                try:rows[role]=f.result()
                except Exception as exc:errors[role]=str(exc)
            if errors:
                self.active['partial_inspection']=rows;self.active['inspection_errors']=errors;self._save()
                raise RuntimeError('; '.join(role+': '+value for role,value in errors.items()))
            return rows

    def _check(self):
        rows=self._all('inspect');check_inventory(rows)
        with self.lock:
            self.active['nodes']=rows
            if not self.active['ap_cidr'] and len(rows['AP']['ips'])==1:self.active['ap_cidr']=rows['AP']['ips'][0]
            self.active['status']='checked';self.event('checked','六节点检查完成；尚未恢复无线或同步时钟')

    def action(self,identity,action,cidr=None):
        with self.lock:
            s=self.get(identity)
            if not self.active or self.active['id']!=identity:raise ValueError('历史准备需先恢复清理，不能直接继续')
            if action=='stop':
                if self.demo_owner:raise ValueError('先在现场演示页停止演示并等待节点恢复，再结束准备')
                if s['status'] in ('ended','failed_clean'):return s
                if s['status']=='recovery_required':raise ValueError('请重新输入凭据并恢复清理')
                self.active['preparation_ready']=False;self.active['status']='stopping';self.cancel.set();self._save()
                if not self.thread or not self.thread.is_alive():self._launch(self._cleanup)
                return self.get(identity)
            if self.thread and self.thread.is_alive():raise ValueError('当前阶段正在执行，请等待或结束准备')
            if action=='restore' and s['status']=='checked':
                if cidr:self.active['ap_cidr']=business_cidr(cidr,self.active['platform']);self._save()
                if not self.active['ap_cidr']:raise ValueError('请填写原有效AP业务CIDR；检查没有发现，不能猜测')
                self._launch(self._restore)
            elif action=='sync' and s['status']=='radio_ready':self._launch(self._sync)
            else:raise ValueError('当前状态不允许此操作')
            return self.get(identity)

    def _restore(self):
        self.active['status']='restoring_radio';self.event('restoring_radio','复用既有配置和核验模块；不构建、不改持久文件')
        rows=self._all('inspect');check_inventory(rows)
        if not self.active['ap_cidr']:raise RuntimeError('AP业务CIDR未发现；请结束准备后填写原有效CIDR，不猜测')
        for role in self.roles:
            if self.cancel.is_set():return self._cleanup()
            self.event('restoring_radio','核对/恢复 '+role,node=role)
            result=self.backend.call(role,'radio',ap_cidr=self.active['ap_cidr'],ap_mac=self.active.get('ap_mac',''))
            self.active['radio_changes'][role]=result;self._save()
            before_power=re.search(r'txpower ([\d.]+) dBm',result['before']['info'])
            after_power=re.search(r'txpower ([\d.]+) dBm',result['after']['info'])
            if not before_power or not after_power or float(before_power[1])!=float(after_power[1]):raise RuntimeError(role+' power changed unexpectedly')
            if role=='AP':
                self.active['ap_mac']=re.search(r'\baddr ([0-9a-f:]{17})',result['after']['info'])[1]
        rows=self._all('inspect');check_radio(rows)
        self.active['nodes']=rows;self.active['status']='radio_ready'
        self.event('radio_ready','无线/FIFO20基线核验通过；时钟尚未验收')

    def _delay(self,seconds,progress_field=None,progress_offset=0):
        if self.wait:self.wait(seconds,self.cancel)
        else:
            end=time.monotonic()+seconds
            while not self.cancel.wait(min(1,max(0,end-time.monotonic()))):
                if progress_field:
                    self.active[progress_field]=progress_offset+min(seconds,int(seconds-max(0,end-time.monotonic())))
                self._save()
                if time.monotonic()>=end:break
        if self.cancel.is_set():raise RuntimeError('用户结束准备')
        if progress_field:
            self.active[progress_field]=progress_offset+seconds;self._save()

    def _clock_limit_ms(self,settled):
        # Shared/demo defaults stay unchanged. Dedicated experiment adapters
        # may explicitly override this hook under their own recorded policy.
        return 10 if settled else 50

    def _clock_sample(self,settled=False):
        if not self.backend.reference_ok():raise RuntimeError('本次时间参考不可用')
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            rows=dict(zip(self.roles,pool.map(self.backend.clock,self.roles)))
        self.active['clock_results']=rows
        self.active.setdefault('clock_samples',[]).append({'at_ns':time.time_ns(),'nodes':rows})
        self._save()
        for role,v in rows.items():
            expected=self.active['platform']['controller']['ntp_source']
            server=re.search(r'(?m)^\s*Server:\s+(\S+)',v['status'])
            limit=self._clock_limit_ms(settled)
            if v['envelope_ms']>limit or SNTP_REQUIRED not in v['status'] or not server or server[1]!=expected:
                raise RuntimeError(role+' 时间/源门禁：'+json.dumps(v,ensure_ascii=False))
        self.active['last_check_ns']=time.time_ns()
        self._save();return rows

    def _sync(self):
        self.active['status']='syncing';self.event('time_reference','启动仅服务六节点的临时时间参考')
        check_radio(self._all('inspect'))
        self.backend.start_reference()
        for role in self.roles:
            if self.cancel.is_set():return self._cleanup()
            self.active['clock_intents'].append(role);self._save()
            receipt=self.backend.call(role,'clock_apply',id=self.active['id'])
            self.active.setdefault('clock_originals',{})[role]=receipt['original_config'];self._save()
        timing=self.active['timing']
        settle,hold,step,monitor=(timing['settle_s'],timing['hold_s'],timing['sample_s'],timing['monitor_s'])
        self.event('settling','等待授时生效 '+str(settle)+' 秒')
        self._delay(settle,progress_field='completed_settle_s');previous=self._clock_sample(settled=True)
        if hold:
            self.active['status']='holding';self.event('holding','一次 '+str(hold)+' 秒无业务持时验收')
        elapsed=0
        while elapsed<hold:
            interval=min(step,hold-elapsed)
            self._delay(interval,progress_field='observed_hold_s',progress_offset=elapsed);elapsed+=interval
            rows=self._clock_sample()
            for role,v in rows.items():
                a,b=previous[role],v
                delta=(b['wall_ns']-b['monotonic_ns'])-(a['wall_ns']-a['monotonic_ns'])
                if abs(delta)>2_000_000:raise RuntimeError(role+' 采样墙钟跳变门禁')
            previous=rows;self.active['completed_hold_s']=elapsed;self._save()
        check_radio(self._all('inspect'))
        if self.cancel.is_set():return self._cleanup()
        self.active['status']='ready';self.active['preparation_ready']=True
        self.event('ready','环境准备就绪，后台持续守时；未进行带流门禁验收')
        while not self.cancel.is_set():
            self._delay(monitor);self._clock_sample()
            if not self.demo_owner:
                rows=self._all('inspect')
                with self.lock:
                    if not self.demo_owner:
                        check_radio(rows);self.active['nodes']=rows;self._save()
        self._cleanup()

    def claim_demo(self,identity,owner,abort):
        with self.lock:
            s=self.get(identity)
            if not self.active or self.active['id']!=identity or not s['preparation_ready'] or s['status']!='ready' or self.cancel.is_set():
                raise ValueError('请先完成当前环境准备和时间同步核验')
            if self.demo_owner or not self.backend:raise ValueError('已有现场任务接管，不能重复开始')
            if s.get('demo_recovery_required'):raise ValueError('前次演示恢复未确认，先核对恢复')
            self.demo_owner=owner;self.demo_abort=abort;self.demo_done.clear()
            self.active['demo_owner']=owner;self._save();return self.backend

    def release_demo(self,owner,restored):
        with self.lock:
            if self.demo_owner!=owner:raise ValueError('演示占用身份不匹配')
            self.active['demo_recovery_required']=not restored
            self.active['demo_owner']=None
            self.demo_owner=None;self.demo_abort=None;self.demo_done.set();self._save()
            if not restored:self.cancel.set()

    def _cleanup(self,failed=False):
        with self.lock:
            abort=self.demo_abort
        if abort:
            abort('环境准备/时钟失效或总控结束')
            if not self.demo_done.wait(90):
                self.active.update(status='recovery_required',preparation_ready=False,error='演示停止/恢复未确认，保留占用，不关闭连接')
                self._save();return
        self.active['preparation_ready']=False;self.active['status']='cleaning';self.event('cleaning','仅清理本会话时间覆盖；无线/FIFO基线保留')
        results={};errors=['前次演示硬件恢复未确认'] if self.active.get('demo_recovery_required') else []
        for role in self.active['clock_intents']:
            try:
                result=self.backend.call(role,'clock_cleanup',id=self.active['id'])
                original=self.active.get('clock_originals',{}).get(role,self.active['nodes'].get(role,{}).get('time_config'))
                if not result['removed'] or result['service'].strip()!='active' or (original is not None and result['config']!=original):
                    raise RuntimeError('原时间配置/服务未恢复')
                results[role]=result
            except Exception as exc:errors.append(role+': '+str(exc))
        if self.backend:
            try:self.backend.close()
            except Exception as exc:errors.append('参考/SSH清理：'+str(exc))
        self.backend=None;self.active['cleanup']={'confirmed':not errors,'errors':errors,'nodes':results,
             'radio_policy':'保留已恢复的无线/FIFO基线；不卸回原07、不清理其他进程'}
        self.active['status']='recovery_required' if errors else 'failed_clean' if failed or self.active.get('error') else 'ended'
        if not errors:self._release()
        self.event(self.active['status'],'清理未确认，需重新输入凭据恢复' if errors else '本会话时间设置已恢复，参考已停止')

    def recover(self,identity,password,sudo_password):
        if any(not isinstance(v,str) or len(v)>1024 for v in (password,sudo_password)):
            raise ValueError('凭据格式无效')
        with self.lock:
            if self.thread and self.thread.is_alive():raise ValueError('当前准备仍在运行')
            s=self.get(identity)
            if s['status']!='recovery_required':raise ValueError('仅恢复中断/清理未确认会话')
            self.backend=self.factory(s['platform'],password,sudo_password or password)
            self.active=s;self.cancel=threading.Event()
            self.active.update(status='recovering',error=None)
            self.event('recovering','正在核对中断会话及设备当前状态')
            self._launch(self._recover_cleanup)
            return self.get(identity)

    def _recover_cleanup(self):
        if self.active.get('demo_recovery_required'):
            rows=self._all('inspect')
            before_nodes=self.active['nodes']
            # Keep a current diagnostic snapshot separate from the saved baseline.
            self.active['recovery_inspection']=rows;self._save()
            before_boots={role:before_nodes.get(role,{}).get('boot_id') for role in self.roles}
            after_boots={role:rows[role].get('boot_id') for role in self.roles}
            rebooted=[role for role in self.roles
                      if before_boots[role] and after_boots[role] and before_boots[role]!=after_boots[role]]
            if len(rebooted)==len(self.roles):
                check_inventory(rows)
                if any(row['demo_monitors'] for row in rows.values()):
                    raise RuntimeError('仍有演示监听接口，暂不能释放设备占用')
                self.active.update(demo_recovery_required=False,demo_owner=None)
                self.event('demo_recovery_checked','六节点均已重启，当前无业务、抓包、追踪、演示监听接口或运行中统计。继续核对授时清理。',rebooted=rebooted)
                return self._cleanup(failed=True)
            if rebooted:
                raise RuntimeError('仅部分设备重启（'+', '.join(rebooted)+'），其余设备的旧会话恢复仍需核对')
            check_radio(rows)
            for role, before in before_nodes.items():
                after=rows[role]
                if after['boot_id']!=before['boot_id']:
                    raise RuntimeError(role+' boot identity changed during interrupted demo')
                if role.startswith('STA'):
                    for field in ('override_enabled','override_valid'):
                        if before['BE'][field]!=after['BE'][field]:raise RuntimeError(role+' CW override not restored')
                    threshold=lambda node:re.search(r'RTS thr[:=](off|\d+)',node['iwconfig'])[1]
                    if before['override']!=after['override'] or threshold(before)!=threshold(after):
                        raise RuntimeError(role+' Basic/RTS baseline not restored')
            self.active.update(demo_recovery_required=False,demo_owner=None)
            self.event('demo_recovery_checked','六节点原无线和CW基线已读回，未发现本次业务进程或统计窗口')
        self._cleanup(failed=True)

    def path(self,identity):
        if not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{32}',identity):raise ValueError('准备标识无效')
        p=self.root/identity/'session.json'
        if p.is_symlink() or p.parent.is_symlink() or not p.is_file():raise ValueError('找不到准备会话')
        return p

    def get(self,identity):
        with self.lock:
            p=self.path(identity)
            s=json.loads(p.read_text(encoding='utf-8'))
            age=(time.time_ns()-s['last_check_ns'])/1e9 if s.get('last_check_ns') else None
            s['check_age_s']=age
            s['preparation_ready']=s.get('preparation_ready',False) and age is not None and 0<=age<25
            s['fresh']=age is not None and 0<=age<25
            return s

    def listing(self):
        with self.lock:
            rows=[]
            for p in self.root.glob('*/session.json'):
                if p.is_symlink() or p.parent.is_symlink():continue
                s=self.get(p.parent.name)
                rows.append({k:s.get(k) for k in ('id','request_key','status','stage','created_ns',
                                                  'preparation_ready','demo_owner','demo_recovery_required','error')})
            return sorted(rows,key=lambda r:r['created_ns'],reverse=True)

    def close(self):
        self.closed=True;self.cancel.set()
        if self.thread:self.thread.join(45)
        if self.backend and (not self.thread or not self.thread.is_alive()):self._cleanup()
