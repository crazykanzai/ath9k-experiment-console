import concurrent.futures
import hashlib
import http.client
import json
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from labconsole.config import default_platform, validate_platform
from labconsole.plans import build_plan, digest
from labconsole.reports import describe, import_report
from labconsole.server import ConsoleServer, local_check
from labconsole.storage import Store
from labconsole.worker import execute, launch


def fixture(path):
    rows = []
    for n in range(1, 5):
        for block in range(1, 6):
            missing = n == 3 and block == 2
            rows.append({'order': len(rows)+1, 'block': block, 'sender': 'STA'+str(n),
                'folder': 'old-root/batch/round'+str(len(rows)+1),
                'S_Mbps': 10+block/10, 'HoL_success_us': 950+block,
                'p_ACK_original': None if missing else .9, 'p_ACK_aux_full': .89 if missing else .9,
                'final_MAC_delivery': .999 if missing else 1., 'queue_nonempty': 1., 'queue_backlog': 1.,
                'complete_packets': 1000, 'complete_failures': 2 if missing else 0,
                'boundary_or_cross': 3, 'unknown_start': 1, 'right_censored': 0,
                'auxiliary_formula': {'quality': 'approximate'} if missing else None})
    path.write_text(json.dumps({'status': 'completed_twenty_formal', 'valid_windows': 20,
                              'planned_windows': 20, 'rows': rows}), encoding='utf-8')


