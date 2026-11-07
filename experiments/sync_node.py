"""Autonomous absolute-time AP/STA worker for one four-STA experiment.

The controller only sends a future plan to an already armed Unix socket.
No SSH arrival or readiness poll determines the traffic/window boundaries.
Run as root on the Linux nodes. Raw files are never reopened by analysis.
"""

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from reg_access import read_registers


def stamp(ns=None):
    ns = time.time_ns() if ns is None else int(ns)
    dt = datetime.fromtimestamp(ns // 1_000_000_000, timezone.utc).astimezone()
    return {"epoch_ns": ns, "monotonic_ns":time.monotonic_ns(), "iso": dt.strftime("%Y-%m-%dT%H:%M:%S") +
            ".%09d" % (ns % 1_000_000_000) + dt.strftime("%z")}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def cmd(args, timeout=8):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"command": args, "exit_code": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except Exception as e:
        return {"command": args, "exit_code": -1, "stdout": "", "stderr": repr(e)}


def retry_reset_state(debugfs, edca):
    """Read BE DCU_Misc bit 6; restore selector, never write register values."""
    try:
        row=next(line for line in edca.splitlines() if line.startswith('AC=BE '))
        q=int(re.search(r'qnum=(\d+)',row).group(1))
        base=Path(debugfs).parent;reading=read_registers(base,[0x1100+4*q])
        value=int(reading['values'][hex(0x1100+4*q)],16)
        return {'exit_code':0,'qnum':q,'register':hex(0x1100+4*q),
                'value':hex(value),'reset_enabled':bool(value&0x40),
                'selector_restored':reading['selector_restored'],'reg_access_abi':1}
    except Exception as exc:
        return {'exit_code':-1,'error':repr(exc)}


def fifo_state(debugfs):
    """Configuration read only; AR_TXCFG is not a clear-on-read counter."""
    reading=read_registers(Path(debugfs).parent,[0x30]);value=int(reading['values']['0x30'],16)
    return {'AR_TXCFG':hex(value),'threshold_64B':(value&0x3f0)>>4,
        'DMA_size_field':value&7,'selector_restored':reading['selector_restored'],'reg_access_abi':1}


def snapshot(role, iface, debugfs=None):
    out = {"captured": stamp(), "host": cmd(["hostname"]),
           "CPU_stat":cmd(['cat','/proc/stat']),"loadavg":cmd(['cat','/proc/loadavg']),
           "ip": cmd(["ip", "-j", "addr", "show", "dev", iface]),
           "wireless": cmd(["iw", "dev", iface, "info"]),
           "power_save": cmd(["iw", "dev", iface, "get", "power_save"]),
           "rts": cmd(["iwconfig", iface]),
           "phy": cmd(["iw", "dev", iface, "station", "dump"]),
           "time": cmd(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "NTP"]),
           "iperf_version": cmd(["iperf3", "--version"])}
    if role == "AP":
        out["stations"] = out["phy"]
        out["survey"] = cmd(["iw", "dev", iface, "survey", "dump"])
        out["tshark_version"] = cmd(["tshark", "--version"])
    else:
        phy_link = Path("/sys/class/net") / iface / "phy80211"
        out['survey']=cmd(['iw','dev',iface,'survey','dump'])
        phy_name = phy_link.resolve().name
        if not re.fullmatch(r"phy\d+", phy_name):
            raise RuntimeError("cannot resolve wireless PHY for " + iface)
        phy_debug = Path("/sys/kernel/debug/ieee80211") / phy_name
        out["phy_name"] = phy_name
        out["link"] = cmd(["iw", "dev", iface, "link"])
        out["qdisc"] = cmd(["tc", "-s", "qdisc", "show", "dev", iface])
        optional=[phy_debug/'aqm',*sorted((phy_debug/('netdev:'+iface)/'stations').glob('*/txq'))]
        out['software_queue_debug']={str(p):cmd(['cat',str(p)]) for p in optional if p.exists()}
        out["queues"] = cmd(["cat", str(phy_debug / "ath9k" / "queues")])
        out["edca"] = cmd(["cat", str(Path(debugfs).with_name("edca_ctrl"))])
        out["basic_access_override"] = cmd(["cat", str(phy_debug / "ath9k" / "basic_access_override")])
        out["rate_mask_2ghz"] = cmd(["cat", str(phy_debug / ("netdev:" + iface) / "rc_rateidx_mask_2ghz")])
        out["dcf_header"] = cmd(["head", "-n", "6", debugfs])
        out['retry_counter_reset']=retry_reset_state(debugfs,out['edca']['stdout'])
        out['native_xmit']=cmd(['cat',str(phy_debug/'ath9k/xmit')])
        out['FIFO_config']=fifo_state(debugfs)
    return out


