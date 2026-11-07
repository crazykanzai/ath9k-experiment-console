"""Offline publication boundary check; no node/network actions."""
import ast
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    problems=[];files=[]
    for p in ROOT.rglob('*'):
        if not p.is_file() or any(x in p.relative_to(ROOT).parts for x in ['.git','__pycache__','.venv','workspace','tmp','build']):
            continue
        rel=str(p.relative_to(ROOT)).replace('\\','/');files.append(rel)
        if p.suffix in ['.ko','.o','.pcap','.pcapng','.zip','.sqlite3','.pem','.key']:
            problems.append('runtime/binary/private artifact: '+rel)
        if p.stat().st_size>5*1024**2:problems.append('unexpected large file: '+rel)
        if p.suffix not in ['.py','.md','.json','.yaml','.yml','.js','.html','.css','.txt','.c','.h','.sh','.bat']:continue
        try:text=p.read_text(encoding='utf-8-sig')
        except UnicodeError:problems.append('unexpected encoding: '+rel);continue
        if re.search(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})',text):problems.append('token-shaped value: '+rel)
        if re.search(r'[A-Za-z]:[\\/](?:Users[\\/][^\\/\s]+|26年大创)',text):problems.append('personal absolute path: '+rel)
        if p.suffix=='.py':
            try:ast.parse(text,filename=rel)
            except SyntaxError as e:problems.append('syntax: '+rel+' '+str(e))
    for forbidden in ['launcher.py','ap/app.py','sta/tools/edca_hw_server.py','sta/tools/mode-select.html','sta/tools/edca-sta-explorer.html']:
        if (ROOT/forbidden).exists():problems.append('excluded old UI: '+forbidden)
    if 'Fixed-four reference engine is disabled' not in (ROOT/'experiments/sync_run.py').read_text():problems.append('reference controller guard missing')
    for p in (ROOT/'labconsole/examples').glob('replay_*.json'):
        data=json.loads(p.read_text(encoding='utf-8'))
        if data.get('schema')!='apstaselect.history-replay.v1':problems.append('bad replay: '+p.name)
    print(json.dumps({'files_checked':len(files),'problems':problems,'node_connections':0},ensure_ascii=False,indent=2))
    raise SystemExit(bool(problems))


if __name__=='__main__':main()
