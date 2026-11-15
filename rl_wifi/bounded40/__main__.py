"""Offline-only preparation CLI. No device/SSH/traffic command exists here."""
import argparse
import hashlib
import json
from pathlib import Path

from .plan import seal


def main():
    parser = argparse.ArgumentParser(description='DQN40 preparation ONLY; no device access or live traffic')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'smoke'):
        p = commands.add_parser(name)
        p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        plan = seal(args.output)
        package = Path(__file__).parent
        sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in package.glob('*.py')}
        (args.output/'source_hashes.json').write_text(json.dumps(sources, indent=2), encoding='utf-8')
        print(json.dumps({'status': plan['status'], 'planned_runs': len(plan['runs']),
                          'live_entrypoint_enabled': False, 'output': str(args.output)}, ensure_ascii=False))
    else:
        from .offline import smoke
        print(json.dumps(smoke(args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
