"""Portable offline console entry; separate from the Linux radio launcher."""
import sys
from labconsole.server import main

if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    main()
