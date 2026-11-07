"""Loopback-only standard-library web service; live execution is fail-closed."""
import hashlib
import io
import json
import mimetypes
import os
import secrets
import shutil
import sys
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from . import VERSION
from .config import validate_platform, load_json
from .plans import TEMPLATES, build_plan, digest
from .reports import import_report, simulation_report
from .storage import Store, atomic_json
from .worker import launch
from .replay import source_catalog, preset_source, import_replay, get_replay, list_replays
from .live import LiveHub

PROJECT = Path(__file__).resolve().parents[1]
WEB = Path(__file__).resolve().parent / 'web'


def default_workspace():
    configured = os.environ.get('APSTA_CONSOLE_WORKSPACE')
    if configured:
        return Path(configured).expanduser()
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'APSTASELECT' / 'LabConsole'
    return Path.home() / '.local' / 'share' / 'apstaselect-console'


def local_check(store):
    platform = validate_platform(store.platform())
    usage = shutil.disk_usage(store.root)
    rules = PROJECT / 'EXPERIMENT_RULES.md'
    checks = [
        {'name': '运行模式', 'status': 'pass', 'detail': '离线；网络执行适配器未启用'},
        {'name': '平台配置格式', 'status': 'pass', 'detail': '六角色、唯一地址、凭据引用、已登记ABI/FIFO'},
        {'name': 'Python', 'status': 'pass' if sys.version_info >= (3, 10) else 'block', 'detail': sys.version.split()[0]},
        {'name': '数据空间', 'status': 'pass' if usage.free >= 100 * 1024**2 else 'block', 'detail': f'{usage.free/1024**3:.2f} GiB；这里只检离线空间，不能代替采集容量预算'},
        {'name': '实验规则', 'status': 'pass' if rules.is_file() else 'block', 'detail': str(rules)},
    ]
    for name in ['SSH与有线管理', '实际接口/IP/关联', '模块/ABI/FIFO与共同寄存器锁',
                 '持续时间同步与启停边界', 'Basic/24Mbps空口及MON15监听', '追踪/进程/队列饱和']:
        checks.append({'name': name, 'status': 'not_checked', 'detail': '未连接节点；未检查，不能据此启动真实实验'})
    return {'checks': checks, 'offline_ready': not any(x['status'] == 'block' for x in checks),
            'live_ready': False, 'platform_sha256': digest(platform),
            'rules_sha256': hashlib.sha256(rules.read_bytes()).hexdigest() if rules.is_file() else None}


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, workspace):
        super().__init__(address, Handler)
        self.store = Store(workspace)
        self.csrf = secrets.token_urlsafe(32)
        self.children = []
        self.live = LiveHub(self.store.root)

    def server_close(self):
        if hasattr(self, 'live'):
            self.live.close()
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
        if not self.allowed_host():
            return self.reply({'error': '仅允许本机Host'}, 403)
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            store = self.server.store
            if path in ['/', '/app.js', '/style.css', '/replay.js', '/live.js']:
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/replay.js': 'replay.js', '/live.js': 'live.js'}[path]
                return self.reply((WEB / name).read_bytes(), content_type=mimetypes.guess_type(name)[0] + '; charset=utf-8')
            if path == '/api/bootstrap':
                return self.reply({'version': VERSION, 'mode': 'offline', 'live_enabled': False,
                    'csrf': self.server.csrf, 'workspace': str(store.root), 'platform': store.platform(),
                    'templates': TEMPLATES, 'jobs': store.jobs(), 'reports': self.reports()})
            if path == '/api/state':
                job = query.get('job', [None])[0]
                return self.reply({'jobs': store.jobs(), 'reports': self.reports(),
                                   'events': store.events(job) if job else []})
            if path == '/api/replay/list':
                return self.reply({'sources': source_catalog(), 'replays': list_replays(store.root)})
            if path == '/api/replay':
                return self.reply(get_replay(store.root, query.get('id', [''])[0]))
            if path == '/api/live/list':
                return self.reply({'sessions': self.server.live.listing()})
            if path in ['/api/live', '/api/live/export']:
                data = self.server.live.get(query.get('id', [''])[0])
                if path.endswith('/export'):
                    return self.reply(json.dumps(data, ensure_ascii=False, allow_nan=False).encode(), download='observation.json')
                return self.reply(data)
            if path in ['/artifact', '/export']:
                folder = self.artifact_folder(query)
                if path == '/export':
                    data = io.BytesIO()
                    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
                        for p in sorted(folder.iterdir()):
                            if p.is_file() and not p.is_symlink() and p.suffix in ['.json', '.md', '.html', '.svg', '.csv', '.log']:
                                archive.write(p, folder.name + '/' + p.name)
                    return self.reply(data.getvalue(), content_type='application/zip', download=folder.name + '.zip')
                name = query.get('file', ['report.html'])[0]
                if Path(name).name != name or '\\' in name or name not in [p.name for p in folder.iterdir() if p.is_file() and not p.is_symlink()]:
                    raise ValueError('文件不属于该报告')
                file = folder / name
                if file.suffix not in ['.html', '.md', '.json', '.csv', '.svg', '.log']:
                    raise ValueError('不支持的文件类型')
                # HTML image URLs must include the selected report's namespace.
                data = file.read_bytes()
                if file.suffix == '.html':
                    text = data.decode('utf-8')
                    for p in folder.glob('*.svg'):
                        prefix = '/artifact?kind=' + query['kind'][0] + '&id=' + folder.name + '&file=' + p.name
                        text = text.replace('src="' + p.name + '"', 'src="' + prefix.replace('&', '&amp;') + '"')
                    data = text.encode()
                return self.reply(data, content_type=(mimetypes.guess_type(name)[0] or 'text/plain') + '; charset=utf-8')
            return self.reply({'error': '页面不存在'}, 404)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.reply({'error': str(exc)}, 400)

    def artifact_folder(self, query):
        kind = query.get('kind', [''])[0]
        identity = query.get('id', [''])[0]
        if kind not in ['runs', 'reports', 'replays'] or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('无效报告标识')
        folder = self.server.store.root / kind / identity
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError('报告不存在')
        return folder

    def reports(self):
        root = self.server.store.root / 'reports'
        if not root.exists():
            return []
        result = []
        # Only this application's own report index, never search experiment windows recursively.
        for p in sorted(root.glob('*/manifest.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            if not p.is_symlink() and not p.parent.is_symlink():
                result.append(load_json(p))
        return result

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

    def action(self, path, data):
        store = self.server.store
        if path == '/api/platform':
            return {'platform': store.save_platform(data['platform'])}
        if path == '/api/validate-platform':
            return {'platform': validate_platform(data['platform'])}
        if path == '/api/check':
            return local_check(store)
        if path == '/api/plan':
            plan = build_plan(data)
            check = local_check(store)
            return {'plan': plan, 'plan_sha256': digest(plan), 'platform_sha256': check['platform_sha256'],
                    'rules_sha256': check['rules_sha256'], 'check': check}
        if path == '/api/start':
            if data.get('mode') != 'offline':
                raise ValueError('当前版本真实执行锁定；只能启动离线演练')
            plan = build_plan(data['request'])
            check = local_check(store)
            if not check['offline_ready']:
                raise ValueError('本机离线检查未通过')
            for key, value in [('plan_sha256', digest(plan)), ('platform_sha256', check['platform_sha256']), ('rules_sha256', check['rules_sha256'])]:
                if data.get(key) != value:
                    raise ValueError('方案、平台或规则已变化，请重新生成并审阅计划')
            job, created = store.create(plan, data['request_key'], data.get('delay', .2), data.get('fault_at'), expected_platform_hash=data['platform_sha256'])
            if created:
                rules = PROJECT / 'EXPERIMENT_RULES.md'
                (store.root / 'runs' / job / 'rules_snapshot.md').write_bytes(rules.read_bytes())
                atomic_json(store.root / 'runs' / job / 'provenance.json',
                            {'version': VERSION, 'rules_sha256': check['rules_sha256'],
                             'source_sha256': {str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((PROJECT / 'labconsole').glob('*.py'))},
                             'mode': 'offline', 'true_T0': None, 'node_connections': 0})
                try:
                    child = launch(store, job)
                    self.server.children[:] = [x for x in self.server.children if x.poll() is None]
                    self.server.children.append(child)
                except OSError as exc:
                    store.finish(job, 'worker_failed', '执行进程未启动：' + str(exc))
                    raise ValueError('离线执行进程启动失败；记录已保留') from exc
            return {'job': store.job(job), 'created': created}
        if path == '/api/stop':
            store.job(data['job'])
            store.stop(data['job'], data['mode'])
            return {'accepted': True}
        if path == '/api/import':
            return {'report': import_report(store.root, data['source'])}
        if path == '/api/replay/import':
            source = preset_source(data['preset']) if data.get('preset') else data['source']
            return {'replay': import_replay(store.root, source)}
        if path == '/api/live/attach':
            return {'session': self.server.live.create(data['source'], data['title'], data['kind'])}
        if path == '/api/live/detach':
            return self.server.live.detach(data['id'])
        if path == '/api/rebuild':
            kind, identity = data['kind'], data['id']
            folder = self.artifact_folder({'kind': [kind], 'id': [identity]})
            if kind == 'reports':
                meta = load_json(folder / 'manifest.json')
                if meta.get('kind') == 'simulation_report_revision':
                    identity = meta['source_job']
                    store.job(identity)
                    folder = store.root / 'runs' / identity
                else:
                    hashes = load_json(folder / 'sha256.json')
                    if hashlib.sha256((folder / 'input_snapshot.json').read_bytes()).hexdigest() != hashes.get('input_snapshot.json'):
                        raise ValueError('分析输入快照哈希不符，拒绝复算')
                    return {'report': import_report(store.root, folder / 'input_snapshot.json')}
            job = store.job(identity)
            if job['status'] not in ['completed', 'stopped', 'gate_failed', 'worker_failed']:
                raise ValueError('演练仍在运行')
            receipt = load_json(folder / 'manifest.json')
            records = load_json(folder / 'simulation_records.json')
            # Independent analysis revision, never redo execution.
            revision = store.root / 'reports' / secrets.token_hex(16)
            revision.mkdir(parents=True)
            simulation_report(revision, receipt, records)
            meta = {'id': revision.name, 'kind': 'simulation_report_revision', 'source_job': identity}
            atomic_json(revision / 'manifest.json', meta)
            return {'report': meta}
        raise ValueError('未知操作')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='APSTASELECT实验总控：离线第一版')
    parser.add_argument('--workspace', type=Path, default=default_workspace())
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--open', action='store_true', help='打开本机浏览器')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('端口无效')
    server = ConsoleServer(('127.0.0.1', args.port), args.workspace)
    url = 'http://127.0.0.1:' + str(server.server_address[1])
    print('APSTASELECT实验总控 ' + VERSION + ' · 仅离线', flush=True)
    print('URL: ' + url, flush=True)
    print('WORKSPACE: ' + str(server.store.root), flush=True)
    print('不会连接SSH、启动业务或改变节点。关闭网页不停止演练；停止服务器后可用同一工作区重新打开。', flush=True)
    if args.open:
        threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
