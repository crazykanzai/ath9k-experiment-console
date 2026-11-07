import copy
import hashlib
import http.client
import io
import json
import shutil
import socket
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from labconsole.replay import build_replay, import_replay, get_replay, validate_snapshot, source_catalog
from labconsole.server import ConsoleServer


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def fixture(root, shape='nodes', valid=True):
    folder = root / 'round01'
    t0 = 1793517495270800000
    active = ['STA1', 'STA2'] if shape == 'nodes' else ['STA1']
    plan = {'t0_ns': t0, 'warmup_s': 15, 'measure_s': 180, 'tail_s': 15,
            'access_mode': 'basic', 'expected_signature': {'cwmin': '31', 'cwmax': '255'}}
    if shape == 'cw':
        plan['single_sta'] = 'STA1'
    else:
        plan['active_stas'] = active
    manifest = {'valid': valid, 'status': 'valid' if valid else 'invalid', 'plan': plan,
                'measurement_start_ns': t0 + 15*10**9, 'measurement_stop_ns': t0 + 195*10**9,
                'traffic_stop_ns': t0 + 210*10**9}
    peers = {n: {'S_Mbps': 4., 'HoL_us': 1000., 'p_ACK_original': None, 'p_ACK_aux_full': .8,
                 'complete_packets': 100, 'ACK_success': 98, 'terminal_failures': 2,
                 'final_delivery': .98, 'queue_nonzero': 1., 'queue_backlog': 1.} for n in active}
    leaf = {'full_goodput_series_Mbps': {n: [2.]*15 + [4.]*180 + [1.]*15 for n in active},
            'per_STA': {n: {'complete_packets': 100, 'ACK_success': 98, 'terminal_failure': 2} for n in active}}
    row = {'order': 1, 'folder': str(folder), 'S_Mbps': len(active)*4., 'HoL_us': 1000.,
           'p_ACK_original': None, 'p_ACK_aux_full': .8, 'per_STA': peers}
    if shape == 'individual':
        row = {'order': 1, 'sender': 'STA1', 'folder': str(folder), 'S_Mbps': 4.,
               'HoL_success_us': 1000., 'p_ACK_original': None, 'p_ACK_aux_full': .8,
               'complete_packets': 100, 'complete_failures': 2, 'final_MAC_delivery': .98,
               'queue_nonempty': 1., 'queue_backlog': 1.}
    if shape == 'cw':
        row = {'order': 1, 'global_order': 16, 'folder': str(folder), 'S_Mbps': 4.,
               'HoL_success_conditional_us': 1000., 'p_ACK_original': None,
               'p_ACK_aux_all_complete': .8, 'terminal_failures': 2, 'complete_packets': 100,
               'ACK_success': 98, 'final_delivery': .98, 'queue_nonzero': 1., 'queue_backlog': 1.}
    write(folder / 'manifest.json', manifest)
    write(folder / 'metrics.json', leaf)
    source = root / 'metrics.json'
    write(source, {'valid_windows': int(valid), 'rows': [row]})
    return source


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_three_adapters_preserve_failure_NA_and_real_bins(self):
        for shape in ['nodes', 'individual', 'cw']:
            source = fixture(self.root / shape, shape)
            files = [source, source.parent/'round01/manifest.json', source.parent/'round01/metrics.json']
            before = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
            with patch.object(socket.socket, 'connect', side_effect=AssertionError('no network')):
                data, hashes = build_replay(source)
            r = data['records'][0]
            self.assertEqual(r['order'], 16 if shape == 'cw' else 1)
            self.assertIsNone(r['aggregate']['p_original'])
            self.assertEqual(r['aggregate']['p_auxiliary'], .8)
            for n in r['active_stas']:
                self.assertEqual(r['per_STA'][n]['failures'], 2)
                self.assertEqual(r['throughput_1s_Mbps'][n], [2.]*15+[4.]*180+[1.]*15)
            self.assertEqual(r['sample_kind']['HoL'], 'whole_window_only')
            self.assertEqual(hashes, before)
            self.assertEqual(before, {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})

    def test_explicit_input_only_and_invalid_preserved(self):
        source = fixture(self.root / 'source', valid=False)
        write(source.parent/'stray/metrics.json', {'rows': 'never read'})
        data, hashes = build_replay(source)
        self.assertEqual(len(data['records']), 1)
        self.assertFalse(data['records'][0]['valid'])
        self.assertEqual(len(hashes), 3)

    def test_missing_bins_or_boundary_mismatch_not_fabricated(self):
        source = fixture(self.root/'source')
        leaf = source.parent/'round01/metrics.json'
        original = json.loads(leaf.read_text())
        for invalid in [None, {}, {'STA1': [1.]*209, 'STA2': [1.]*210}]:
            v=copy.deepcopy(original);v['full_goodput_series_Mbps']=invalid;write(leaf,v)
            with self.assertRaises(ValueError):build_replay(source)
        write(leaf,original)
        p=source.parent/'round01/manifest.json';v=json.loads(p.read_text());v['measurement_start_ns']+=1;write(p,v)
        with self.assertRaises(ValueError):build_replay(source)

    def test_duplicates_and_nonfinite_values_rejected(self):
        source=fixture(self.root/'source');batch=json.loads(source.read_text());batch['rows']*=2;batch['valid_windows']=2;write(source,batch)
        with self.assertRaises(ValueError):build_replay(source)
        source.write_text('{"rows": NaN}')
        with self.assertRaises(ValueError):build_replay(source)

    def test_snapshot_portable_and_tamper_refused(self):
        source=fixture(self.root/'source')
        meta=import_replay(self.root/'workspace',source)
        got=get_replay(self.root/'workspace',meta['id'])
        self.assertEqual(got['manifest']['valid_windows'],1)
        shutil.rmtree(source.parent)  # Only the disposable test fixture.
        snapshot=self.root/'workspace/replays'/meta['id']/'replay.json'
        new=import_replay(self.root/'other-machine',snapshot)
        self.assertEqual(get_replay(self.root/'other-machine',new['id'])['data'],got['data'])
        snapshot.write_text('{}')
        with self.assertRaises(ValueError):get_replay(self.root/'workspace',meta['id'])
        with self.assertRaises(ValueError):get_replay(self.root/'workspace','../x')

    def test_original_NA_never_filled_by_auxiliary_or_mean(self):
        data,_=build_replay(fixture(self.root/'source'))
        for n in data['records'][0]['active_stas']:
            s=data['records'][0]['per_STA'][n]
            self.assertIsNone(s['p_original']);self.assertEqual(s['p_auxiliary'],.8)
        bad=copy.deepcopy(data);bad['records'][0]['per_STA']['STA1']['p_original']=1.1
        with self.assertRaises(ValueError):validate_snapshot(bad)

    def test_catalog_falls_back_to_portable_samples_without_scanning(self):
        sample=self.root/'labconsole/examples/replay_nodes20.json';write(sample,{})
        records=source_catalog(self.root)
        self.assertEqual(records[0]['source'],str(sample))
        self.assertTrue(records[0]['available']);self.assertFalse(records[1]['available'])


