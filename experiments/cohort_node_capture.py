"""Linux root-only owned ftrace reader. Never changes driver/MAC policy."""
import json
import os
from pathlib import Path
import subprocess
import time
import traceback


class Capture:
    def __init__(self,spec,out):
        self.spec=spec;self.out=Path(out);self.root=Path('/sys/kernel/tracing')
        self.instance=self.root/'instances/apsta_cohort';self.created=False;self.names=[]
        self.reader=None;self.log=None;self.err=None

    def start(self):
        live=Path('/sys/module/ath9k/srcversion').read_text().strip()
        if live!=self.spec['expected_srcversion']:raise RuntimeError('module changed since DWARF verification')
        if self.instance.exists() or 'apsta_audit/' in (self.root/'kprobe_events').read_text():
            raise RuntimeError('cohort trace instance or namespace occupied')
        try:
            self.instance.mkdir();self.created=True
            for name,event in self.spec['events'].items():
                if not event.startswith('p:apsta_audit/'+name+' '):raise RuntimeError('unsafe probe specification')
                self.append_event(event)
                self.names.append(name)
            (self.instance/'trace_clock').write_text('mono\n')
            (self.instance/'buffer_size_kb').write_text('16384\n')
            for name in self.names:(self.instance/('events/apsta_audit/'+name+'/enable')).write_text('1\n')
            self.log=(self.out/'tx_trace.txt').open('w');self.err=(self.out/'trace_reader.stderr').open('w')
            self.reader=subprocess.Popen(['cat',str(self.instance/'trace_pipe')],stdout=self.log,stderr=self.err)
            (self.instance/'tracing_on').write_text('1\n')
            return self
        except Exception:
            (self.out/'cohort_setup_error.txt').write_text(traceback.format_exc())
            try:self.close()
            except Exception:
                (self.out/'cohort_setup_cleanup_error.txt').write_text(traceback.format_exc())
            raise

    def append_event(self,event):
        # Control files may reject the SEEK_END attempted by Python append
        # streams. POSIX O_APPEND + write performs no seek/truncate.
        fd=os.open(str(self.root/'kprobe_events'),os.O_WRONLY|os.O_APPEND)
        try:os.write(fd,(event+'\n').encode())
        finally:os.close(fd)

    def close(self):
        errors=[]
        if not self.created:return
        (self.instance/'tracing_on').write_text('0\n');time.sleep(.3)
        for name in self.names:
            try:(self.instance/('events/apsta_audit/'+name+'/enable')).write_text('0\n')
            except Exception as e:errors.append(repr(e))
        if self.reader:
            self.reader.terminate();self.reader.wait(timeout=5)
            self.log.close();self.err.close()
        (self.out/'trace_stats.txt').write_text('\n'.join(p.read_text() for p in sorted((self.instance/'per_cpu').glob('cpu*/stats'))))
        (self.out/'kprobe_profile.txt').write_text((self.root/'kprobe_profile').read_text())
        for name in reversed(self.names):
            try:
                self.append_event('-:apsta_audit/'+name)
            except Exception as e:errors.append(repr(e))
        try:self.instance.rmdir();self.created=False
        except Exception as e:errors.append(repr(e))
        (self.out/'cohort_capture_cleanup.json').write_text(json.dumps({'errors':errors,'reader_exit':self.reader.returncode if self.reader else None}))
        if errors:raise RuntimeError('cohort capture cleanup failed: '+repr(errors))
