#!/usr/bin/env python3
"""Install an exact, validated ath9k/HoL bundle through normal kmod boot paths.

No module reload or reboot is performed. Stock package modules stay untouched.
All replaced files and the old initramfs are backed up before any mutation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

NAMES=('mac80211','ath9k_hw','ath9k_common','ath9k')
TOOL=Path('/usr/local/libexec/apstaselect-driver-persist.py')
UNIT=Path('/etc/systemd/system/apstaselect-driver-verify.service')
AUTO=Path('/etc/modules-load.d/apstaselect-ath9k.conf')
UNIT_TEXT='''[Unit]
Description=Verify APSTASELECT HoL and retry driver bundle
After=systemd-modules-load.service sys-kernel-debug.mount
Wants=sys-kernel-debug.mount

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/libexec/apstaselect-driver-persist.py verify --live
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
'''

def command(args,timeout=60):
    p=subprocess.run([str(a) for a in args],capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('command failed: '+repr(args)+'\n'+p.stdout+p.stderr)
    return p.stdout.strip()

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def kernel_dir(kernel):
    if not re.fullmatch(r'[A-Za-z0-9_.+-]+',kernel):raise ValueError('invalid kernel release')
    return Path('/lib/modules')/kernel/'updates/apstaselect'

def validate_manifest(manifest,kernel):
    if manifest.get('kernel')!=kernel:raise ValueError('manifest kernel does not match running kernel')
    if set(manifest.get('modules',{}))!=set(NAMES):raise ValueError('exactly four matched modules required')
    if manifest.get('basic_retry_abi')!=2 or manifest.get('hol_abi')!=1:raise ValueError('unapproved driver ABI')
    if manifest.get('rts_retry_abi',1)!=1:raise ValueError('unapproved RTS ABI: candidate 2 failed air audit')
    for item in manifest['modules'].values():
        if not re.fullmatch('[0-9a-f]{64}',item.get('sha256','')):raise ValueError('bad module checksum')
        if not re.fullmatch('[0-9A-F]{1,24}',item.get('srcversion','')):raise ValueError('bad srcversion')

def validate_bundle(source,manifest,kernel,inspect=command):
    validate_manifest(manifest,kernel)
    for name in NAMES:
        path=source/(name+'.ko');item=manifest['modules'][name]
        if not path.is_file() or path.is_symlink():raise ValueError('missing/unsafe module '+str(path))
        if digest(path)!=item['sha256']:raise ValueError('module checksum mismatch: '+name)
        if inspect(['modinfo','-F','vermagic',path]).split()[0]!=kernel:raise ValueError('vermagic mismatch: '+name)
        if inspect(['modinfo','-F','srcversion',path])!=item['srcversion']:raise ValueError('srcversion mismatch: '+name)
    if b'basic_retry_abi=2' not in (source/'ath9k.ko').read_bytes():raise ValueError('Basic ABI marker missing from binary')
    if manifest.get('rts_retry_abi')==2 and b'rts_retry_abi=2' not in (source/'ath9k.ko').read_bytes():raise ValueError('RTS ABI marker missing from binary')

def atomic_copy(source,target,mode=0o644):
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.apstaselect-',dir=str(target.parent));os.close(fd)
    try:
        shutil.copyfile(str(source),temp);os.chmod(temp,mode);os.replace(temp,str(target))
    finally:
        if os.path.exists(temp):os.unlink(temp)

def atomic_text(target,text,mode=0o644):
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.apstaselect-',dir=str(target.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(text)
        os.chmod(temp,mode);os.replace(temp,str(target))
    finally:
        if os.path.exists(temp):os.unlink(temp)

def save_record(backup,record):
    atomic_text(backup/'record.json',json.dumps(record,ensure_ascii=False,indent=2)+'\n',0o600)

def backup_file(path,backup,record):
    path=Path(path)
    if path.is_symlink():raise ValueError('refuse to replace symlink '+str(path))
    entry={'path':str(path),'existed':path.exists()}
    if path.exists():
        saved=backup/'files'/str(len(record['files']))
        saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(str(path),str(saved))
        st=path.stat();entry.update(saved=str(saved),sha256=digest(path),uid=st.st_uid,gid=st.st_gid,mode=st.st_mode&0o777)
    record['files'].append(entry);save_record(backup,record)

def verify(live=False):
    kernel=platform.release();target=kernel_dir(kernel)
    manifest=json.loads((target/'bundle.json').read_text(encoding='utf-8'))
    validate_bundle(target,manifest,kernel)
    selected={name:command(['modinfo','-n',name]) for name in NAMES}
    for name,path in selected.items():
        if Path(path).resolve()!=(target/(name+'.ko')).resolve():raise RuntimeError('kmod selects a different module: '+name+' '+path)
    result={'kernel':kernel,'selected_modules':selected,'disk_valid':True}
    if live:
        for name in NAMES:
            actual=(Path('/sys/module')/name/'srcversion').read_text().strip()
            if actual!=manifest['modules'][name]['srcversion']:raise RuntimeError('live module mismatch: '+name)
        found=[]
        for _ in range(40):
            found=list(Path('/sys/kernel/debug/ieee80211').glob('*/ath9k/dcf_stats'))
            if found:break
            time.sleep(.25)
        if not found:raise RuntimeError('ath9k debugfs/HoL ABI unavailable')
        for path in found:
            header=path.read_text().split('\n',1)[0].split()
            if not {'hol_abi=1','basic_retry_abi=2'}.issubset(header):raise RuntimeError('live ABI check failed: '+str(path))
            if manifest.get('rts_retry_abi')==2 and 'rts_retry_abi=2' not in header:raise RuntimeError('live RTS ABI check failed')
        result['live_valid']=True
    return result

def restore(backup):
    record=json.loads((backup/'record.json').read_text(encoding='utf-8'))
    if record['kernel']!=platform.release():raise RuntimeError('rollback kernel differs from running kernel')
    # Only the owned verification unit is disabled. No other service is stopped.
    if not record.get('unit_enabled_before',False):subprocess.run(['systemctl','disable','apstaselect-driver-verify.service'],capture_output=True)
    for entry in reversed(record['files']):
        target=Path(entry['path'])
        if entry['existed']:
            if digest(entry['saved'])!=entry['sha256']:raise RuntimeError('backup checksum mismatch')
            atomic_copy(entry['saved'],target,entry['mode']);os.chown(str(target),entry['uid'],entry['gid'])
        elif target.exists():
            # Exact files recorded before this installation; never recursive deletion.
            if target.is_symlink():raise RuntimeError('rollback target became a symlink')
            target.unlink()
    command(['depmod','-a',record['kernel']]);command(['systemctl','daemon-reload'])
    target=kernel_dir(record['kernel'])
    if str(target)!=record['module_dir']:raise RuntimeError('rollback module directory mismatch')
    if target.is_dir() and not any(target.iterdir()):target.rmdir()
    record['status']='rolled_back';save_record(backup,record)
    return {'status':'rolled_back','backup':str(backup),'running_modules_unchanged':True}

def install(source,manifest_path,app_dirs,replace_manifest_sha256=None):
    if os.geteuid()!=0:raise PermissionError('install requires sudo/root')
    kernel=platform.release();manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    validate_bundle(source,manifest,kernel)
    target=kernel_dir(kernel)
    # Do not replace a separately installed custom driver bundle silently.
    previous=None
    if target.exists():
        if not replace_manifest_sha256 or digest(target/'bundle.json')!=replace_manifest_sha256:
            raise RuntimeError('persistent bundle already exists; exact --replace-manifest-sha256 required')
        previous=json.loads((target/'bundle.json').read_text(encoding='utf-8'))
        validate_bundle(target,previous,kernel)
        # A retry-only update may not silently replace the matching dependency stack.
        for name in NAMES[:-1]:
            if manifest['modules'][name]!=previous['modules'][name]:raise RuntimeError('retry upgrade changes dependency '+name)
    elif replace_manifest_sha256:
        raise RuntimeError('requested upgrade but no persistent bundle exists')
    for name in NAMES:
        if command(['cat',Path('/sys/module')/name/'srcversion'])!=manifest['modules'][name]['srcversion']:
            raise RuntimeError('source does not match currently validated live stack: '+name)
    app_dirs=[p.resolve() for p in app_dirs]
    for app in app_dirs:
        if not (app/'src/driver_ctrl.c').is_file():raise ValueError('not an APSTASELECT app directory: '+str(app))
        for name in NAMES:
            p=app/'modules'/kernel/(name+'.ko')
            if not p.is_file() or p.is_symlink():raise ValueError('app module slot missing/unsafe')
            # Never replace unexpected user custom modules, except known old ath9k.
            approved={manifest['modules'][name]['srcversion']}
            if name=='ath9k':approved.add('44E5C855E1DF9FD89EEAA7F')
            if previous:approved.add(previous['modules'][name]['srcversion'])
            if command(['modinfo','-F','srcversion',p]) not in approved:raise ValueError('unexpected app module '+str(p))
            if previous and digest(p)!=previous['modules'][name]['sha256']:raise ValueError('app module differs from approved previous bundle')
    backup=Path('/var/lib/apstaselect/driver-backups')/(kernel+'-'+str(time.time_ns()))
    backup.mkdir(parents=True,mode=0o700)
    enabled=subprocess.run(['systemctl','is-enabled','apstaselect-driver-verify.service'],capture_output=True,text=True)
    record={'kernel':kernel,'status':'preparing','files':[],'unit_enabled_before':enabled.returncode==0,'module_dir':str(target),'live_reloaded':False}
    paths=[target/(n+'.ko') for n in NAMES]+[target/'bundle.json',target/'bundle.sha256',AUTO,TOOL,UNIT,Path('/boot')/('initrd.img-'+kernel)]
    for app in app_dirs:paths.extend([app/'modules'/kernel/'ath9k.ko'])
    for path in paths:backup_file(path,backup,record)
    try:
        for name in NAMES:atomic_copy(source/(name+'.ko'),target/(name+'.ko'))
        atomic_copy(manifest_path,target/'bundle.json')
        atomic_text(target/'bundle.sha256',''.join(manifest['modules'][n]['sha256']+'  '+str(target/(n+'.ko'))+'\n' for n in NAMES))
        for app in app_dirs:
            slot=app/'modules'/kernel/'ath9k.ko';stat=slot.stat()
            atomic_copy(source/'ath9k.ko',slot);os.chown(str(slot),stat.st_uid,stat.st_gid)
        atomic_copy(Path(__file__).resolve(),TOOL,0o755)
        atomic_text(AUTO,'# APSTASELECT matched HoL/Basic driver bundle; revalidate after kernel upgrades.\nath9k\n')
        atomic_text(UNIT,UNIT_TEXT)
        record['status']='files_installed';save_record(backup,record)
        command(['depmod','-a',kernel]);record['disk_verify']=verify()
        record['modprobe_dependencies']=command(['modprobe','--show-depends','ath9k'])
        # A complete matched dependency chain is required, not ath9k alone.
        for name in NAMES:
            if str(target/(name+'.ko')) not in record['modprobe_dependencies']:raise RuntimeError('incomplete selected dependency chain: '+name)
        output=command(['update-initramfs','-u','-k',kernel],timeout=600)
        atomic_text(backup/'update-initramfs.log',output+'\n',0o600)
        entries=command(['lsinitramfs',Path('/boot')/('initrd.img-'+kernel)],timeout=60).splitlines()
        selected=[p for p in entries if Path(p).name in {n+'.ko' for n in NAMES}]
        if any('/updates/apstaselect/' not in p for p in selected):raise RuntimeError('initramfs still contains stock members of the custom stack')
        record['initramfs_module_entries']=selected
        command(['systemctl','daemon-reload']);command(['systemctl','enable','apstaselect-driver-verify.service'])
        command(['systemctl','restart','apstaselect-driver-verify.service'])
        record['live_verify']=verify(live=True);record['status']='installed_verified';save_record(backup,record)
        return {'status':record['status'],'backup':str(backup),'module_dir':str(target),'initramfs_entries':selected,'live_reloaded':False}
    except Exception:
        record['status']='failed_rolling_back';save_record(backup,record)
        restore(backup)
        raise

def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('install');p.add_argument('--bundle',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--app-dir',action='append',type=Path,default=[])
    p.add_argument('--replace-manifest-sha256',help='explicit guarded upgrade of exactly this existing bundle')
    p=sub.add_parser('verify');p.add_argument('--live',action='store_true')
    p=sub.add_parser('rollback');p.add_argument('--backup',type=Path,required=True)
    args=parser.parse_args()
    if args.action=='install':result=install(args.bundle,args.manifest,args.app_dir,args.replace_manifest_sha256)
    elif args.action=='verify':result=verify(args.live)
    else:
        if os.geteuid()!=0:raise PermissionError('rollback requires sudo/root')
        result=restore(args.backup)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
