"""Linux preparation helper, sent in memory over an explicitly authorized SSH.

Only existing registered assets are used. No business, tracing, installation,
reboot, persistent configuration write or clear-on-read MIB access.
"""
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ORIGINAL = '270d53c6d955b7451983b6b97f0627b98b596a44e582164fe4313ae8c1a9dee1'
CANDIDATE = '1f02b2ee31ddcb5eff3b6cc515405229c3071147a4127b15516e1891d12d7019'
VERSION = '0AB85B473A0B1B032BFECCD'
STAGE = '/home/w/ath9k-kernel/fifo-trigger-10-20261018-01'
MODULE = STAGE+'/ath9k-fifo-trigger.ko'
APP = '/home/w/APSTASELECT/sta/app/ath9k-edca'
AP_CONFIG = '/home/w/APSTASELECT/sta/app/config/hostapd-ath9k-collision-ch1.conf'


def run(args, timeout=12, optional=False):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode and not optional:
        raise RuntimeError(' '.join(args[:3])+': '+(p.stderr or p.stdout).strip()[:300])
    return p.stdout


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fields(text):return dict(x.split('=',1) for x in text.split() if '=' in x)


def discover(hint=''):
    text=run(['iw','dev']);names=re.findall(r'(?m)^\s*Interface (\S+)',text)
    if hint:
        if hint not in names:raise RuntimeError('interface hint missing')
        iface=hint
    elif len(names)==1:iface=names[0]
    else:raise RuntimeError('wireless interface ambiguous')
    info=run(['iw','dev',iface,'info'])
    phy='phy'+re.search(r'(?m)^\s*wiphy (\d+)',info)[1]
    return iface,phy,info


def registers(base, expected_helper_sha):
    helper=Path('/home/w/apsta-sync/reg_access.py')
    if sha(helper)!=expected_helper_sha:raise RuntimeError('registered common-lock helper hash mismatch')
    spec=importlib.util.spec_from_file_location('console_preparation_reg_access',helper)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    result=mod.read_registers(str(base),[0x30,0x1030,0x1070])
    if not result['selector_restored']:raise RuntimeError('register selector restoration failed')
    return result


def inspect(a):
    iface,phy,info=discover(a.get('interface_hint',''))
    base=Path('/sys/kernel/debug/ieee80211')/phy/'ath9k'
    ip=json.loads(run(['ip','-j','-4','addr','show','dev',iface]))
    ips=[x['local']+'/'+str(x['prefixlen']) for row in ip for x in row['addr_info'] if x['family']=='inet']
    route=run(['ip','route','get',a['controller_ip']])
    if re.search(r'\bdev '+re.escape(iface)+r'\b',route):raise RuntimeError('management depends on wireless')
    profiles=run(['nmcli','-t','-f','NAME,UUID','connection','show'])
    uuids=[r.split(':',1)[1] for r in profiles.splitlines() if r.startswith('ath9k_test:')]
    role=a['role'];src=Path('/sys/module/ath9k/srcversion').read_text().strip()
    result={'interface':iface,'phy':phy,'info':info,'ips':ips,
      'link':run(['iw','dev',iface,'link']),'power_save':run(['iw','dev',iface,'get','power_save']),
      'route':route,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
      'demo_monitors':re.findall(r'(?m)^\s*Interface (acm[0-9a-f]{8})\s*$',run(['iw','dev'])),
      'kernel':run(['uname','-r']).strip(),'srcversion':src,'profile_uuids':uuids,
      'busy':run(['pgrep','-a','-x','iperf3'],optional=True)+run(['pgrep','-a','-x','tcpdump'],optional=True),
      'trace_busy':'\n'.join(s for p in (Path('/sys/kernel/tracing/kprobe_events'),Path('/sys/kernel/debug/tracing/kprobe_events'))
                             if p.exists() for s in p.read_text().splitlines() if 'apsta_' in s),
      'services':run(['systemctl','is-active','NetworkManager','wpa_supplicant','systemd-timesyncd'],optional=True),
      'time_config':run(['systemd-analyze','cat-config','systemd/timesyncd.conf']),
      'timesync':run(['timedatectl','show'])+run(['timedatectl','timesync-status'],optional=True)}
    if role=='AP':
        result.update(ap_config=Path(AP_CONFIG).read_text(),app_exists=Path(APP).is_file(),
                      hostapd=run(['pgrep','-a','-x','hostapd'],optional=True))
    if role.startswith('STA'):
        edca=(base/'edca_ctrl').read_text();be=next(fields(s) for s in edca.splitlines() if s.startswith('AC=BE '))
        canonical=Path('/lib/modules')/result['kernel']/'updates/apstaselect/ath9k.ko'
        result.update(BE=be,edca=edca,stats_header=(base/'dcf_stats').read_text().splitlines()[0],
          parameter=(Path('/sys/module/ath9k/parameters/diag_tx_fifo_trigger').read_text().strip()
                     if Path('/sys/module/ath9k/parameters/diag_tx_fifo_trigger').exists() else None),
          mask=(base.parent/('netdev:'+iface)/'rc_rateidx_mask_2ghz').read_text().strip(),
          override=(base/'basic_access_override').read_text().strip(),
          iwconfig=run(['iwconfig',iface]),
          candidate_sha=sha(MODULE),original_sha=sha(canonical),rollback_sha=sha(STAGE+'/rollback-ath9k.ko'),
          candidate_info=run(['modinfo','-F','vermagic',MODULE]).strip(),
          reg=registers(base,a['helper_sha']))
    return result


