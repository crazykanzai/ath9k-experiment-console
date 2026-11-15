"""Single owner for a local demo workspace, on Windows and Linux."""
import os
from pathlib import Path

class WorkspaceLock:
    def __init__(self, root):
        path=Path(root)/'.controller.lock'
        if path.is_symlink():raise ValueError('工作区锁不能为符号链接')
        self.file=path.open('a+b')
        try:
            if os.name=='nt':
                import msvcrt
                if path.stat().st_size==0:self.file.write(b'0');self.file.flush()
                self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise ValueError('同一工作区已有总控进程，请使用现有服务或另选工作区') from exc
    def close(self):self.file.close()
