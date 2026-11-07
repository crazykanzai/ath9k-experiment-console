"""Build a credential-free standalone offline console archive, without Git/.venv."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from . import VERSION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    paths = [root / name for name in ['start_console.py', 'start_console.bat', 'EXPERIMENT_RULES.md']]
    paths += [p for p in (root / 'labconsole').rglob('*') if p.is_file() and '__pycache__' not in p.parts
              and p.suffix in ['.py', '.md', '.html', '.css', '.js', '.json'] and 'acceptance' not in p.parts]
    content = {str(p.relative_to(root)).replace('\\', '/'): p.read_bytes() for p in sorted(paths)}
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in content.items()}
    release = {'version': VERSION, 'mode': 'offline_only', 'requires': 'Python>=3.10 standard library',
               'live_enabled': False, 'optional_readonly_SSH_observer': 'Paramiko; explicit CLI; field acceptance pending', 'files': hashes}
    content['RELEASE.json'] = (json.dumps(release, ensure_ascii=False, indent=2) + '\n').encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, raw in content.items():
            archive.writestr('APSTASELECT-console/' + name, raw)
    # Verify the archive by reading and decompressing every member; never remove source files.
    with zipfile.ZipFile(args.output) as archive:
        if set(archive.namelist()) != {'APSTASELECT-console/' + name for name in content}:
            raise RuntimeError('归档成员清单不符')
        for name, raw in content.items():
            if archive.read('APSTASELECT-console/' + name) != raw:
                raise RuntimeError('归档内容不符：' + name)
    print(json.dumps({'archive': str(args.output.resolve()), 'files': len(content),
                      'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(), 'verified_all_members': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
