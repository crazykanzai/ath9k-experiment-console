"""Preparation safety and lifecycle tests use an in-memory backend, never SSH."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from labconsole.config import default_platform
from labconsole.preparation import PreparationHub, ROLES, business_cidr, check_inventory, check_radio
from labconsole.prepare_node import CANDIDATE, ORIGINAL, VERSION, time_path, time_text
from labconsole.real_demo import DemoHub, Session
from labconsole.server import ConsoleServer
from labconsole.storage import Store, atomic_json
from labconsole.time_reference import response, stamp


def inventory():
    rows={}
    for i,role in enumerate(ROLES):
        rows[role]={'interface':'wlp2s0','phy':'phy0',
          'info':'addr 00:26:b6:f7:78:e7\nssid ath9k_test\ntype '+('AP' if role=='AP' else 'managed')+
                 '\nchannel 1 (2412 MHz), width: 20 MHz (no HT)\ntxpower 14.00 dBm',
          'link':'Connected to 00:26:b6:f7:78:e7','power_save':'Power save: off',
          'route':'192.168.100.2 dev enp2s0 src 192.168.100.'+str(10+i)+' ',
          'ips':['192.168.50.'+str(i+1)+'/24'],'profile_uuids':['original-profile'],
          'boot_id':'same-boot','kernel':'5.15.0-139-generic','srcversion':VERSION,
          'busy':'','services':'active\nactive\nactive\n','time_config':'original time config',
          'stats_header':'hol_abi=1 basic_retry_abi=2 running=0','candidate_sha':CANDIDATE,
          'original_sha':ORIGINAL,'rollback_sha':ORIGINAL,'candidate_info':'5.15.0-139-generic SMP',
          'parameter':'20','mask':'0x100','BE':{'hw_aifs':'3','hw_cwmin':'15','hw_cwmax':'1023','status_name':'verified'},
          'reg':{'values':{'0x30':'0x140','0x1070':hex(9*44),'0x1030':hex(8*44)},'selector_restored':True}}
    return rows


class FakeBackend:
    def __init__(self,platform,password,sudo):
        self.platform=platform;self.calls=[];self.rows=inventory();self.reference=False
        self.closed=False;self.envelope=1.;self.fail_cleanup=False;self.owned=set()

    def call(self,role,op,**args):
        self.calls.append((role,op,args))
        if op=='inspect':return copy.deepcopy(self.rows[role])
        if op=='radio':return {'before':copy.deepcopy(self.rows[role]),'after':copy.deepcopy(self.rows[role])}
        if op=='clock_apply':
            self.owned.add(role);return {'original_config':'original time config'}
        if op=='clock_cleanup':
            if self.fail_cleanup:raise RuntimeError('simulated cleanup failure')
            self.owned.discard(role)
            return {'removed':True,'service':'active','config':'original time config'}
        raise AssertionError(op)

    def clock(self,role):
        return {'offset_ms':.5,'uncertainty_ms':.5,'envelope_ms':self.envelope,
                'status':'NTPSynchronized=yes\nServer: 192.168.100.2 (192.168.100.2)',
                'wall_ns':100_000_000_000,'monotonic_ns':50_000_000_000}

    def start_reference(self):self.reference=True
    def reference_ok(self):return self.reference
    def close(self):self.reference=False;self.closed=True


def wait_state(hub,identity,wanted):
    end=time.monotonic()+7
    while time.monotonic()<end:
        state=hub.get(identity)
        if state['status'] in wanted:return state
        time.sleep(.01)
    raise AssertionError(hub.get(identity))


def fast_wait(seconds,cancel):
    if seconds==10:cancel.wait(10)  # Hold the production monitor, not its acceptance checks.


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.hub=PreparationHub(self.store,FakeBackend,wait=fast_wait)
        self.addCleanup(self.hub.close)

    def create(self):
        v=self.hub.create(default_platform(),'ephemeral-test-password','ephemeral-test-sudo','request-key-1234')
        wait_state(self.hub,v['id'],{'checked','failed_clean'});return v['id']

    def radio(self):
        identity=self.create();self.hub.action(identity,'restore')
        self.assertEqual(wait_state(self.hub,identity,{'radio_ready','failed_clean'})['status'],'radio_ready')
        return identity

    def test_full_lifecycle_holds_and_releases(self):
        identity=self.radio();backend=self.hub.backend
        self.hub.action(identity,'sync');v=wait_state(self.hub,identity,{'ready','failed_clean'})
        self.assertEqual(v['status'],'ready');self.assertTrue(v['preparation_ready'])
        self.assertFalse(v['experiment_ready']);self.assertFalse(v['traffic_started'])
        self.assertEqual(v['timing'],{'settle_s':30,'hold_s':0,'sample_s':0,'monitor_s':10})
        self.assertEqual(v['completed_settle_s'],30)
        self.assertEqual(v['completed_hold_s'],0);self.assertEqual(len(v['clock_samples']),1)
        self.assertNotIn('holding',[e['stage'] for e in v['events']])
        with self.assertRaises(ValueError):self.store.save_platform(default_platform())
        self.hub.action(identity,'stop');v=wait_state(self.hub,identity,{'ended'})
        self.assertTrue(v['cleanup']['confirmed']);self.assertTrue(backend.closed)
        self.assertFalse(backend.owned);self.store.save_platform(default_platform())
        self.assertEqual(self.hub.action(identity,'stop')['status'],'ended')

    def test_basic_then_rts_reuse_same_preparation_after_recovery(self):
        identity=self.radio()
        self.hub.action(identity,'sync')
        wait_state(self.hub,identity,{'ready'})
        backend=self.hub.backend
        calls_before=list(backend.calls)
        demo=DemoHub(self.store.root,self.hub)
        self.addCleanup(demo.close)

        def stop(session, rows, ips):
            session.abort('用户停止演示')
            session.check_cancel()

        with patch.object(Session,'preflight',return_value=({},{})), \
             patch.object(Session,'traffic',stop), \
             patch.object(Session,'cleanup',return_value=(True,[])), \
             patch('labconsole.real_demo.shutil.disk_usage') as disk:
            disk.return_value.free=40*1024**3
            for access in ('basic','rts'):
                session=demo.start({'preparation_id':identity,'request_key':'reuse-test-'+access,
                                    'mode':'fixed','access_mode':access,'authorize':True})
                demo.thread.join(5)
                self.assertFalse(demo.thread.is_alive())
                record=demo.get(session['id'])
                self.assertEqual(record['status'],'stopped')
                self.assertEqual(record['request']['access_mode'],access)
                state=demo.listing()
                self.assertEqual(state['ready_preparation'],identity)
                self.assertFalse(state['demo_busy'])
                self.assertFalse(state['preparation_recovery_required'])
        self.assertIs(self.hub.backend,backend)
        self.assertTrue(backend.reference)
        self.assertEqual(backend.calls,calls_before)
        self.hub.action(identity,'stop')
        wait_state(self.hub,identity,{'ended'})
        self.assertIsNone(demo.listing()['ready_preparation'])

    def test_no_password_persistence_and_idempotence(self):
        identity=self.create()
        same=self.hub.create(default_platform(),'other','other','request-key-1234')
        self.assertEqual(identity,same['id'])
        with self.assertRaises(ValueError):self.hub.create(default_platform(),'','','request-key-5678')
        value=(self.hub.root/identity/'session.json').read_text(encoding='utf-8')
        self.assertNotIn('ephemeral-test-password',value);self.assertNotIn('ephemeral-test-sudo',value)
        self.assertEqual({c[1] for c in self.hub.backend.calls},{'inspect'})

    def test_numeric_clock_failure_is_preserved_and_cleaned(self):
        identity=self.radio();backend=self.hub.backend;backend.envelope=70
        self.hub.action(identity,'sync');v=wait_state(self.hub,identity,{'failed_clean'})
        self.assertEqual(v['clock_samples'][0]['nodes']['STA2']['envelope_ms'],70)
        self.assertFalse(v['preparation_ready']);self.assertFalse(backend.owned)
        self.assertEqual(sum(c[1]=='clock_apply' for c in backend.calls),6)

    def test_short_profile_keeps_initial_ten_ms_gate(self):
        identity=self.radio();self.hub.backend.envelope=11
        self.hub.action(identity,'sync');v=wait_state(self.hub,identity,{'failed_clean'})
        self.assertFalse(v['preparation_ready']);self.assertEqual(len(v['clock_samples']),1)

    def check_hold_failure(self,failure):
        self.hub.timing=(30,45,15,10)
        identity=self.radio();backend=self.hub.backend;clock=backend.clock;counts={}
        def sample(role):
            counts[role]=counts.get(role,0)+1;row=clock(role)
            if role=='STA3' and counts[role]>1:
                if failure=='offset':row['envelope_ms']=51
                else:row['wall_ns']+=3_000_000
            return row
        backend.clock=sample
        self.hub.action(identity,'sync');v=wait_state(self.hub,identity,{'failed_clean'})
        self.assertFalse(v['preparation_ready']);self.assertTrue(v['cleanup']['confirmed'])
        self.assertIn('时间/源门禁' if failure=='offset' else '墙钟跳变',v['error'])

    def test_short_profile_keeps_hold_fifty_ms_gate(self):
        self.check_hold_failure('offset')

    def test_short_profile_keeps_hold_jump_gate(self):
        self.check_hold_failure('jump')

    def test_explicit_long_profile_and_session_timing_are_preserved(self):
        self.hub.timing=(90,300,30,10)
        identity=self.radio()
        # A session uses its recorded policy even if the next-session default changes.
        self.hub.timing=(30,0,0,10)
        self.hub.action(identity,'sync');v=wait_state(self.hub,identity,{'ready'})
        self.assertEqual(v['timing']['settle_s'],90);self.assertEqual(v['completed_settle_s'],90)
        self.assertEqual(v['completed_hold_s'],300);self.assertEqual(len(v['clock_samples']),11)

    def test_short_profile_wait_budget_and_continued_monitoring(self):
        identity=self.radio();backend=self.hub.backend;waits=[]
        def wait(seconds,cancel):
            waits.append(seconds)
            if seconds==10:backend.envelope=51
        self.hub.wait=wait
        self.hub.action(identity,'sync');v=wait_state(self.hub,identity,{'failed_clean'})
        self.assertEqual(waits,[30,10])
        self.assertTrue(any(e['stage']=='ready' for e in v['events']))
        self.assertEqual(len(v['clock_samples']),2)
        self.assertFalse(v['preparation_ready']);self.assertTrue(v['cleanup']['confirmed'])

    def test_hold_progress_updates_each_second_without_advancing_verified_samples(self):
        self.radio();self.hub.thread.join(2);self.hub.wait=None
        elapsed=[0];progress=[]
        def tick(seconds):elapsed[0]+=seconds;return False
        def save():progress.append(self.hub.active.get('observed_hold_s'))
        with patch('labconsole.preparation.time.monotonic',side_effect=lambda:elapsed[0]), \
             patch.object(self.hub.cancel,'wait',side_effect=tick),patch.object(self.hub,'_save',side_effect=save):
            self.hub._delay(3,progress_field='observed_hold_s',progress_offset=15)
        self.assertEqual(progress,[16,17,18,18])
        self.assertEqual(self.hub.active['completed_hold_s'],0)

    def test_cleanup_failure_retains_reservation_and_explicit_recovery(self):
        identity=self.radio();backend=self.hub.backend;backend.fail_cleanup=True
        self.hub.action(identity,'sync');wait_state(self.hub,identity,{'ready'})
        self.hub.action(identity,'stop');wait_state(self.hub,identity,{'recovery_required'})
        with self.assertRaises(ValueError):self.store.save_platform(default_platform())
        self.hub.recover(identity,'reentered','reentered');v=wait_state(self.hub,identity,{'failed_clean'})
        self.assertTrue(v['cleanup']['confirmed']);self.store.save_platform(default_platform())

    def test_reboot_recovery_releases_only_after_quiet_and_clock_cleanup(self):
        for block in ('', 'live monitor', 'active stats', 'partial reboot'):
            with self.subTest(block=block), tempfile.TemporaryDirectory() as folder:
                store=Store(folder)
                hub=PreparationHub(store,FakeBackend,wait=fast_wait)
                try:
                    created=hub.create(default_platform(),'test','test','reboot-recovery-key')
                    identity=created['id']
                    state=wait_state(hub,identity,{'checked'})
                    hub.thread.join(2)
                    hub.backend.close()
                    hub.backend=None
                    state.update(status='recovery_required',demo_recovery_required=True,
                                 clock_intents=list(ROLES))
                    atomic_json(hub.path(identity),state)
                    def factory(*args):
                        backend=FakeBackend(*args)
                        for role,row in backend.rows.items():
                            row.update(boot_id='new-boot',info='type managed',ips=[],demo_monitors=[])
                        if block=='live monitor':backend.rows['MON15']['demo_monitors']=['acm12345678']
                        if block=='active stats':backend.rows['STA1']['stats_header']='running=1'
                        if block=='partial reboot':backend.rows['STA1']['boot_id']='same-boot'
                        return backend
                    hub.factory=factory
                    hub.recover(identity,'test','test')
                    hub.thread.join(3)
                    self.assertFalse(hub.thread.is_alive())
                    recovered=hub.get(identity)
                    self.assertEqual(recovered['status'],'recovery_required' if block else 'failed_clean')
                    self.assertEqual(recovered['cleanup']['confirmed'],not block)
                    self.assertEqual(recovered['nodes']['AP']['boot_id'],'same-boot')
                    self.assertEqual(recovered['recovery_inspection']['AP']['boot_id'],'new-boot')
                    if block:
                        with self.assertRaises(ValueError):store.save_platform(default_platform())
                    else:
                        self.assertEqual(set(recovered['cleanup']['nodes']),set(ROLES))
                        store.save_platform(default_platform())
                finally:
                    hub.close()

    def test_restart_marks_unfinished_and_does_not_connect(self):
        identity=self.create();self.hub.thread.join(2)
        replacement=PreparationHub(self.store,lambda *a:(_ for _ in ()).throw(AssertionError('must not connect')))
        self.addCleanup(replacement.close)
        self.assertEqual(replacement.get(identity)['status'],'recovery_required')
        self.assertFalse(replacement.get(identity)['preparation_ready'])
        with self.assertRaises(ValueError):replacement.create(default_platform(),'','','new-request-key')

    def test_stale_ready_is_not_green(self):
        identity=self.radio();self.hub.action(identity,'sync');wait_state(self.hub,identity,{'ready'})
        with self.hub.lock:
            self.hub.active['last_check_ns']=time.time_ns()-26_000_000_000;self.hub._save()
        self.assertFalse(self.hub.get(identity)['preparation_ready'])

    def test_cancel_during_settle_cleans_owned_time_files(self):
        identity=self.radio();backend=self.hub.backend;entered=threading.Event()
        def wait(seconds,cancel):
            entered.set();cancel.wait(5)
        self.hub.wait=wait;self.hub.action(identity,'sync');self.assertTrue(entered.wait(3))
        self.hub.action(identity,'stop');v=wait_state(self.hub,identity,{'ended'})
        self.assertFalse(backend.owned);self.assertTrue(v['cleanup']['confirmed'])
        self.assertIsNone(v['error']);self.assertEqual(v['completed_hold_s'],0)

    def test_unregistered_resource_profile_cannot_restore(self):
        platform=default_platform();platform['driver']['expected_srcversion']='other'
        with self.assertRaises(ValueError):self.hub.create(platform,'','','resource-test-key')

    def test_inventory_and_radio_gates(self):
        for field,value in [('busy','user iperf'),('trace_busy','p:apsta_existing'),('candidate_sha','bad'),('stats_header','running=1')]:
            rows=inventory();rows['STA1'][field]=value
            with self.subTest(field=field),self.assertRaises(RuntimeError):check_inventory(rows)
        rows=inventory();rows['STA2']['reg']['values']['0x30']='0x100'
        with self.assertRaises(RuntimeError):check_radio(rows)
        rows=inventory();rows['STA3']['link']='Not connected'
        with self.assertRaises(RuntimeError):check_radio(rows)
        self.assertEqual(check_radio(inventory()),'00:26:b6:f7:78:e7')

    def test_management_subnet_and_invalid_cidr_rejected(self):
        for value in ['192.168.100.1/24','8.8.8.1/24','192.168.50.1/32','invalid']:
            with self.subTest(value=value),self.assertRaises(ValueError):business_cidr(value,default_platform())
        self.assertEqual(business_cidr('192.168.50.1/24',default_platform()),'192.168.50.1/24')

    def test_no_automatic_radio_on_check(self):
        identity=self.create();backend=self.hub.backend
        self.assertEqual({c[1] for c in backend.calls},{'inspect'})
        self.hub.action(identity,'stop');wait_state(self.hub,identity,{'ended'})
        self.assertEqual({c[1] for c in backend.calls},{'inspect'})

    def test_partial_ssh_error_is_preserved(self):
        class Failed(FakeBackend):
            def call(self,role,op,**args):
                if role=='STA3':raise OSError('SSH unavailable')
                return super().call(role,op,**args)
        self.hub.factory=Failed;identity=self.create();v=wait_state(self.hub,identity,{'failed_clean'})
        self.assertIn('STA3',v['inspection_errors']);self.assertIn('AP',v['partial_inspection'])
        self.assertFalse(v['traffic_started'])


class PreparationHTTPTests(unittest.TestCase):
    def test_explicit_actions_csrf_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            server=ConsoleServer(('127.0.0.1',0),tmp)
            server.preparation.factory=FakeBackend;server.preparation.wait=fast_wait
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(path,body=None,token=None):
                c=http.client.HTTPConnection('127.0.0.1',server.server_port)
                c.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),
                          {'X-Console-Token':token or server.csrf})
                r=c.getresponse();value=r.read();status=r.status;c.close();return status,value
            try:
                self.assertEqual(request('/api/preparation/check',{},token='bad')[0],403)
                status,value=request('/api/preparation/check',{'password':'pw-not-saved','request_key':'http-prep-key'})
                self.assertEqual(status,200);identity=json.loads(value)['id']
                wait_state(server.preparation,identity,{'checked'})
                self.assertEqual(request('/api/preparation/action',{'id':identity,'action':'restore'})[0],400)
                self.assertEqual(request('/api/start',{'mode':'live'})[0],400)
                self.assertEqual(request('/api/preparation/action',{'id':identity,'action':'restore','allow_temporary_restore':True})[0],200)
                wait_state(server.preparation,identity,{'radio_ready'})
                status,value=request('/api/preparation/export?id='+identity)
                self.assertEqual(status,200);self.assertNotIn(b'pw-not-saved',value)
                self.assertEqual(request('/api/preparation?id=../../oops')[0],400)
                request('/api/preparation/action',{'id':identity,'action':'stop'})
                wait_state(server.preparation,identity,{'ended'})
            finally:server.shutdown();server.server_close();thread.join(3)


class ReferenceAndOwnershipTests(unittest.TestCase):
    def test_recreated_wireless_waits_for_networkmanager_before_activation(self):
        from labconsole import prepare_node
        calls=[]
        states=iter(['20 (unavailable)','30 (disconnected)'])
        def run(args,**kwargs):
            calls.append(args)
            if args[:3]==['nmcli','-g','GENERAL.STATE']:return next(states)
            if args[:2]==['iw','dev']:return 'Not connected.\n'
            return ''
        with patch.object(prepare_node,'run',side_effect=run),patch.object(prepare_node.time,'sleep'),patch.object(prepare_node.time,'monotonic',side_effect=[0,15]):
            prepare_node.activate_profile('wlp2s0','original-profile','00:11:22:33:44:55')
        self.assertEqual(calls[:2],[['nmcli','-g','GENERAL.STATE','device','show','wlp2s0']]*2)
        self.assertEqual(calls[-1],['nmcli','connection','up','uuid','original-profile','ifname','wlp2s0'])

    def test_unavailable_wireless_has_bounded_wait_without_activation(self):
        from labconsole import prepare_node
        with patch.object(prepare_node,'run',return_value='20 (unavailable)') as run,patch.object(prepare_node.time,'monotonic',side_effect=[0,31]):
            with self.assertRaisesRegex(RuntimeError,'readiness timeout'):
                prepare_node.activate_profile('wlp2s0','original-profile','00:11:22:33:44:55')
        self.assertEqual(run.call_count,1)

    def test_reference_uses_client_originate_and_server_timestamps(self):
        request=bytearray(48);request[0]=35;request[2]=4;request[40:48]=b'12345678'
        v=response(request,1_000_000_000,1_001_000_000)
        self.assertEqual(v[24:32],b'12345678');self.assertEqual(v[32:40],stamp(1_000_000_000))
        self.assertEqual(v[0]&7,4);self.assertEqual(v[1],1)
        self.assertIsNone(response(b'bad',0,0));request[0]=36;self.assertIsNone(response(request,0,0))

    def test_time_override_path_and_content_are_bounded(self):
        with self.assertRaises(ValueError):time_path('../../other')
        with self.assertRaises(ValueError):time_text('8.8.8.8')
        self.assertTrue(str(time_path('a'*32)).endswith('90-ath9k-console-'+('a'*32)+'.conf'))
        self.assertIn('NTP=192.168.100.2\n',time_text('192.168.100.2'))

    def test_cleanup_does_not_remove_replaced_file(self):
        from labconsole import prepare_node
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'owned.conf';p.write_text('other owner')
            with patch.object(prepare_node,'time_path',return_value=p),self.assertRaises(RuntimeError):
                prepare_node.dispatch({'op':'clock_cleanup','id':'a'*32,'controller_ip':'192.168.100.2'})
            self.assertEqual(p.read_text(),'other owner')


if __name__=='__main__':unittest.main()
