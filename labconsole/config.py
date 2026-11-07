"""Validated, credential-free platform configuration; no SSH dependencies."""
import copy
import ipaddress
import json
from pathlib import Path

ROLES = ['AP', 'STA1', 'STA2', 'STA3', 'STA4', 'MON15']
FORBIDDEN = {'password', 'passwd', 'secret', 'token', 'private_key', 'sudo_password'}


def reject_secrets(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k.lower() in FORBIDDEN:
                raise ValueError('平台档案不保存密码或密钥正文；请使用 credential_ref')
            reject_secrets(v)
    elif isinstance(value, list):
        for v in value:
            reject_secrets(v)


def default_platform():
    return {
        'schema_version': 1, 'name': '四STA ath9k实验平台',
        'controller': {'management_ip': '192.168.100.2', 'ntp_source': '192.168.100.2'},
        'nodes': [
            {'role': role, 'address': '192.168.100.' + str(10 + i), 'port': 22,
             'username': 'abc' if role == 'MON15' else 'w',
             'credential_ref': 'lab-ssh', 'interface_hint': '',
             'remote_work_dir': '~/apsta-console'}
            for i, role in enumerate(ROLES)
        ],
        'driver': {'expected_srcversion': '0AB85B473A0B1B032BFECCD',
                   'expected_module_sha256': '1f02b2ee31ddcb5eff3b6cc515405229c3071147a4127b15516e1891d12d7019',
                   'hol_abi': 1, 'basic_retry_abi': 2, 'fifo_trigger': 20,
                   'resource_bundle': ''},
        'notes': '历史平台档案，仅作配置示例；接口、业务IP和实际状态均须现场重新读取。',
    }


def validate_platform(raw):
    if not isinstance(raw, dict):
        raise ValueError('平台档案须为JSON对象')
    reject_secrets(raw)
    p = copy.deepcopy(raw)
    if p.get('schema_version') != 1:
        raise ValueError('不支持的平台档案版本')
    if not isinstance(p.get('name'), str) or not 1 <= len(p['name']) <= 100:
        raise ValueError('平台名称须为1～100个字符')
    nodes = p.get('nodes', [])
    if not isinstance(nodes, list) or len(nodes) != 6 or any(not isinstance(n, dict) for n in nodes) or sorted(str(n.get('role', '')) for n in nodes) != sorted(ROLES):
        raise ValueError('第一版平台档案须恰好包含AP、STA1～4、MON15各一次')
    addresses = []
    for n in nodes:
        ip = ipaddress.ip_address(n['address'])
        if ip.version != 4 or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
            raise ValueError('节点地址须为有效的非回环IPv4地址')
        addresses.append(str(ip))
        if type(n.get('port')) is not int or not 1 <= n['port'] <= 65535:
            raise ValueError('SSH端口无效')
        for field in ['username', 'credential_ref', 'remote_work_dir']:
            if not isinstance(n.get(field), str) or not n[field].strip() or len(n[field]) > 200 or any(c in n[field] for c in '\r\n\x00'):
                raise ValueError('节点字段无效：' + field)
    if len(set(addresses)) != 6:
        raise ValueError('节点IP不能重复')
    if not isinstance(p.get('controller'), dict) or not isinstance(p.get('driver'), dict):
        raise ValueError('controller和driver须为JSON对象')
    for key in ['management_ip', 'ntp_source']:
        value = ipaddress.ip_address(p['controller'][key])
        if value.version != 4 or value.is_unspecified or value.is_loopback or value.is_multicast:
            raise ValueError('控制机和时间源须为有效IPv4')
    if p['controller']['management_ip'] in addresses:
        raise ValueError('控制机IP不能与节点重复')
    d = p['driver']
    if d.get('hol_abi') != 1 or d.get('basic_retry_abi') != 2 or d.get('fifo_trigger') != 20:
        raise ValueError('当前已登记配置要求HoL ABI1、Basic ABI2和FIFO20')
    digest = d.get('expected_module_sha256', '')
    if len(digest) != 64 or any(x not in '0123456789abcdef' for x in digest):
        raise ValueError('模块SHA256格式无效')
    return p


def load_json(path):
    path = Path(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('JSON超过32MiB限制')
    def bad(value):
        raise ValueError('JSON不接受NaN或Infinity：' + value)
    return json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=bad)