class ConfigPlanTests(unittest.TestCase):
    def test_platform_validation_and_no_secrets(self):
        p=default_platform();self.assertEqual(validate_platform(p),p)
        p['nodes'][1]['address']=p['nodes'][0]['address']
        with self.assertRaises(ValueError):validate_platform(p)
        p=default_platform();p['notes']={'nested':{'password':'do-not-export'}}
        with self.assertRaises(ValueError):validate_platform(p)
        p=default_platform();p['controller']['management_ip']=p['nodes'][0]['address']
        with self.assertRaises(ValueError):validate_platform(p)
        for bad in [[],None,{'schema_version':1,'name':'bad','nodes':[None]*6}]:
            with self.assertRaises(ValueError):validate_platform(bad)

    def test_fixed_twenty_and_all_cw(self):
        a=build_plan({});b=build_plan({})
        self.assertEqual(digest(a),digest(b))
        self.assertEqual([x['active_stas'][0] for x in a['formal_inputs'][:4]],['STA2','STA1','STA4','STA3'])
        for n in range(1,5):self.assertEqual(sum(r['active_stas']==['STA'+str(n)] for r in a['formal_inputs']),5)
        p=build_plan({'template':'cw'})
        self.assertEqual(len(p['formal_inputs']),25)
        for w in [8,16,32,64,128]:
            rows=[r for r in p['formal_inputs'] if r['W']==w]
            self.assertEqual(len(rows),5)
            self.assertTrue(all(r['cwmin']==w-1 and r['cwmax']==8*w-1 for r in rows))

    def test_nodes_and_power_are_explicit(self):
        p=build_plan({'template':'nodes'})
        self.assertEqual(len(p['prechecks']),7)
        for n in range(1,5):self.assertEqual(sum(len(r['active_stas'])==n for r in p['formal_inputs']),5)
        self.assertEqual(build_plan({'template':'power'})['parameters']['STA_power_dBm'],5)
        self.assertEqual(build_plan({})['timing']['measure_s'],180)

    def test_invalid_parameters_fail(self):
        for p in [{'W':9},{'repeats':True},{'repeats':0},{'template':'rts'},{'sender':'STA9'},{'seed':-1}]:
            with self.assertRaises(ValueError):build_plan(p)
        with self.assertRaises(ValueError):build_plan([])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'任意目录 with spaces')

    def create(self,**kw):return self.store.create(build_plan({'repeats':1}), 'request-key-123',delay=.01,**kw)[0]

    def test_idempotence_and_endpoint_exclusion(self):
        plan=build_plan({'repeats':1})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            values=list(pool.map(lambda _:self.store.create(plan,'same-request-key',delay=.01),range(2)))
        self.assertEqual(values[0][0],values[1][0]);self.assertEqual(sum(v[1] for v in values),1)
        with self.assertRaises(ValueError):self.store.create(plan,'different-request-key')
        with self.assertRaises(ValueError):self.store.save_platform(default_platform())
        self.assertTrue(self.store.claim(values[0][0]));self.assertFalse(self.store.claim(values[0][0]))

    def test_complete_has_zero_formal_and_no_network(self):
        job=self.create()
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
            execute(self.store.root,job)
        state=self.store.job(job);self.assertEqual(state['status'],'completed')
        m=json.loads((self.store.root/'runs'/job/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(m['formal_valid'],0);self.assertEqual(m['simulated_complete'],4)
        self.assertEqual(m['remote_actions'],[])
        rows=json.loads((self.store.root/'runs'/job/'simulation_records.json').read_text(encoding='utf-8'))
        self.assertTrue(all(x['S_Mbps'] is None and x['p_ACK_original'] is None for x in rows))
        # Finished job releases reservations; repeating execute does nothing.
        execute(self.store.root,job)
        self.store.create(build_plan({'repeats':1}),'new-request-after-finish',delay=.01)

    def test_gate_failure_stops_without_replacement(self):
        job=self.create(fault_at=2);execute(self.store.root,job)
        self.assertEqual(self.store.job(job)['status'],'gate_failed')
        rows=json.loads((self.store.root/'runs'/job/'simulation_records.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows),2);self.assertEqual(rows[-1]['status'],'simulated_gate_failure')

    def test_tampered_plan_refused(self):
        job=self.create();p=self.store.root/'runs'/job/'plan.json'
        p.write_text('{}');execute(self.store.root,job)
        self.assertEqual(self.store.job(job)['status'],'worker_failed')
        self.assertEqual(self.store.job(job)['progress'],0)

    def test_immediate_stop(self):
        job=self.create();self.store.stop(job,'immediate');execute(self.store.root,job)
        self.assertEqual(self.store.job(job)['status'],'stopped');self.assertEqual(self.store.job(job)['progress'],0)

    def test_after_round_stop_preserves_completed_round(self):
        job=self.store.create(build_plan({'repeats':1}),'after-round-request',delay=.08)[0]
        thread=threading.Thread(target=execute,args=(self.store.root,job));thread.start()
        try:
            deadline=time.monotonic()+5
            while self.store.job(job)['stage']!='simulated_measurement':
                if time.monotonic()>deadline:self.fail('worker did not reach measurement')
                time.sleep(.01)
            self.store.stop(job,'after_round')
        finally:thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(self.store.job(job)['status'],'stopped')
        self.assertEqual(self.store.job(job)['progress'],1)

    def test_detached_process_survives_without_browser(self):
        job=self.create();p=launch(self.store,job)
        self.assertEqual(p.wait(timeout=12),0)
        self.assertEqual(Store(self.store.root).job(job)['status'],'completed')

    def test_platform_hash_rechecked_inside_creation_transaction(self):
        original=digest(self.store.platform())
        changed=default_platform();changed['name']='changed after preview';self.store.save_platform(changed)
        with self.assertRaises(ValueError):
            self.store.create(build_plan({}),'concurrent-platform-change',expected_platform_hash=original)

    def test_portable_without_git_virtualenv_or_site_packages(self):
        root=Path(__file__).resolve().parents[2]
        moved=Path(self.temp.name)/'另一台电脑 other drive';moved.mkdir()
        shutil.copytree(root/'labconsole',moved/'labconsole',ignore=shutil.ignore_patterns('__pycache__','acceptance','*.log'))
        shutil.copy2(root/'start_console.py',moved/'start_console.py')
        shutil.copy2(root/'EXPERIMENT_RULES.md',moved/'EXPERIMENT_RULES.md')
        proc=subprocess.run([sys.executable,'-S',str(moved/'start_console.py'),'--help'],cwd=self.temp.name,capture_output=True,timeout=10)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertIn(b'--workspace',proc.stdout)


class ReportTests(unittest.TestCase):
    def test_t_interval_and_na(self):
        d=describe([1.,2.,3.,4.,5.]);self.assertAlmostEqual(d['mean'],3)
        self.assertAlmostEqual(d['CI95'][1],4.963243,places=5)
        self.assertIsNone(describe([.9,None,.8])['mean']);self.assertIsNone(describe([1.])['CI95'])

    def test_explicit_import_keeps_failures_na_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'原始 metrics.json';fixture(source)
            original=source.read_bytes();v=import_report(root/'new-workspace',source)
            folder=root/'new-workspace/reports'/v['id'];m=json.loads((folder/'metrics.json').read_text(encoding='utf-8'))
            self.assertIsNone(m['per_STA']['STA3']['p_ACK_original']['mean'])
            self.assertEqual(m['per_STA']['STA3']['complete_failures'],2)
            self.assertEqual(m['per_STA']['STA3']['p_ACK_aux_full']['n_available'],5)
            self.assertEqual(source.read_bytes(),original)
            # Reanalysis works from the portable normalized snapshot after original moved away.
            source.unlink();v2=import_report(root/'other-workspace',folder/'input_snapshot.json')
            self.assertNotEqual(v['id'],v2['id'])

    def test_reject_duplicate_rows_invalid_and_nan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'metrics.json';fixture(source)
            v=json.loads(source.read_text(encoding='utf-8'));v['rows'][1]['order']=v['rows'][0]['order'];source.write_text(json.dumps(v))
            with self.assertRaises(ValueError):import_report(root/'output',source)
            source.write_text('{"bad":NaN}')
            with self.assertRaises(ValueError):import_report(root/'output',source)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.server=ConsoleServer(('127.0.0.1',0),Path(self.temp.name)/'console')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_address[1]
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)
        for p in self.server.children:p.wait(timeout=15)

    def request(self,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        h={'X-Console-Token':self.server.csrf}
        if headers:h.update(headers)
        c.request('GET' if body is None else 'POST',path,body=None if body is None else json.dumps(body),headers=h)
        r=c.getresponse();v=r.read();status=r.status;c.close();return status,v

    def test_local_only_csrf_and_live_lock(self):
        code,_=self.request('/');self.assertEqual(code,200)
        code,v=self.request('/api/check',{});self.assertEqual(code,200)
        self.assertFalse(json.loads(v)['live_ready'])
        code,_=self.request('/api/start',{'mode':'live'});self.assertEqual(code,400)
        code,_=self.request('/api/check',{}, {'X-Console-Token':'wrong'});self.assertEqual(code,403)
        code,_=self.request('/api/check',{}, {'Origin':'https://example.org'});self.assertEqual(code,403)
        code,_=self.request('/api/bootstrap',headers={'Host':'evil.example'});self.assertEqual(code,403)
        code,_=self.request('/artifact?kind=reports&id=../../x');self.assertEqual(code,400)

    def test_http_plan_start_state_report(self):
        request={'repeats':1}
        code,v=self.request('/api/plan',request);self.assertEqual(code,200);preview=json.loads(v)
        data={k:preview[k] for k in ['plan_sha256','platform_sha256','rules_sha256']}
        data.update(mode='offline',request=request,request_key='http-test-request',delay=.01)
        code,v=self.request('/api/start',data);self.assertEqual(code,200);job=json.loads(v)['job']['id']
        code,v=self.request('/api/start',data);self.assertEqual(code,200);self.assertFalse(json.loads(v)['created'])
        for child in self.server.children:self.assertEqual(child.wait(timeout=15),0)
        code,v=self.request('/api/state?job='+job);self.assertEqual(code,200)
        self.assertEqual(json.loads(v)['jobs'][0]['status'],'completed')
        code,v=self.request('/artifact?kind=runs&id='+job+'&file=report.html');self.assertEqual(code,200)
        self.assertIn('正式有效0'.encode(),v)
        code,v=self.request('/export?kind=runs&id='+job);self.assertEqual(code,200);self.assertTrue(v.startswith(b'PK'))
        code,v=self.request('/api/rebuild',{'kind':'runs','id':job});self.assertEqual(code,200)
        report=json.loads(v)['report']
        code,v=self.request('/api/rebuild',{'kind':'reports','id':report['id']});self.assertEqual(code,200)
        self.assertNotEqual(json.loads(v)['report']['id'],report['id'])

    def test_platform_change_invalidates_reviewed_plan(self):
        _,v=self.request('/api/plan',{});preview=json.loads(v)
        platform=default_platform();platform['name']='different platform'
        self.request('/api/platform',{'platform':platform})
        data={k:preview[k] for k in ['plan_sha256','platform_sha256','rules_sha256']}
        data.update(mode='offline',request={},request_key='invalidated-request')
        code,_=self.request('/api/start',data);self.assertEqual(code,400)

    def test_report_import_rebuild_and_svg_assets(self):
        source=Path(self.temp.name)/'source.json';fixture(source)
        code,v=self.request('/api/import',{'source':str(source)});self.assertEqual(code,200)
        r=json.loads(v)['report'];identity=r['id']
        code,v=self.request('/artifact?kind=reports&id='+identity+'&file=report.html')
        self.assertEqual(code,200);self.assertIn(b'&amp;file=S_Mbps.svg',v)
        code,v=self.request('/artifact?kind=reports&id='+identity+'&file=S_Mbps.svg')
        self.assertEqual(code,200);self.assertTrue(v.startswith(b'<svg'))
        code,v=self.request('/api/rebuild',{'kind':'reports','id':identity});self.assertEqual(code,200)
        self.assertNotEqual(json.loads(v)['report']['id'],identity)
        p=self.server.store.root/'reports'/identity/'input_snapshot.json';p.write_text('{}')
        code,_=self.request('/api/rebuild',{'kind':'reports','id':identity});self.assertEqual(code,400)


if __name__=='__main__':unittest.main()