def quiet():
    if run(['pgrep','-a','-x','iperf3'],optional=True) or run(['pgrep','-a','-x','tcpdump'],optional=True):
        raise RuntimeError('existing business/capture; will not stop it')


def wait_network_device(iface, timeout=30):
    """Wait for NetworkManager to observe a recreated interface, once."""
    deadline = time.monotonic() + timeout
    state = ''
    while True:
        state = run(['nmcli','-g','GENERAL.STATE','device','show',iface],optional=True).strip()
        number = re.match(r'(\d+)', state)
        if number and 30 <= int(number[1]) <= 100:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('wireless device readiness timeout: '+iface+'; wait_budget_s='+str(timeout)+'; NetworkManager state='+state)
        time.sleep(.1)


def activate_profile(iface, profile, ap_mac):
    wait_network_device(iface)
    link = run(['iw','dev',iface,'link'])
    if 'Connected to '+ap_mac not in link:
        run(['nmcli','connection','up','uuid',profile,'ifname',iface],timeout=60)


def radio(a):
    quiet();before=inspect(a);iface=before['interface'];role=a['role']
    if role=='AP':
        config=before['ap_config']
        required=['interface='+iface,'ssid=ath9k_test','channel=1','ieee80211n=0','wmm_enabled=1','basic_rates=240']
        if not all(x in config.splitlines() for x in required):raise RuntimeError('AP registered configuration differs')
        if 'type AP' not in before['info']:
            if before['hostapd']:raise RuntimeError('unexpected hostapd, refuse replacement')
            cidr=str(ipaddress.IPv4Interface(a['ap_cidr']))
            run([APP,iface,'mode','ap','--config',AP_CONFIG,'--ip',cidr],timeout=90)
        elif 'ssid ath9k_test' not in before['info']:raise RuntimeError('AP already serves another SSID')
    else:
        if len(before['profile_uuids'])!=1:raise RuntimeError('ath9k_test profile missing/ambiguous')
        if 'Connected to '+a['ap_mac'] not in before['link']:
            activate_profile(iface,before['profile_uuids'][0],a['ap_mac'])
        if role.startswith('STA'):
            if 'running=0' not in before['stats_header']:raise RuntimeError('driver statistics owned by another run')
            canonical=Path('/lib/modules')/before['kernel']/'updates/apstaselect/ath9k.ko'
            if before['candidate_sha']!=CANDIDATE or before['original_sha']!=ORIGINAL or before['rollback_sha']!=ORIGINAL:
                raise RuntimeError('registered candidate/original/rollback hash mismatch')
            if not before['candidate_info'].startswith(before['kernel']+' '):raise RuntimeError('kernel mismatch; no build or installation allowed')
            if before['srcversion'] not in (VERSION,'C6E744C117E5F6ABADF932F'):raise RuntimeError('unexpected running module')
            if before['srcversion']!=VERSION or before['parameter']!='20':
                changed=False
                try:
                    run(['rmmod','ath9k']);changed=True
                    run(['insmod',MODULE,'diag_tx_fifo_trigger=20'])
                    activate_profile(iface,before['profile_uuids'][0],a['ap_mac'])
                except Exception:
                    if changed:
                        run(['rmmod','ath9k'],optional=True);run(['modprobe','ath9k'])
                        activate_profile(iface,before['profile_uuids'][0],a['ap_mac'])
                    raise
            # Preserve power and retry organization. Fixed PHY mask and BE baseline
            # are this preparation profile, not a throughput optimization.
            run(['iw','dev',iface,'set','bitrates','legacy-2.4','24'])
            run(['iw','dev',iface,'set','power_save','off'])
            _,phy,_=discover(iface);base=Path('/sys/kernel/debug/ieee80211')/phy/'ath9k'
            be=next(fields(s) for s in (base/'edca_ctrl').read_text().splitlines() if s.startswith('AC=BE '))
            if (be['hw_aifs'],be['hw_cwmin'],be['hw_cwmax'])!=('3','15','1023'):
                (base/'edca_ctrl').write_text('BE 3 15 1023\n')
            # Existing base AIFS may be2 after startup. Use explicit override if
            # reset cannot recover BE3; do not falsely report the expected values.
            be=next(fields(s) for s in (base/'edca_ctrl').read_text().splitlines() if s.startswith('AC=BE '))
            if be['hw_aifs']!='3':(base/'edca_ctrl').write_text('BE 3 15 1023\n')
    after=inspect(a)
    if after['boot_id']!=before['boot_id']:raise RuntimeError('unexpected reboot')
    return {'before':before,'after':after,'power_change_requested':False,'persistent_modules_changed':False}


