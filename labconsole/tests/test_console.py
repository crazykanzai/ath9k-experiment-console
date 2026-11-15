"""Demo-only HTTP surface, authorization and workspace isolation tests."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import errno
from unittest.mock import patch, Mock
from labconsole.server import ConsoleServer,existing_console
from labconsole.server import bind_console


class StartupTests(unittest.TestCase):
    def test_bind_failure_preserves_original_error_and_closes_socket(self):
        with tempfile.TemporaryDirectory() as folder:
            server=object.__new__(ConsoleServer)
            with patch.object(ConsoleServer,'server_bind',side_effect=PermissionError(errno.EACCES,'reserved port')):
                with self.assertRaisesRegex(PermissionError,'reserved port'):
                    ConsoleServer.__init__(server,('127.0.0.1',0),folder)
            self.assertEqual(server.socket.fileno(),-1)
            self.assertIsNone(server._demo)
            self.assertFalse((Path(folder)/'.controller.lock').exists())

    def test_reserved_default_falls_back_once_but_not_for_ownership_or_explicit_port(self):
        denied=PermissionError(errno.EACCES,'reserved port')
        sentinel=Mock()
        with patch('labconsole.server.ConsoleServer',side_effect=[denied,sentinel]) as factory:
            self.assertIs(bind_console('fixture',8790),sentinel)
            self.assertEqual([c.args[0] for c in factory.call_args_list],[('127.0.0.1',8790),('127.0.0.1',0)])
        for error,port,fallback in [(denied,8790,False),(denied,0,True),
                                    (ValueError('workspace occupied'),8790,True),
                                    (OSError(errno.EADDRINUSE,'occupied'),8790,True)]:
            with patch('labconsole.server.ConsoleServer',side_effect=error) as factory:
                with self.assertRaises(type(error)):bind_console('fixture',port,allow_fallback=fallback)
                factory.assert_called_once()

    def test_store_failure_releases_bound_socket(self):
        with tempfile.TemporaryDirectory() as folder:
            server=object.__new__(ConsoleServer)
            with patch('labconsole.server.Store',side_effect=ValueError('invalid workspace')):
                with self.assertRaisesRegex(ValueError,'invalid workspace'):
                    ConsoleServer.__init__(server,('127.0.0.1',0),folder)
            self.assertEqual(server.socket.fileno(),-1)

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.server=ConsoleServer(('127.0.0.1',0),self.tmp.name)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3);self.tmp.cleanup()
    def request(self,path,body=None,token=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        c.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),{'X-Console-Token':token or self.server.csrf})
        r=c.getresponse();result=r.status,r.read();c.close();return result
    def test_removed_pages_and_routes_are_unavailable(self):
        _,raw=self.request('/api/bootstrap');boot=json.loads(raw)
        self.assertEqual(boot['mode'],'demo_only');self.assertNotIn('jobs',boot)
        for path in ('/api/state','/api/replay/list','/api/live/list','/api/adaptive/list','/replay.js','/adaptive.js'):
            self.assertEqual(self.request(path)[0],404,path)
        for path in ('/api/start','/api/import','/api/adaptive/start','/api/live/attach','/api/replay/import','/api/plan'):
            self.assertEqual(self.request(path,{})[0],400,path)
        _,raw=self.request('/')
        self.assertNotIn(b'id="adaptive"',raw);self.assertIn(b'id="real-demo"',raw)
    def test_authentication_and_export(self):
        self.assertEqual(self.request('/api/platform',{},token='wrong')[0],403)
        _,raw=self.request('/api/bootstrap');platform=json.loads(raw)['platform']
        self.assertEqual(self.request('/api/validate-platform',{'platform':platform})[0],200)
        self.assertEqual(self.request('/api/demo?id=../../bad')[0],400)
    def test_duplicate_workspace_and_existing_server(self):
        self.assertEqual(existing_console(Path(self.tmp.name),self.server.server_port),'http://127.0.0.1:'+str(self.server.server_port))
        with self.assertRaisesRegex(ValueError,'已有总控'):
            ConsoleServer(('127.0.0.1',0),self.tmp.name)
if __name__=='__main__':unittest.main()