def valid_preflight(data, role, iface, ap_ip, cohort_diagnostic=False):
    failures = []
    for key in ("ip", "wireless", "power_save", "iperf_version"):
        if data[key]["exit_code"]:
            failures.append(key + " command failed")
    if role == "STA":
        link = data["link"]["stdout"]
        if data["link"]["exit_code"] or "Connected to" not in link or "SSID: ath9k_test" not in link:
            failures.append("not associated to ath9k_test")
        if "Power save: off" not in data["power_save"]["stdout"]:
            failures.append("power save not off")
        if "hol_abi=1" not in data["dcf_header"]["stdout"]:
            failures.append("HoL ABI missing")
        if (data["basic_access_override"]["stdout"].strip() == "1" and
                "basic_retry_abi=2" not in data["dcf_header"]["stdout"].split("\n", 1)[0].split()):
            failures.append("Basic retry ABI 2 missing; legacy counter omits preceding rate series")
        if re.search(r'RTS thr=500 B',data.get('rts',{}).get('stdout','')):
            if not cohort_diagnostic:
                failures.append('RTS count calibration unresolved: preceding-series failed RTS missing even in candidate ABI 2')
                if 'rts_retry_abi=2' not in data['dcf_header']['stdout'].split('\n',1)[0].split():
                    failures.append('RTS retry ABI 2 missing')
            reset=data.get('retry_counter_reset',{})
            if reset.get('exit_code')!=0 or reset.get('reset_enabled') is not False or not reset.get('selector_restored'):
                failures.append('RTS retry-counter reset state unknown/enabled')
        if data["edca"]["exit_code"]:
            failures.append("EDCA readback failed")
        if data["basic_access_override"]["exit_code"]:
            failures.append("Basic override readback failed")
        if data["rate_mask_2ghz"]["exit_code"]:
            failures.append("rate mask readback failed")
        if cohort_diagnostic and data['rate_mask_2ghz']['stdout'].strip()!='0x100':
            failures.append('cohort fixed-24-Mbps mask missing')
        if ap_ip not in cmd(["ip", "route", "get", ap_ip])["stdout"]:
            failures.append("no route to AP wireless IP")
    elif data["tshark_version"]["exit_code"]:
        failures.append("tshark unavailable for monitor audit")
    return failures


def export_air_frames(pcap, output, report):
    """Export stable, small 802.11 fields while the capture host has tshark."""
    fields = ("frame.time_epoch", "wlan.fc.type_subtype", "wlan.ta", "wlan.ra",
              "wlan.bssid", "radiotap.datarate", "radiotap.dbm_antsignal")
    command = ["tshark", "-r", str(pcap), "-T", "fields", "-E", "header=y"]
    for field in fields:
        command += ["-e", field]
    started = stamp()
    try:
        with Path(output).open("w") as handle:
            value = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE,
                                   text=True, timeout=90)
        result = {"command": command, "exit_code": value.returncode,
                  "stderr": value.stderr, "started": started, "finished": stamp()}
    except Exception as exc:
        result = {"command": command, "exit_code": -1, "stderr": repr(exc),
                  "started": started, "finished": stamp()}
    save(report, result)
    if result["exit_code"]:
        raise RuntimeError("tshark monitor export failed: " + result["stderr"])


