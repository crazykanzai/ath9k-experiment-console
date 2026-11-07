"""Optional sixth ready/armed participant, isolated from business sender sets."""
import hashlib,json,re,shlex,time
from pathlib import Path
import paramiko
from sync_run import Remote,probe,correct_clock,write
from continuous_ntp import require as require_ntp_source
class PassiveRemote(Remote):
    def __init__(self,spec,password):
        self.name='MON15';self.address=spec['address'];self.password=password
        self.ssh=paramiko.SSHClient();self.ssh.load_system_host_keys();self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(self.address,username=spec['username'],password=password,timeout=8,auth_timeout=8,banner_timeout=8)
        self.sftp=self.ssh.open_sftp()
class PassiveSession:
    def __init__(self,spec_file,folder,password):
        self.spec=json.loads(Path(spec_file).read_text(encoding='utf-8-sig'));self.folder=folder
        if self.spec['address']!='192.168.100.15' or self.spec['username']!='abc' or not re.fullmatch('[a-zA-Z0-9_]+',self.spec['iface']):raise ValueError('unexpected passive node/iface')
        self.remote=PassiveRemote(self.spec,password)
        token=hashlib.sha256(str(folder.resolve()).encode()).hexdigest()[:14]
        self.out='/home/EXPERIMENT_USER/apsta-air/'+token;self.script='/home/EXPERIMENT_USER/apsta-air/passive_listener_node.py';self.record={'spec':self.spec,'remote_output':self.out,'clock_checks':[]}
        if self.remote.run('test -e '+self.out)['code']==0:raise RuntimeError('passive remote output already exists')
    def need(self,z):
        if z['code']:raise RuntimeError('passive command: '+repr(z))
        return z['stdout'].strip()
    def read_state(self):
        data=self.remote.file(self.out+'/state.json')
        return json.loads(data) if data else None
    def arm(self):
        source=getattr(self,'spec',{}).get('continuous_ntp_source')
        if source:self.record['continuous_NTP_before']=require_ntp_source(self.remote,source)
        for attempt in range(3):
            c=probe(self.remote);self.record['clock_checks'].append(c)
            if c['within_50ms_proven'] and abs(c['best']['offset_ns'])<=10_000_000:break
            if source:raise RuntimeError('passive continuous NTP 10ms margin gate; no manual recentering')
            if attempt==2:raise RuntimeError('passive clock gate')
            self.record.setdefault('clock_corrections',[]).append(correct_clock(self.remote,c['best']['offset_ns']))
        self.record['clock_before']=c
        self.need(self.remote.run('mkdir -p /home/EXPERIMENT_USER/apsta-air'))
        source=Path(__file__).with_name('passive_listener_node.py');self.remote.sftp.put(str(source),self.script)
        self.remote.sftp.put(str(Path(__file__).with_name('window_observation.py')),'/home/EXPERIMENT_USER/apsta-air/window_observation.py')
        self.record['worker_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
        command='nohup python3 '+self.script+' arm --out '+self.out+' --iface '+self.spec['iface']+' --frequency '+str(self.spec['frequency'])+' >'+self.out+'.stdout 2>'+self.out+'.stderr </dev/null &'
        self.need(self.remote.run('sh -c '+shlex.quote(command),sudo=True));deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            s=self.read_state()
            if s and s.get('errors'):raise RuntimeError('passive arm failure '+repr(s))
            if s and s.get('armed') and not s.get('finished'):self.record['armed']=s['armed'];self.save();return
            time.sleep(.2)
        raise RuntimeError('passive armed timeout')
    def save(self):write(self.folder/'controller/passive_listener.json',self.record)
    def ready(self):
        s=self.read_state()
        if not s or not s.get('armed') or s.get('finished') or s.get('errors'):raise RuntimeError('passive not armed/alive')
    def send(self,action,plan=None):
        suffix=''
        if plan is not None:
            path=self.out+'.plan.json'
            with self.remote.sftp.open(path,'w') as f:f.write(json.dumps(plan))
            suffix=' --plan-file '+path
        z=self.remote.run('python3 '+self.script+' send --out '+self.out+' --action '+action+suffix,sudo=True)
        reply=json.loads(self.need(z))
        if not reply.get('ok'):raise RuntimeError('passive rejected plan/abort '+repr(reply))
        self.record.setdefault('control_replies',[]).append({'action':action,'reply':reply,'controller_ns':time.time_ns()})
        if action=='abort':self.record['abort_ack']=reply
        self.save()
        return reply
    def poll(self):
        s=self.read_state()
        if not s or s.get('errors'):raise RuntimeError('passive capture failure '+repr(s))
    def finish(self):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            s=self.read_state()
            if s and s.get('finished'):
                self.record['final_state']=s;self.record['clock_after']=probe(self.remote,5);self.save()
                if not s.get('valid') or s.get('errors') or not self.record['clock_after']['within_50ms_proven']:raise RuntimeError('passive finish/clock gate')
                source=getattr(self,'spec',{}).get('continuous_ntp_source')
                if source:self.record['continuous_NTP_after']=require_ntp_source(self.remote,source);self.save()
                return
            time.sleep(.2)
        raise RuntimeError('passive finish timeout')
    def cleanup(self):
        try:
            s=self.read_state()
            if s and not s.get('finished'):
                # An acknowledged abort can already have closed control.sock
                # while its final state is still being persisted.
                if not self.record.get('abort_ack'):self.send('abort')
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    s=self.read_state()
                    if s and s.get('finished'):break
                    time.sleep(.2)
                else:raise RuntimeError('passive cleanup not confirmed')
            self.record['cleanup_state']=s
            self.remote.get_tree(self.out,self.folder/'raw/MON15')
            for suffix in ['stdout','stderr']:
                value=self.remote.file(self.out+'.'+suffix)
                if value is not None:(self.folder/'raw/MON15'/('worker_'+suffix+'.txt')).write_text(value,encoding='utf-8')
            self.save()
        finally:self.remote.close()