class ReplayWebTests(unittest.TestCase):
    def test_local_import_resume_export_and_live_still_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=fixture(root/'source')
            server=ConsoleServer(('127.0.0.1',0),root/'workspace')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def call(path,data=None,token=True):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                headers={'X-Console-Token':server.csrf if token else 'bad'}
                conn.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers)
                res=conn.getresponse();body=res.read();status=res.status;conn.close();return status,body
            try:
                status,body=call('/replay.js');self.assertEqual(status,200);self.assertNotIn(b'/api/start',body)
                self.assertEqual(call('/api/replay/list')[0],200)
                self.assertEqual(call('/api/replay/import',{'source':str(source)},token=False)[0],403)
                status,body=call('/api/replay/import',{'source':str(source)});self.assertEqual(status,200)
                identity=json.loads(body)['replay']['id']
                status,body=call('/api/replay?id='+identity);self.assertEqual(status,200)
                self.assertIsNone(json.loads(body)['data']['records'][0]['aggregate']['p_original'])
                status,body=call('/export?kind=replays&id='+identity);self.assertEqual(status,200)
                with zipfile.ZipFile(io.BytesIO(body)) as z:self.assertIn(identity+'/replay.json',z.namelist())
                self.assertEqual(call('/api/replay?id=../../x')[0],400)
                self.assertEqual(call('/api/start',{'mode':'live'})[0],400)
                self.assertEqual(server.store.jobs(),[])
            finally:
                server.shutdown();server.server_close();thread.join(3)


if __name__ == '__main__':
    unittest.main()