def time_path(identity):
    if not re.fullmatch('[0-9a-f]{32}',identity):raise ValueError('invalid preparation identity')
    return Path('/run/systemd/timesyncd.conf.d')/('90-ath9k-console-'+identity+'.conf')


def time_text(ip):
    addr=ipaddress.IPv4Address(ip)
    if not addr.is_private:raise ValueError('time reference must be private wired IPv4')
    return '[Time]\nNTP=\nNTP='+str(addr)+'\nFallbackNTP=\nPollIntervalMinSec=16\nPollIntervalMaxSec=32\n'


def dispatch(a):
    if a['op']=='inspect':return inspect(a)
    if a['op']=='radio':return radio(a)
    if a['op']=='clock_apply':
        quiet();p=time_path(a['id']);text=time_text(a.get('time_source',a['controller_ip']))
        if p.exists():raise RuntimeError('runtime source already exists; recover previous session first')
        other=[str(x) for x in p.parent.glob('*') if x.is_file() and ('ath9k-console' in x.name or 'apstaselect' in x.name)]
        if other:raise RuntimeError('another experiment time override exists')
        original=run(['systemd-analyze','cat-config','systemd/timesyncd.conf'])
        p.parent.mkdir(parents=True,exist_ok=True);p.open('x').write(text)
        try:run(['systemctl','restart','systemd-timesyncd'])
        except Exception:
            p.unlink();run(['systemctl','restart','systemd-timesyncd']);raise
        return {'original_config':original,'owned_path':str(p),'content_sha256':hashlib.sha256(text.encode()).hexdigest()}
    if a['op']=='clock_cleanup':
        p=time_path(a['id']);text=time_text(a.get('time_source',a['controller_ip']))
        if p.is_symlink():raise RuntimeError('runtime source symlink refused')
        if p.exists():
            if p.read_text()!=text:raise RuntimeError('runtime source ownership/content mismatch')
            p.unlink();run(['systemctl','restart','systemd-timesyncd'])
        return {'removed':not p.exists(),'config':run(['systemd-analyze','cat-config','systemd/timesyncd.conf']),
                'service':run(['systemctl','is-active','systemd-timesyncd'])}
    if a['op']=='clock_status':
        return {'status':run(['timedatectl','show'])+run(['timedatectl','timesync-status'],optional=True),
                'wall_ns':__import__('time').time_ns(),'monotonic_ns':__import__('time').monotonic_ns()}
    raise ValueError('unknown preparation operation')


if __name__=='__main__':
    try:
        print('ATH9K_PREP_READY',flush=True)
        a=json.loads(sys.stdin.readline());value=dispatch(a)
        print(json.dumps({'ok':True,'value':value},ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));raise SystemExit(1)