def wait_until(target_ns, abort, watch=None, health_fault=None):
    while True:
        if abort.is_set():
            raise RuntimeError("controller abort")
        if health_fault is not None and health_fault.is_set():
            raise RuntimeError("wireless association lost during measurement")
        if watch is not None and watch.poll() is not None:
            raise RuntimeError("iperf client exited before scheduled boundary: rc=%s" % watch.returncode)
        remaining = (target_ns - time.time_ns()) / 1e9
        if remaining <= 0:
            return stamp()
        abort.wait(min(max(remaining - 0.003, 0.0005), 0.25))


def collect_journal(out, since_epoch):
    value = cmd(["journalctl", "-k", "--since", "@" + str(since_epoch), "--no-pager"], 12)
    (out / "kernel_journal.txt").write_text(value["stdout"] + value["stderr"])


def idle_sta_window(args,out,state,plan,abort):
    """Keep a non-sending associated STA observed on the same absolute window."""
    t0=plan['t0_ns'];warm=plan['warmup_s'];measure=plan['measure_s'];tail=plan['tail_s']
    state['idle_sta']=True
    # The idle path polls association for health only, never to trigger a window.
    fault=threading.Event();stop=threading.Event()
    from sender_selection import IdleAssociationMonitor,idle_grace_seconds,idle_grace_policy
    association=IdleAssociationMonitor(idle_grace_seconds(plan),idle_grace_policy(plan))
    def health():
        with (out/'queue_health.jsonl').open('w') as log:
            while not stop.is_set():
                link=cmd(['iw','dev',args.iface,'link'])
                at=stamp()
                log.write(json.dumps({'at':at,'link':link,'queue':cmd(['cat',str(Path(args.debugfs).with_name('queues'))])})+'\n');log.flush()
                if not association.observe(at['monotonic_ns'],'Connected to' in link['stdout'],link['exit_code']==0):fault.set();return
                stop.wait(1)
    thread=threading.Thread(target=health,daemon=True);thread.start()
    try:
        wait_until(t0+warm*1_000_000_000,abort,health_fault=fault)
        state['events']['driver_start_call']=stamp();Path(args.debugfs).write_text('start\n')
        state['events']['driver_start_return']=stamp()
        save(out/'config_window_start.json',snapshot('STA',args.iface,args.debugfs))
        wait_until(t0+(warm+measure)*1_000_000_000,abort,health_fault=fault)
        state['events']['driver_stop_call']=stamp();Path(args.debugfs).write_text('stop\n')
        state['events']['driver_stop_return']=stamp()
        (out/'dcf_stats.txt').write_text(Path(args.debugfs).read_text())
        save(out/'config_window_stop.json',snapshot('STA',args.iface,args.debugfs))
        wait_until(t0+(warm+measure+tail)*1_000_000_000,abort,health_fault=fault)
    finally:
        stop.set();thread.join(timeout=2)
        state['idle_association_health']=association.report()
    if not state['idle_association_health']['pass']:
        raise RuntimeError('idle association grace exceeded or not recovered')


def wait_idle_preflight_association(iface,timeout_s,abort):
    """Readiness only: await natural reassociation before armed/T0, never repair."""
    deadline=time.monotonic()+timeout_s;observations=[]
    while True:
        link=cmd(['iw','dev',iface,'link']);observations.append({'at':stamp(),'link':link})
        remaining=deadline-time.monotonic()
        if link['exit_code']!=0:return {'pass':False,'reason':'iw link query failed','observations':observations}
        if 'Connected to' in link['stdout'] and remaining>=0:return {'pass':True,'observations':observations}
        if remaining<=0:return {'pass':False,'reason':'readiness association wait expired','observations':observations}
        if abort.wait(min(.25,remaining)):raise RuntimeError('controller abort while awaiting idle readiness')

