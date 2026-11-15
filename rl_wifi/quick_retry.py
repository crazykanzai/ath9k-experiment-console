"""One initial continuation + at most one automatic retry after clean restore.

Keeps credentials only in this supervisor process; never reboots devices.
Each attempt owns an immutable plan, new worker IDs, raw data and restoration.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time

from labconsole.storage import atomic_json
from .quick_screen import execute,seal,password_gui,SOURCES,ROOT


MAX_ATTEMPTS=2
PREPARATION_TIMING=(90,0,0,10)


def can_retry(manifest,preparation):
    return (manifest['status']!='completed_eight' and
            not manifest.get('cleanup_error') and
            manifest.get('postflight',{}).get('confirmed') is True and
            not manifest.get('remaining_reservations') and
            preparation.get('cleanup',{}).get('confirmed') is True)


def verify_plan(folder):
    file=folder/'sealed/plan.json';p=json.loads(file.read_text(encoding='utf-8'))
    digest=lambda file:hashlib.sha256(file.read_bytes()).hexdigest()
    if digest(file)!=(folder/'sealed/plan.sha256').read_text().split()[0]:raise RuntimeError('sealed plan hash mismatch')
    if any(digest(ROOT/name)!=v for name,v in p['sources'].items()):raise RuntimeError('source changed after seal')
    for row in p.get('carried_windows',[]):
        source=Path(row['carried_from'])
        if digest(source/'manifest.json')!=row['carried_manifest_sha256'] or digest(source/row['id']/'session.json')!=row['carried_session_sha256']:
            raise RuntimeError('carried evidence changed')
    return p


def supervise(root,workspace,password):
    begin=time.monotonic();state={'status':'running','started_at':datetime.now().astimezone().isoformat(),
        'policy':'one automatic retry after restoration; no device reboot','max_attempts':2,'attempts':[],
        'device_reboots_authorized':False,'preparation_timing_s':list(PREPARATION_TIMING)}
    atomic_json(root/'supervisor.json',state)
    folder=root/'attempt_01'
    for number in range(1,MAX_ATTEMPTS+1):
        plan=verify_plan(folder)
        print('ATTEMPT',number,'remaining_windows',plan['new_windows'],flush=True)
        execute(folder,plan,workspace,password)
        m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        prep_file=folder/'controller/preparation_final.json'
        preparation=json.loads(prep_file.read_text(encoding='utf-8')) if prep_file.is_file() else {}
        state['attempts'].append({'number':number,'batch':str(folder),'status':m['status'],'error':m['error'],
                                  'restored':can_retry({**m,'status':'stopped_at_gate'},preparation)})
        atomic_json(root/'supervisor.json',state)
        if m['status']=='completed_eight':
            state['status']='completed_eight';break
        if number==MAX_ATTEMPTS or not can_retry(m,preparation) or time.monotonic()-begin>=90*60:
            state['status']='stopped_after_retry' if number==MAX_ATTEMPTS else 'stopped_restore_or_budget_gate';break
        # Preserve and close attempt_01. Build a fresh explicit retry of the
        # first incomplete slot and remaining suffix; completed slots stay put.
        source=folder;folder=root/'attempt_02';folder.mkdir(exist_ok=False);(folder/'controller').mkdir()
        seal(folder,source,allow_retry=True,preparation_timing=PREPARATION_TIMING)
        print('AUTOMATIC_RETRY_ONCE after confirmed restoration',flush=True)
    state['finished_at']=datetime.now().astimezone().isoformat();state['elapsed_s']=time.monotonic()-begin
    state['final_batch']=str(folder);atomic_json(root/'supervisor.json',state)
    print('SUPERVISOR_FINAL',json.dumps(state,ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser(description='Authorized fixed-screen continuation with one bounded automatic retry')
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--resume-from',type=Path)
    parser.add_argument('--seal-only',action='store_true');parser.add_argument('--execute-live',action='store_true')
    parser.add_argument('--workspace',default=str(Path(os.environ.get('LOCALAPPDATA','.'))/'APSTASELECT/LabConsole'))
    args=parser.parse_args()
    if args.seal_only==args.execute_live:parser.error('choose seal-only or execute-live')
    if args.seal_only:
        if not args.resume_from:parser.error('explicit resume source required')
        folder=args.root/'attempt_01';folder.mkdir(parents=True,exist_ok=False);(folder/'controller').mkdir()
        seal(folder,args.resume_from,allow_retry=True,preparation_timing=PREPARATION_TIMING)
        return
    if (args.root/'supervisor.json').exists():raise RuntimeError('supervisor already attempted; no extra retries')
    verify_plan(args.root/'attempt_01')
    password=password_gui()
    supervise(args.root,args.workspace,password)


if __name__=='__main__':main()
