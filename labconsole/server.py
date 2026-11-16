"""Loopback-only console for environment preparation and real CW demos."""
import json
import errno
import mimetypes
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs,urlparse
from . import VERSION
from .config import validate_platform,load_json
from .storage import Store,atomic_json
from .workspace_lock import WorkspaceLock
from .preparation import DEFAULT_TIME_TRANSPORT, PreparationHub
PROJECT=Path(__file__).resolve().parents[1]
WEB=Path(__file__).resolve().parent/'web'

def default_workspace():
    configured = os.environ.get('APSTA_CONSOLE_WORKSPACE')
    if configured:
        return Path(configured).expanduser()
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'APSTASELECT' / 'LabConsole'
    return Path.home() / '.local' / 'share' / 'apstaselect-console'

def local_check(store):
    return {'checks':[{'name':'Python','status':'pass','detail':sys.version.split()[0]},
        {'name':'运行模式','status':'pass','detail':'现场演示专用；节点操作仅在点击后执行'}]}


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,workspace):
        # TCPServer calls our server_close() even if bind fails in super().__init__.
        self._demo=None;self._demo_lock=threading.Lock()
        super().__init__(address,Handler)
        try:
            self.store=Store(workspace);self.csrf=secrets.token_urlsafe(32)
            self.owner=WorkspaceLock(self.store.root)
            self.preparation=PreparationHub(self.store)
        except Exception:
            self.server_close();raise
    @property
    def demo(self):
        with self._demo_lock:
            if self._demo is None:
                from .real_demo import DemoHub
                self._demo=DemoHub(self.store.root,self.preparation)
            return self._demo
    def server_close(self):
        try:
            if self._demo is not None:self._demo.close()
            if hasattr(self,'preparation'):self.preparation.close()
        finally:
            if hasattr(self,'owner'):self.owner.close()
            super().server_close()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
            pass

    def reply(self, data, code=200, content_type='application/json; charset=utf-8', download=None):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if not isinstance(data, bytes) else data
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'")
            if download:
                self.send_header('Content-Disposition', 'attachment; filename="' + download + '"')
            self.end_headers()
            self.wfile.write(body)

    def allowed_host(self):
            port = self.server.server_address[1]
            return self.headers.get('Host') in [f'127.0.0.1:{port}', f'localhost:{port}']

    def do_GET(self):
        if not self.allowed_host():return self.reply({'error':'仅允许本机Host'},403)
        try:
            u=urlparse(self.path);path=u.path;q=parse_qs(u.query)
            if path in ('/','/app.js','/style.css','/prepare.js','/real-demo.js'):
                name='index.html' if path=='/' else path[1:]
                return self.reply((WEB/name).read_bytes(),content_type=mimetypes.guess_type(name)[0]+'; charset=utf-8')
            if path=='/api/bootstrap':
                timing=self.server.preparation.timing
                return self.reply({'version':VERSION,'mode':'demo_only','capabilities':['preparation','real_demo','continuous_demo','continuous_sequential','demo_access_modes','unified_demo','throughput_ucb_v2','per_sta_cw','demo_lifecycle_v3','ucb_hold_v3','ucb_hold_w32','unified_10s','per_sta_cw_pairs','auto_cw_profile','ucb_same_w_drop'],
                    'csrf':self.server.csrf,'workspace':str(self.server.store.root),'platform':self.server.store.platform(),
                    'preparation_policy':dict(revision=3,settle_s=timing[0],hold_s=timing[1],sample_s=timing[2],monitor_s=timing[3])})
            if path=='/api/preparation/list':return self.reply({'sessions':self.server.preparation.listing()})
            if path=='/api/demo/list':return self.reply(self.server.demo.listing())
            if path in ('/api/preparation','/api/preparation/export','/api/demo','/api/demo/export'):
                hub=self.server.preparation if path.startswith('/api/preparation') else self.server.demo
                value=hub.get(q.get('id',[''])[0])
                return self.reply(value,download='session-'+value['id']+'.json' if path.endswith('/export') else None)
            return self.reply({'error':'页面不存在或功能已移除'},404)
        except (ValueError,OSError,KeyError,TypeError) as exc:self.reply({'error':str(exc)},400)

    def do_POST(self):
            origin = self.headers.get('Origin')
            port = self.server.server_address[1]
            if not self.allowed_host() or (origin and origin not in [f'http://127.0.0.1:{port}', f'http://localhost:{port}']) or self.headers.get('X-Console-Token') != self.server.csrf:
                # Drain a bounded rejected body before closing HTTP/1.0. Closing with
                # unread request bytes can reset the TCP connection on Windows, hiding
                # the intended 403 response from the client. Authentication stays first.
                try:
                    rejected_length = int(self.headers.get('Content-Length', '0'))
                    if 0 < rejected_length <= 1024**2:
                        self.connection.settimeout(2)
                        self.rfile.read(rejected_length)
                except (ValueError, OSError):
                    pass
                return self.reply({'error': '本机请求校验失败'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024**2:
                    raise ValueError('请求大小无效')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('请求须为JSON对象')
                self.reply(self.action(urlparse(self.path).path, data))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self.reply({'error': str(exc)}, 400)

    def action(self,path,data):
        store=self.server.store
        if path == '/api/demo/start':
            return self.server.demo.start(data)
        if path == '/api/demo/command':
            return self.server.demo.command(data)
        if path == '/api/preparation/check':
            if set(data)-{'password','sudo_password','request_key','ap_cidr','time_transport'}:raise ValueError('未知准备字段')
            return self.server.preparation.create(store.platform(),data.get('password',''),data.get('sudo_password',''),data['request_key'],data.get('ap_cidr',''),data.get('time_transport',DEFAULT_TIME_TRANSPORT))
        if path == '/api/preparation/action':
            if data.get('action')=='restore' and data.get('allow_temporary_restore') is not True:
                raise ValueError('须明确允许本次临时无线/FIFO恢复')
            return self.server.preparation.action(data['id'],data['action'],data.get('ap_cidr'))
        if path == '/api/preparation/recover':
            return self.server.preparation.recover(data['id'],data.get('password',''),data.get('sudo_password',''))
        if path == '/api/platform':
            return {'platform': store.save_platform(data['platform'])}
        if path == '/api/validate-platform':
            return {'platform': validate_platform(data['platform'])}
        if path == '/api/check':
            return local_check(store)
        raise ValueError("该功能已从演示版移除或操作不存在")


def existing_console(workspace, requested_port):
    """Locate a responding console for this workspace before taking its lock."""
    import urllib.request
    workspace = Path(workspace).resolve()
    ports = [requested_port, 8790]
    endpoint = workspace/'console_endpoint.json'
    if endpoint.is_file() and not endpoint.is_symlink():
        try:
            record = load_json(endpoint)
            if Path(record['workspace']).resolve()==workspace:
                ports.insert(0, record['port'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in dict.fromkeys(p for p in ports if type(p) is int and 1<=p<=65535):
        url = 'http://127.0.0.1:' + str(port)
        try:
            with opener.open(url+'/api/bootstrap', timeout=.7) as response:
                raw = response.read(2*1024**2+1)
            if len(raw)>2*1024**2:
                continue
            value = json.loads(raw)
            if (isinstance(value, dict) and isinstance(value.get('version'), str)
                    and isinstance(value.get('csrf'), str) and ('capabilities' in value or 'templates' in value)
                    and Path(value.get('workspace', '')).resolve()==workspace):
                return url
        except (OSError, ValueError, TypeError):
            continue
    return None

def bind_console(workspace, port, allow_fallback=True):
    """A reserved default Windows port may be denied even without a listener."""
    try:
        return ConsoleServer(('127.0.0.1',port),workspace)
    except OSError as exc:
        denied = getattr(exc,'winerror',None)==10013 or exc.errno==errno.EACCES
        if not allow_fallback or port==0 or not denied:
            raise
        print(f'默认本机端口 {port} 被系统拒绝；改用系统分配的可用端口。',flush=True)
        return ConsoleServer(('127.0.0.1',0),workspace)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='ath9k实验总控：环境准备与现场自适应演示')
    parser.add_argument('--workspace', type=Path, default=default_workspace())
    parser.add_argument('--port', type=int, default=None, help='指定本机端口；默认8790受限时自动选可用端口')
    parser.add_argument('--open', action='store_true', help='打开本机浏览器')
    args = parser.parse_args(argv)
    explicit_port = args.port is not None
    if args.port is None:args.port=8790
    if not 0 <= args.port <= 65535:
        parser.error('端口无效')
    existing = existing_console(args.workspace, args.port)
    if existing:
        reuse_console(existing, args.open)
        return
    try:
        server = bind_console(args.workspace,args.port,allow_fallback=not explicit_port)
    except (ValueError, OSError) as exc:
        existing = existing_console(args.workspace, args.port)
        if existing:
            reuse_console(existing, args.open)
            return
        raise SystemExit('总控未能启动：' + str(exc) + '\n请检查已有总控窗口；同一数据工作区只需启动一次。\n若指定端口受系统限制，可用 --port 0 让系统分配可用端口。') from None
    url = 'http://127.0.0.1:' + str(server.server_address[1])
    endpoint = server.store.root/'console_endpoint.json'
    owner = secrets.token_hex(12)
    atomic_json(endpoint, {'owner':owner, 'port':server.server_address[1],
                           'workspace':str(server.store.root)})
    print('ath9k实验总控 ' + VERSION + ' · 本机网页', flush=True)
    print('URL: ' + url, flush=True)
    print('WORKSPACE: ' + str(server.store.root), flush=True)
    print('默认不连接节点。环境准备仅在点击后通过SSH执行；不启动业务。', flush=True)
    print('结束前请在网页点击“结束使用并清理”，等待清理完成，再关闭本窗口。', flush=True)
    if args.open:
        threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            if not endpoint.is_symlink() and load_json(endpoint).get('owner')==owner:
                endpoint.unlink()
        except (OSError, ValueError):
            pass

def reuse_console(url, open_browser):
    print('总控后台已在运行，打开现有网页：' + url, flush=True)
    print('本次启动沿用现有后台。更新程序时，请先结束当前会话并退出原后台，再运行启动器。', flush=True)
    print('如果原后台没有终端窗口，可在任务管理器的“详细信息”中按监听端口对应的 PID 查找进程。', flush=True)
    if open_browser:
        webbrowser.open(url)


if __name__=="__main__":main()