def worker(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    state = {"role": args.role, "armed": None, "plan": None, "events": {},
             "errors": [], "processes": {}, "valid": False}
    abort = threading.Event()
    socket_path = out / "control.sock"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    sock.listen(8)
    selected = threading.Event()
    health_fault = threading.Event()
    created_monitor = False
    def listener():
        while not abort.is_set():
            try:
                conn, _ = sock.accept()
                with conn:
                    payload = json.loads(conn.recv(65536).decode())
                    if payload.get("action") == "abort":
                        abort.set()
                        reply = {"ok": True, "aborted": stamp()}
                    elif payload.get("action") == "plan" and not selected.is_set():
                        plan = payload["plan"]
                        if int(plan["t0_ns"]) < time.time_ns() + 5_000_000_000:
                            reply = {"ok": False, "error": "T0 too close"}
                        else:
                            state["plan"] = plan
                            state["events"]["plan_received"] = stamp()
                            save(out / "state.json", state)
                            selected.set()
                            reply = {"ok": True, "received": stamp()}
                    else:
                        reply = {"ok": False, "error": "already planned/unknown action"}
                    conn.sendall(json.dumps(reply).encode())
            except OSError:
                break
            except Exception as exc:
                state["errors"].append("listener: " + repr(exc))
    threading.Thread(target=listener, daemon=True).start()
    procs = {}
    cohort_capture=None
    try:
        before = snapshot(args.role, args.iface, args.debugfs)
        save(out / "config_before.json", before)
        cohort_diagnostic=getattr(args,'cohort_diagnostic',False)
        failures = valid_preflight(before, args.role, args.iface, args.ap_ip,cohort_diagnostic)
        if failures==['not associated to ath9k_test'] and args.role=='STA' and getattr(args,'idle_sta',False) and getattr(args,'idle_association_readiness_wait_s',0)==5:
            save(out/'config_readiness_initial.json',before)
            state['idle_association_readiness_wait']=wait_idle_preflight_association(args.iface,5,abort)
            save(out/'idle_association_readiness_wait.json',state['idle_association_readiness_wait'])
            if state['idle_association_readiness_wait']['pass']:
                before=snapshot(args.role,args.iface,args.debugfs)
                save(out/'config_before.json',before)
                failures=valid_preflight(before,args.role,args.iface,args.ap_ip,cohort_diagnostic)
        if failures:
            raise RuntimeError("preflight: " + "; ".join(failures))
        if cohort_diagnostic:
            state.update(kind='complete-service cohort diagnostic',formal_probability_approved=False)
            if args.role=='STA' and getattr(args,'cohort_trace_profile','full')=='full':
                from cohort_node_capture import Capture
                spec=json.loads(Path(args.cohort_spec).read_text())
                cohort_capture=Capture(spec,out).start()
            if getattr(args,'fifo_diagnostic',False):
                state.update(kind='FIFO isolation diagnostic',trace_profile=args.cohort_trace_profile)
        if args.role == "AP":
            for port in args.ports:
                err = (out / ("iperf_server_%d.stderr" % port)).open("w")
                p = subprocess.Popen(["iperf3", "-s", "-1", "-p", str(port), "-J",
                                      "--logfile", str(out / ("iperf_server_%d.json" % port))],
                                     stdout=subprocess.DEVNULL, stderr=err)
                procs["server_%d" % port] = (p, err)
            time.sleep(0.6)
            for port in args.ports:
                check = cmd(["ss", "-ltn", "sport", "=", ":%d" % port])
                if check["exit_code"] or ":%d" % port not in check["stdout"]:
                    raise RuntimeError("iperf server not ready on %d: %s" % (port, check))
            pcap_err = (out / "tcpdump.stderr").open("w")
            filt = "udp and (" + " or ".join("port %d" % p for p in args.ports) + ")"
            pcap = subprocess.Popen(["tcpdump", "-i", args.iface, "-s", "128", "-U", "-w",
                                     str(out / "ap_udp.pcap"), filt], stdout=subprocess.DEVNULL,
                                    stderr=pcap_err)
            procs["tcpdump"] = (pcap, pcap_err)
            time.sleep(0.4)
            if pcap.poll() is not None:
                raise RuntimeError("AP tcpdump exited before armed")
            monitor_info = cmd(["iw", "dev", args.monitor_iface, "info"])
            if monitor_info["exit_code"]:
                created = cmd(["iw", "dev", args.iface, "interface", "add",
                               args.monitor_iface, "type", "monitor"])
                if created["exit_code"]:
                    raise RuntimeError("cannot create monitor interface: " + str(created))
                created_monitor = True
            monitor_up = cmd(["ip", "link", "set", args.monitor_iface, "up"])
            monitor_info = cmd(["iw", "dev", args.monitor_iface, "info"])
            if (monitor_up["exit_code"] or monitor_info["exit_code"] or
                    "type monitor" not in monitor_info["stdout"]):
                raise RuntimeError("monitor interface setup/readback failed")
            state["monitor"] = {"interface": args.monitor_iface,
                                "created_by_worker": created_monitor,
                                "info": monitor_info}
        state["armed"] = stamp()
        save(out / "state.json", state)
        while not selected.wait(0.25):
            if abort.is_set():
                raise RuntimeError("aborted before T0")
            if args.role == "AP" and any(p.poll() is not None for k,(p,_) in procs.items() if k.startswith("server")):
                raise RuntimeError("AP server exited before T0")
        plan = state["plan"]
        from sender_selection import validate_worker_selection
        validate_worker_selection(plan,args.role,getattr(args,'port',None),getattr(args,'idle_sta',False))
        t0 = int(plan["t0_ns"])
        warm = int(plan["warmup_s"])
        measure = int(plan["measure_s"])
        tail = int(plan["tail_s"])
        monitor_seconds = int(plan.get("monitor_capture_s", 0))
        end = t0 + (warm + measure + tail) * 1_000_000_000
        if plan.get('window_observation'):
            from window_observation import WindowObserver
            window_observer=WindowObserver(args.iface,out,plan,abort).start()
        wait_until(t0 - 1_000_000_000, abort)
        if args.role == "STA":
            check = cmd(["iw", "dev", args.iface, "link"])
            if check["exit_code"] or "Connected to" not in check["stdout"]:
                raise RuntimeError("STA lost association before T0")
        else:
            check = cmd(["iw", "dev", args.iface, "station", "dump"])
            if len(re.findall(r"associated:\s+yes", check["stdout"])) < 4:
                raise RuntimeError("AP has fewer than four associated STAs before T0")
        wait_until(t0, abort)
        state["events"]["t0_reached"] = stamp()
        if args.role == 'STA' and getattr(args,'idle_sta',False):
            idle_sta_window(args,out,state,plan,abort)
        elif args.role == "STA":
            err = (out / "iperf_client.stderr").open("w")
            state["events"]["iperf_launch_call"] = stamp()
            p = subprocess.Popen(["iperf3", "-c", args.ap_ip, "-p", str(args.port),
                                  "-u", "-b", args.bitrate, "-l", str(args.payload),
                                  "-t", str(warm + measure + tail + 10), "-J",
                                  "--logfile", str(out / "iperf_client.json")],
                                 stdout=subprocess.DEVNULL, stderr=err)
            procs["client"] = (p, err)
            state["events"]["iperf_spawn_return"] = stamp()
            sample_stop = threading.Event()
            def sample_health():
                with (out / "queue_health.jsonl").open("w") as logfile:
                    while not sample_stop.is_set():
                        link = cmd(["iw", "dev", args.iface, "link"])
                        queue = cmd(["cat", str(Path(args.debugfs).with_name("queues"))])
                        logfile.write(json.dumps({"at": stamp(), "link": link,
                                                  "queue": queue,
                                                  'cpu':[s for s in Path('/proc/stat').read_text().splitlines() if s.startswith('cpu')]}, ensure_ascii=False) + "\n")
                        logfile.flush()
                        if link["exit_code"] or "Connected to" not in link["stdout"]:
                            health_fault.set()
                            break
                        sample_stop.wait(1)
            sampler = threading.Thread(target=sample_health, daemon=True)
            sampler.start()
            wait_until(t0 + warm * 1_000_000_000, abort, p, health_fault)
            if p.poll() is not None:
                raise RuntimeError("iperf client exited during warmup")
            state["events"]["driver_start_call"] = stamp()
            Path(args.debugfs).write_text("start\n")
            state["events"]["driver_start_return"] = stamp()
            started = snapshot(args.role, args.iface, args.debugfs)
            save(out / "config_window_start.json", started)
            line = next((x for x in started["dcf_header"]["stdout"].splitlines() if x.startswith("AC=BE ")), "")
            actual = dict(x.split("=", 1) for x in line.split() if "=" in x)
            expected = plan["expected_signature"]
            for key in ("aifsn", "cwmin", "cwmax", "mode"):
                if actual.get(key) != expected[key]:
                    raise RuntimeError("driver window signature mismatch %s: %s != %s" % (key, actual.get(key), expected[key]))
            if "running=1" not in started["dcf_header"]["stdout"].splitlines()[0]:
                raise RuntimeError("driver window did not start")
            wait_until(t0 + (warm + measure) * 1_000_000_000, abort, p, health_fault)
            state["events"]["driver_stop_call"] = stamp()
            Path(args.debugfs).write_text("stop\n")
            state["events"]["driver_stop_return"] = stamp()
            (out / "dcf_stats.txt").write_text(Path(args.debugfs).read_text())
            save(out / "config_window_stop.json", snapshot(args.role, args.iface, args.debugfs))
            wait_until(end, abort, p, health_fault)
            sample_stop.set(); sampler.join(timeout=2)
            state["events"]["iperf_stop_call"] = stamp()
            if p.poll() is None:
                state["processes"]["client_stop_reason"] = "scheduled_local_SIGINT_at_absolute_end"
                p.send_signal(signal.SIGINT)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.terminate()
                p.wait(timeout=3)
            state["events"]["iperf_stop_return"] = stamp()
        else:
            if monitor_seconds <= 0 or monitor_seconds > measure:
                raise RuntimeError("invalid monitor capture duration")
            monitor_start = t0 + (warm * 1_000_000_000) + \
                ((measure - monitor_seconds) * 1_000_000_000 // 2)
            monitor_stop = monitor_start + monitor_seconds * 1_000_000_000
            state["monitor"].update({"planned_start_ns": monitor_start,
                                     "planned_stop_ns": monitor_stop,
                                     "duration_s": monitor_seconds})
            wait_until(monitor_start, abort, watch=pcap)
            air_err = (out / "monitor_tcpdump.stderr").open("w")
            state["events"]["monitor_start_call"] = stamp()
            air = subprocess.Popen(["tcpdump", "-i", args.monitor_iface, "-s", "256",
                                    "-U", "-w", str(out / "air_monitor.pcap")],
                                   stdout=subprocess.DEVNULL, stderr=air_err)
            procs["air_tcpdump"] = (air, air_err)
            state["events"]["monitor_spawn_return"] = stamp()
            time.sleep(0.15)
            if air.poll() is not None:
                raise RuntimeError("monitor tcpdump exited at capture start")
            wait_until(monitor_stop, abort, watch=air)
            state["events"]["monitor_stop_call"] = stamp()
            air.terminate()
            try:
                air.wait(timeout=5)
            except subprocess.TimeoutExpired:
                air.kill(); air.wait()
            air_err.flush()
            state["events"]["monitor_stop_return"] = stamp()
            export_air_frames(out / "air_monitor.pcap", out / "air_monitor_frames.tsv",
                              out / "air_monitor_tshark.json")
            wait_until(end, abort, watch=pcap)
            state["events"]["ap_end_reached"] = stamp()
            for key,(p,_) in procs.items():
                if key.startswith("server"):
                    from sender_selection import required_server
                    if not required_server(key,plan):
                        if p.poll() is None:p.terminate()
                        p.wait(timeout=3)
                        state['processes'][key+'_idle_stop']='unused sender-set server, stopped locally after absolute end'
                        continue
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        state["errors"].append(key + " did not finish")
            save(out / "config_after.json", snapshot(args.role, args.iface, args.debugfs))
        state["valid"] = not state["errors"]
    except Exception as exc:
        state["errors"].append(repr(exc))
        if args.role == "STA" and "driver_start_call" in state["events"] and "driver_stop_call" not in state["events"]:
            try:
                Path(args.debugfs).write_text("stop\n")
                (out / "dcf_stats.txt").write_text(Path(args.debugfs).read_text())
                state["events"]["emergency_driver_stop"] = stamp()
            except Exception as e:
                state["errors"].append("emergency stop: " + repr(e))
    finally:
        if 'window_observer' in locals():
            abort.set()
            state['window_observation_cleanup']=window_observer.close()
        if args.role == "STA" and "sample_stop" in locals():
            sample_stop.set()
            sampler.join(timeout=2)
        for key,(p,handle) in procs.items():
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait()
            state["processes"][key] = {"exit_code": p.returncode, "finished": stamp()}
            handle.close()
        if cohort_capture is not None:
            try:
                time.sleep(2)  # post-flow ANI observation only; never a synchronization mechanism
                cohort_capture.close()
            except Exception as e:state['errors'].append('cohort capture: '+repr(e))
        if args.role=='STA' and getattr(args,'fifo_diagnostic',False):
            try:save(out/'config_after.json',snapshot(args.role,args.iface,args.debugfs))
            except Exception as e:state['errors'].append('FIFO postflight: '+repr(e))
        if args.role == "AP" and created_monitor:
            down = cmd(["ip", "link", "set", args.monitor_iface, "down"])
            deleted = cmd(["iw", "dev", args.monitor_iface, "del"])
            state.setdefault("monitor", {})["cleanup"] = {"down": down, "delete": deleted}
            if down["exit_code"] or deleted["exit_code"]:
                state["errors"].append("monitor interface cleanup failed")
        collect_journal(out, int((state["armed"] or stamp())["epoch_ns"] // 1_000_000_000))
        state["valid"] = not state["errors"]
        state["finished"] = stamp()
        save(out / "state.json", state)
        sock.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def send(args):
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(Path(args.out) / "control.sock"))
    payload = {"action": args.action}
    if args.action == "plan":
        payload["plan"] = json.loads(args.plan)
    s.sendall(json.dumps(payload).encode())
    print(s.recv(65536).decode())


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("arm")
    a.add_argument("--out", required=True)
    a.add_argument("--role", choices=("AP", "STA"), required=True)
    a.add_argument("--iface", required=True)
    a.add_argument("--ap-ip", required=True)
    a.add_argument("--port", type=int)
    a.add_argument("--ports", type=int, nargs="*")
    a.add_argument("--bitrate", default="5M")
    a.add_argument("--payload", type=int, default=1200)
    a.add_argument("--debugfs")
    a.add_argument("--monitor-iface", default="apsta_mon0")
    a.add_argument('--cohort-diagnostic',action='store_true')
    a.add_argument('--cohort-spec')
    a.add_argument('--cohort-trace-profile',choices=['full','off'],default='full')
    a.add_argument('--fifo-diagnostic',action='store_true')
    a.add_argument('--idle-sta',action='store_true',help='associated observer, no iperf client')
    a.add_argument('--idle-association-readiness-wait-s',type=int,choices=[0,5],default=0)
    b = sub.add_parser("send")
    b.add_argument("--out", required=True)
    b.add_argument("--action", choices=("plan", "abort"), required=True)
    b.add_argument("--plan")
    args = p.parse_args()
    if args.command == "arm":
        worker(args)
    else:
        send(args)


if __name__ == "__main__":
    main()
