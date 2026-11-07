"""Four-STA absolute-time orchestrator. Requires paramiko on controller.

Usage: APSTA_SSH_PASSWORD=... python experiments/sync_run.py --dry-run
       APSTA_SSH_PASSWORD=... python experiments/sync_run.py --formal
No CW scan is implemented. A run is always one fixed read-back configuration.
"""

import argparse
import concurrent.futures
import getpass
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paramiko
from sender_selection import normalize_senders,selection_parameters,inactive_worker
from continuous_ntp import source_address,check as check_ntp_source

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT.parent / "打流分析"
NODES = {"AP": "192.168.100.10", **{"STA%d" % i: "192.168.100.%d" % (10+i) for i in range(1,5)}}
PORTS = [5201, 5202, 5203, 5204]
NODE_SCRIPT = ROOT / "experiments" / "sync_node.py"
REMOTE_SCRIPT = "/home/EXPERIMENT_USER/apsta-sync/sync_node.py"
REMOTE_BASE = "/home/EXPERIMENT_USER/apsta-runs"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Remote:
    def __init__(self, name, address, password):
        if os.environ.get("APSTA_ENABLE_REFERENCE_ENGINE") != "I_UNDERSTAND_FIXED_FOUR_STA":
            raise RuntimeError("Fixed-four reference engine is disabled; see experiments/README.md")
        self.name, self.address, self.password = name, address, password
        self.ssh = paramiko.SSHClient()
        self.ssh.load_system_host_keys()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(address, username="w", password=password, timeout=8,
                         auth_timeout=8, banner_timeout=8)
        self.sftp = self.ssh.open_sftp()

    def run(self, command, sudo=False, timeout=15):
        if sudo:
            command = "sudo -S -p '' " + command
        stdin, stdout, stderr = self.ssh.exec_command(command, timeout=timeout)
        if sudo:
            stdin.write(self.password + "\n")
            stdin.flush()
        stdin.close()
        code = stdout.channel.recv_exit_status()
        return {"code": code, "stdout": stdout.read().decode(errors="replace"),
                "stderr": stderr.read().decode(errors="replace")}

    def file(self, path):
        try:
            with self.sftp.open(path, "r") as f:
                return f.read().decode()
        except IOError:
            return None

    def get_tree(self, remote, local):
        local.mkdir(parents=True, exist_ok=True)
        for entry in self.sftp.listdir_attr(remote):
            source = posixpath.join(remote, entry.filename)
            target = local / entry.filename
            if entry.st_mode & 0o170000 == 0o040000:
                self.get_tree(source, target)
            elif entry.filename != "control.sock":
                self.sftp.get(source, str(target))

    def close(self):
        self.sftp.close(); self.ssh.close()


def probe(remote, repeats=7):
    samples = []
    # Reuse one timestamp process/channel: per-sample Python/SSH process
    # startup used to dominate RTT and give ~25 ms uncertainty on this LAN.
    # This only measures clocks, never launches traffic or determines T0.
    script='import sys,time\nfor line in sys.stdin:\n if line.strip()=="t": print(time.time_ns(),flush=True)'
    stdin,stdout,stderr=remote.ssh.exec_command('python3 -u -c '+shlex.quote(script),timeout=5)
    try:
        for _ in range(repeats):
            before=time.time_ns();stdin.write('t\n');stdin.flush()
            line=stdout.readline();after=time.time_ns()
            if not line:raise RuntimeError(remote.name+' clock timestamp process ended')
            remote_ns=int(line.strip())
            samples.append({"offset_ns":remote_ns-(before+after)//2,
                "uncertainty_ns":(after-before)//2,"rtt_ns":after-before,"controller_mid_ns":(before+after)//2})
    finally:
        stdin.close()
    code=stdout.channel.recv_exit_status()
    if code:raise RuntimeError(remote.name+' clock probe failed: '+stderr.read().decode(errors='replace'))
    best = min(samples, key=lambda x: x["rtt_ns"])
    return {"best": best, "samples": samples,
            'method':'persistent wired SSH timestamp echo, minimum RTT sample; no traffic synchronization polling',
            "within_50ms_proven": abs(best["offset_ns"]) + best["uncertainty_ns"] <= 50_000_000}


def correct_clock(remote, offset_ns):
    # Local wired SSH midpoint fallback when no functioning NTP/PTP source exists.
    # Step the Linux wall clock by the observed offset; monotonic clock is untouched.
    script = "import time; time.clock_settime_ns(time.CLOCK_REALTIME, time.time_ns() - (%d))" % offset_ns
    result = remote.run("python3 -c " + shlex.quote(script), sudo=True)
    if result["code"]:
        raise RuntimeError(remote.name + " clock correction failed: " + str(result))
    return {"method": "wired-SSH midpoint one-time CLOCK_REALTIME step",
            "applied_offset_ns": offset_ns, "result": result,
            "applied_controller_ns": time.time_ns()}


def remote_dir(run_id, name):
    directory=posixpath.join(REMOTE_BASE,run_id,name)
    # Linux sockaddr_un.sun_path is 108 bytes including its terminating NUL.
    # Keep descriptive LOCAL folders; use a recorded compact remote identity.
    if len(posixpath.join(directory,'control.sock').encode('utf-8'))>=104:
        directory=posixpath.join(REMOTE_BASE,'r_'+hashlib.sha256(run_id.encode('utf-8')).hexdigest()[:24],name)
    return directory


def remote_run_id(run_dir):
    """Include the batch namespace; identical leaf labels are not identities."""
    return run_dir.resolve().relative_to(OUTPUT_ROOT.resolve()).as_posix()


def require_unused_remote_directories(remotes, directories):
    for name, remote in remotes.items():
        value = remote.run('test ! -e ' + shlex.quote(directories[name]))
        if value['code']:
            raise RuntimeError(name + ' remote output already exists or cannot be checked; no worker launched: ' + directories[name])


def workers_ready(states, launch_begin_ns):
    for name, state in states.items():
        if not state:continue
        if state.get('finished'):
            raise RuntimeError(name + ' worker ended before readiness acceptance')
        armed = state.get('armed')
        if armed and (not isinstance(armed, dict) or armed.get('epoch_ns', 0) < launch_begin_ns-50_000_000):
            raise RuntimeError(name + ' stale or malformed armed state; no plan delivery')
    return all(state and state.get('armed') for state in states.values())


def node_send(remote, directory, action, plan=None):
    cmd = "python3 %s send --out %s --action %s" % tuple(map(shlex.quote,
        (REMOTE_SCRIPT, directory, action)))
    if plan is not None:
        cmd += " --plan " + shlex.quote(json.dumps(plan, separators=(",", ":")))
    result = remote.run(cmd, sudo=True)
    if result["code"]:
        raise RuntimeError(remote.name + " send failed: " + str(result))
    reply = json.loads(result["stdout"].strip())
    if not reply.get("ok"):
        raise RuntimeError(remote.name + " rejected " + action + ": " + str(reply))
    return reply


def read_state(remote, directory):
    raw = remote.file(posixpath.join(directory, "state.json"))
    return json.loads(raw) if raw else None


def abort_workers(remotes, directories, timeout_s=20, now=time.monotonic, sleep=time.sleep):
    """Record abort causality and await cleanup, never trigger/sync traffic.

    Initial observations, original node errors and exit codes remain intact.
    Missing nodes are explicitly pending, not silently counted as finished.
    The wait budget starts after bounded SSH abort deliveries.
    """
    report = {"dispatch": {}, "initial_states": {}, "final_states": {},
              "state_read_errors": {}, "wait_budget_s": timeout_s}
    def stamp():
        return {"epoch_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns()}
    def states():
        result = {}
        for name, remote in remotes.items():
            channel = None
            previous_timeout = None
            try:
                channel = remote.sftp.get_channel() if hasattr(remote, 'sftp') else None
                previous_timeout = channel.gettimeout() if channel else None
                if channel:channel.settimeout(3)
                result[name] = read_state(remote, directories[name])
            except Exception as exc:
                result[name] = None
                report["state_read_errors"][name] = repr(exc)
            finally:
                if channel:
                    try:channel.settimeout(previous_timeout)
                    except Exception as exc:report['state_read_errors'][name] = repr(exc)
        return result
    report["initial_states"] = states()
    for name, remote in remotes.items():
        row = {"dispatch_begin": stamp()}
        try:
            row["acknowledgement"] = node_send(remote, directories[name], "abort")
        except Exception as exc:
            row["error"] = repr(exc)
        row["dispatch_end"] = stamp()
        report["dispatch"][name] = row
    deadline = now() + timeout_s
    while True:
        report["final_states"] = states()
        pending = [n for n,v in report["final_states"].items() if not v or not v.get("finished")]
        if not pending or now() >= deadline:
            break
        sleep(min(.2, max(0, deadline-now())))
    report.update(pending_nodes=pending, all_finished=not pending,
                  wait_timed_out=bool(pending), observation_end=stamp(),
                  causality_note="exit after abort dispatch may be secondary; timestamps do not prove causality by themselves")
    report['exit_order_annotations'] = {}
    for name, state in report['final_states'].items():
        if not state:continue
        begin = report['dispatch'][name]['dispatch_begin']['epoch_ns']
        annotations = {}
        for proc, value in state.get('processes', {}).items():
            # The real AP schema also contains explanatory string entries.
            if not isinstance(value, dict):
                annotations[proc] = 'metadata_not_a_process_record'
                continue
            finished = value.get('finished')
            epoch = finished.get('epoch_ns', 0) if isinstance(finished, dict) else 0
            annotations[proc] = ('after_abort_dispatch_order_only' if isinstance(epoch, (int,float)) and epoch >= begin
                                 else 'before_dispatch_or_timestamp_unavailable')
        report['exit_order_annotations'][name] = annotations
    return report


def find_iface(data, role):
    wireless = data["wireless"]["stdout"]
    if role == "AP" and "type AP" not in wireless:
        raise RuntimeError("AP wireless interface not AP")
    if role != "AP" and "type managed" not in wireless:
        raise RuntimeError("STA wireless interface not managed")
    ips = json.loads(data["ip"]["stdout"])[0].get("addr_info", [])
    return [x["local"] for x in ips if x.get("family") == "inet"]


def current_signature(data, payload=1200, cohort_diagnostic=False):
    line = next((x for x in data["edca"]["stdout"].splitlines() if x.startswith("AC=BE ")), "")
    edca = dict(x.split("=",1) for x in line.split() if "=" in x)
    if edca.get("status_name") != "verified":
        raise RuntimeError("BE EDCA hardware readback not verified: " + line)
    for key in ("aifs", "cwmin", "cwmax"):
        if edca.get(key) != edca.get("hw_" + key):
            raise RuntimeError("BE EDCA hardware mismatch: " + line)
    radio = data["rts"]["stdout"]
    m = re.search(r"RTS thr[:=](off|\d+)", radio)
    rate = re.search(r"Bit Rate=([0-9.]+) Mb/s", radio)
    if not m or not rate:
        raise RuntimeError("RTS/PHY rate unreadable: " + radio)
    if cohort_diagnostic and data.get('rate_mask_2ghz',{}).get('stdout','').strip()!='0x100':
        raise RuntimeError('cohort diagnostic requires fixed 24 Mbps mask 0x100')
    override_raw = data.get("basic_access_override", {}).get("stdout", "").strip()
    if override_raw not in ("0", "1"):
        raise RuntimeError("Basic override readback missing or invalid: " + override_raw)
    override = int(override_raw)
    if override and m.group(1) != "off":
        raise RuntimeError("Basic override requires RTS threshold off")
    # RTS-off alone is NOT Basic: minstrel fallback/ERP protection produced
    # RTS on all four ath9k STAs in the 2026-10-17 air-mode diagnosis.
    mode = "basic" if override else ("rts-cts" if m.group(1) != "off" and int(m.group(1)) <= payload+66 else "unverified")
    return {"aifsn": edca["aifs"], "cwmin": edca["cwmin"],
            "cwmax": edca["cwmax"], "mode": mode,
            "basic_access_override": override,
            "rate_mask_2ghz": data.get("rate_mask_2ghz", {}).get("stdout", "").strip(),
            "rts_threshold": m.group(1), "phy_mbps": '24' if cohort_diagnostic else rate.group(1),
            "channel": re.search(r"channel (\d+) \((\d+) MHz\)", data["wireless"]["stdout"]).group(0)}


def discover_wireless_interface(remote,role):
    result=remote.run('iw dev')
    if result['code']:raise RuntimeError(remote.name+' cannot enumerate wireless interfaces')
    expected='AP' if role=='AP' else 'managed'
    candidates=[]
    for block in re.split(r'(?m)^\s*Interface ',result['stdout'])[1:]:
        name=block.splitlines()[0].strip()
        if re.fullmatch(r'[A-Za-z0-9_.-]+',name) and re.search(r'(?m)^\s*type '+expected+r'\s*$',block):candidates.append(name)
    if len(candidates)!=1:raise RuntimeError(remote.name+' ambiguous/missing '+expected+' wireless interface: '+repr(candidates))
    return candidates[0]

def sta_phy(remote):
    iface=shlex.quote(getattr(remote,'iface','wlp2s0'))
    result = remote.run("basename $(readlink -f /sys/class/net/"+iface+"/phy80211)")
    name = result["stdout"].strip()
    if result["code"] or not re.fullmatch(r"phy\d+", name):
        raise RuntimeError(remote.name + " PHY resolution failed: " + str(result))
    return name


def access_config(remote, phy):
    base = "/sys/kernel/debug/ieee80211/" + phy + "/ath9k/basic_access_override"
    override = remote.run("cat " + base, sudo=True)
    radio = remote.run("iwconfig "+shlex.quote(getattr(remote,'iface','wlp2s0')))
    match = re.search(r"RTS thr[:=](off|\d+)", radio["stdout"])
    if override["code"] or radio["code"] or not match or override["stdout"].strip() not in ("0", "1"):
        raise RuntimeError(remote.name + " access-mode readback failed")
    return {"rts": match.group(1), "override": int(override["stdout"].strip())}


def set_access_config(remote, phy, config):
    rts = remote.run("iw phy " + phy + " set rts " + config["rts"], sudo=True)
    if rts["code"]:
        raise RuntimeError(remote.name + " set RTS failed: " + str(rts))
    path = "/sys/kernel/debug/ieee80211/" + phy + "/ath9k/basic_access_override"
    result = remote.run("sh -c 'echo %d > %s'" % (config["override"], path), sudo=True)
    if result["code"] or access_config(remote, phy) != config:
        raise RuntimeError(remote.name + " access-mode write/readback mismatch")


def inspect_readbacks(remotes, dirs, cohort_diagnostic=False):
    configs = {}
    for name, remote in remotes.items():
        raw = remote.file(posixpath.join(dirs[name], "config_before.json"))
        if not raw:
            raise RuntimeError(name + " config readback unavailable")
        configs[name] = json.loads(raw)
        ips = find_iface(configs[name], name)
        if not ips:
            raise RuntimeError(name + " no wireless IPv4")
        configs[name]["wireless_ipv4"] = ips
    ap_ip = configs["AP"]["wireless_ipv4"][0]
    bssids = []
    signatures = []
    for name in ("STA1", "STA2", "STA3", "STA4"):
        item = configs[name]
        if ap_ip in item["wireless_ipv4"]:
            raise RuntimeError(name + " duplicate AP wireless IP")
        link = item["link"]["stdout"]
        m = re.search(r"Connected to ([0-9a-f:]{17})", link, re.I)
        if not m:
            raise RuntimeError(name + " has no BSSID")
        bssids.append(m.group(1).lower())
        signatures.append(current_signature(item,cohort_diagnostic=cohort_diagnostic))
    if len(set(bssids)) != 1 or bssids[0] != re.search(r"addr ([0-9a-f:]{17})", configs["AP"]["wireless"]["stdout"], re.I).group(1).lower():
        raise RuntimeError("STA BSSID does not match AP")
    if len({json.dumps(s, sort_keys=True) for s in signatures}) != 1:
        raise RuntimeError("AIFS/CW/mode readback mismatch: " + str(signatures))
    if len({configs[n]["wireless_ipv4"][0] for n in ("STA1", "STA2", "STA3", "STA4")}) != 4:
        raise RuntimeError("STA wireless IPv4 addresses are not unique")
    return configs, ap_ip, signatures[0]


def choose_run_dir(label):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    for i in range(1,100):
        p = OUTPUT_ROOT / ("%s_%s_%02d" % (day,label,i))
        if not p.exists():
            p.mkdir()
            return p
    raise RuntimeError("too many runs")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--formal", action="store_true")
    p.add_argument("--access-mode", choices=("basic", "rts"),
                   help="explicitly configure and verify data-frame access mode")
    p.add_argument("--abort-test", action="store_true", help="arm then abort without traffic")
    p.add_argument('--cohort-diagnostic',action='store_true',help='separate raw diagnostic path; never approves formal RTS probability')
    p.add_argument('--bitrate',default='5M')
    p.add_argument('--fifo-diagnostic',choices=['full','off'],help='isolated FIFO test; no formal/cohort metrics when trace is off')
    p.add_argument('--fifo-candidate')
    p.add_argument('--fifo-candidate-sha')
    p.add_argument('--fifo-candidate-hosts',nargs='*',choices=['STA1','STA2','STA3','STA4'],default=[])
    p.add_argument('--bianchi-dai',action='store_true',help='explicit trend profile; approximate RTS counts, full FIFO/cohort acquisition')
    p.add_argument('--single-sta',choices=['STA1','STA2','STA3','STA4'],help='one active sender; other STAs stay associated and sampled, 5/30/5')
    p.add_argument('--single-sta-calibration',action='store_true',help='explicit Basic single-STA formal 15/180/15 calibration; no retry/driver changes')
    p.add_argument('--active-stas',nargs='+',choices=['STA1','STA2','STA3','STA4'],help='explicit Basic trend-profile sender set; all other STAs stay associated and observed')
    p.add_argument('--idle-association-grace-s',type=int,choices=[0,5],default=0,help='explicit opt-in: cumulative idle-only association gap budget; active senders remain strict')
    p.add_argument('--idle-association-grace-policy',choices=['cumulative','per_episode'],default='cumulative',help='explicit per-episode five-second idle-only tolerance; old plans stay cumulative')
    p.add_argument('--passive-listener-spec',type=Path,help='optional independent receive-only listener participating in ready/absolute-time plan')
    p.add_argument('--continuous-ntp-source',type=source_address,help='require this active NTP source before/after; disables controller manual clock correction')
    p.add_argument('--expected-sta-txpower-dbm',type=int,choices=[5,14],default=14,help='expected STA power readback only; does not set power')
    p.add_argument('--radio-plan',type=Path,help='explicit expected AP/per-STA power and legal channel; does not configure radio')
    p.add_argument('--output-dir',type=Path,help='new independent run directory; refuses overwrite')
    p.add_argument("--password", default=os.environ.get("APSTA_SSH_PASSWORD"))
    args = p.parse_args()
    radio=None
    if args.radio_plan:
        from radio_plan import validate_radio_plan
        if not args.bianchi_dai or args.access_mode!='basic':p.error('radio plan requires explicit Basic Bianchi-Dai profile')
        try:radio=validate_radio_plan(json.loads(args.radio_plan.read_text(encoding='utf-8-sig')))
        except (OSError,ValueError,TypeError,KeyError) as exc:p.error('radio plan: '+str(exc))
    if args.expected_sta_txpower_dbm!=14 and (not args.bianchi_dai or args.access_mode!='basic'):
        p.error('explicit low-power expectation requires Basic Bianchi-Dai profile')
    if args.idle_association_grace_policy=='per_episode' and args.idle_association_grace_s!=5:
        p.error('per_episode policy requires --idle-association-grace-s 5')
    if args.passive_listener_spec and (not args.cohort_diagnostic or not args.bianchi_dai or args.access_mode!='basic' or args.active_stas is None or len(args.active_stas)!=1):
        p.error('passive listener requires an explicit single Basic Bianchi-Dai cohort sender')
    if args.idle_association_grace_s and (args.active_stas is None or not args.bianchi_dai or args.access_mode!='basic'):
        p.error('idle grace requires an explicit Basic Bianchi-Dai active sender set')
    if args.active_stas is not None:
        if args.single_sta or args.single_sta_calibration or not args.bianchi_dai or args.access_mode!='basic':
            p.error('--active-stas requires Basic Bianchi-Dai profile and cannot be mixed with single-STA flags')
        try:args.active_stas=normalize_senders(args.active_stas)
        except ValueError as exc:p.error(str(exc))
    selected=args.active_stas if args.active_stas is not None else [args.single_sta] if args.single_sta else ['STA1','STA2','STA3','STA4']
    if args.passive_listener_spec:
        from listener_selection import listener_target
        try:listener_target(json.loads(args.passive_listener_spec.read_text(encoding='utf-8-sig')),selected)
        except (OSError,ValueError,KeyError,TypeError) as exc:p.error('passive target binding: '+str(exc))
    if args.output_dir:
        target=args.output_dir.resolve()
        if not target.is_relative_to(OUTPUT_ROOT.resolve()) or target.exists():
            p.error('output directory must be new and inside experiment root')
    if args.single_sta_calibration and (not args.single_sta or not args.bianchi_dai or args.access_mode!='basic'):
        p.error('single-STA calibration requires explicit single sender, Bianchi-Dai and Basic')
    if args.single_sta and (not args.bianchi_dai or (not args.dry_run and not args.single_sta_calibration)):
        p.error('--single-sta formal requires explicit --single-sta-calibration')
    if args.fifo_diagnostic and not args.cohort_diagnostic:p.error('--fifo-diagnostic requires --cohort-diagnostic')
    if args.bianchi_dai and (args.fifo_diagnostic!='full' or set(args.fifo_candidate_hosts)!={'STA1','STA2','STA3','STA4'}):
        p.error('--bianchi-dai requires full FIFO/cohort trace and all four explicit FIFO candidates')
    if args.fifo_candidate_hosts and (not args.fifo_diagnostic or not args.fifo_candidate or not re.fullmatch('[0-9a-f]{64}',args.fifo_candidate_sha or '')):
        p.error('explicit FIFO diagnostic, candidate path and SHA256 required')
    if args.abort_test and not args.dry_run:
        p.error("--abort-test requires --dry-run")
    if args.formal and not args.access_mode:
        p.error("formal run requires --access-mode basic or rts")
    password = args.password or getpass.getpass("SSH/sudo password: ")
    remotes = {}
    passive=None
    run_dir = None
    warmup, measure, tail = (5, 20, 5) if args.dry_run else (15, 180, 15)
    if (args.single_sta or args.active_stas is not None) and args.dry_run:warmup,measure,tail=5,30,5
    monitor_capture = 10 if args.dry_run else 15
    manifest = {"status": "preparing", "valid": False, "nodes": NODES,
                "ports": PORTS, "parameters": {"warmup_s": warmup, "measure_s": measure, "tail_s": tail,
                "bitrate": args.bitrate, "payload_bytes": 1200, "dry_run": args.dry_run,
                "access_mode": args.access_mode, "monitor_capture_s": monitor_capture,
                "monitor_interface": "apsta_mon0"},
                "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "git_status_short": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
                "controller_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "node_script_sha256": hashlib.sha256(NODE_SCRIPT.read_bytes()).hexdigest(),
                "controller_started": datetime.now().astimezone().isoformat()}
    if args.cohort_diagnostic:
        manifest.update(kind='complete-service cohort diagnostic',formal_probability_approved=False)
        manifest['parameters']['cohort_diagnostic']=True
    if args.fifo_diagnostic:
        manifest.update(kind='FIFO isolation diagnostic',FIFO_trace_profile=args.fifo_diagnostic,formal_probability_approved=False)
        manifest['parameters']['fifo_diagnostic']=args.fifo_diagnostic
    if args.bianchi_dai:
        manifest.update(kind='Bianchi-Dai trend experiment',probability_quality='approximate_not_hardware_calibrated')
        manifest['parameters']['bianchi_dai']=True
        manifest['parameters']['expected_STA_txpower_dBm']=args.expected_sta_txpower_dbm
        if radio is not None:manifest['parameters'].update(radio_plan=radio,window_observation=True)
    if args.single_sta:manifest['parameters']['single_sta']=args.single_sta
    if args.active_stas is not None:manifest['parameters'].update(selection_parameters(selected))
    if args.idle_association_grace_s:manifest['parameters']['idle_association_grace_s']=args.idle_association_grace_s
    if args.idle_association_grace_policy!='cumulative':manifest['parameters']['idle_association_grace_policy']=args.idle_association_grace_policy
    manifest['sender_selection_script_sha256']=hashlib.sha256((ROOT/'experiments/sender_selection.py').read_bytes()).hexdigest()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(Remote, name, ip, password): name for name, ip in NODES.items()}
            for fut in concurrent.futures.as_completed(futures):
                remotes[futures[fut]] = fut.result()
        clock = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(probe, remote): name for name, remote in remotes.items()}
            for fut in concurrent.futures.as_completed(futures):
                clock[futures[fut]] = fut.result()
        manifest["clock_before"] = json.loads(json.dumps(clock))
        if args.continuous_ntp_source:
            manifest['continuous_NTP_before']={n:check_ntp_source(r,args.continuous_ntp_source) for n,r in remotes.items()}
            manifest['parameters']['continuous_ntp_source']=args.continuous_ntp_source
            if not all(v['pass'] for v in manifest['continuous_NTP_before'].values()):
                raise RuntimeError('continuous NTP source/status gate before traffic')
            if not all(v['within_50ms_proven'] and abs(v['best']['offset_ns'])+v['best']['uncertainty_ns']<=10_000_000 for v in clock.values()):
                raise RuntimeError('continuous NTP pre-traffic 10ms margin gate; no manual recentering')
        manifest["controller_time_service"] = subprocess.run(
            ["w32tm", "/query", "/status"], capture_output=True, text=True).stdout
        manifest["clock_gate_pass"] = all(clock[n]["within_50ms_proven"] for n in NODES if n != "AP")
        print("Clock offset ± uncertainty (ms):", {n: (round(v["best"]["offset_ns"]/1e6,2),
              round(v["best"]["uncertainty_ns"]/1e6,2)) for n,v in clock.items()}, flush=True)
        if args.abort_test and not manifest['clock_gate_pass']:
            raise RuntimeError('no-traffic acceptance clock gate failed; no clock correction or worker launch')
        recenter=not args.abort_test and args.cohort_diagnostic and any(abs(v['best']['offset_ns'])>10_000_000 for v in clock.values())
        if not args.continuous_ntp_source and (not manifest["clock_gate_pass"] or recenter):
            manifest['clock_recenter_reason']='pre-traffic margin for diagnostic runs; hard gate remains 50 ms'
            corrections = {}
            for attempt in range(2):
                failures = {n:v for n,v in clock.items() if not v["within_50ms_proven"] or
                    (args.cohort_diagnostic and abs(v['best']['offset_ns'])>10_000_000)}
                if not failures:
                    break
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                    futures = {pool.submit(correct_clock, remotes[n], v["best"]["offset_ns"]): n
                               for n,v in failures.items()}
                    for fut in concurrent.futures.as_completed(futures):
                        corrections.setdefault(futures[fut], []).append(fut.result())
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                    futures = {pool.submit(probe, remote): name for name,remote in remotes.items()}
                    for fut in concurrent.futures.as_completed(futures):
                        clock[futures[fut]] = fut.result()
            manifest["clock_corrections"] = corrections
            manifest["clock_after_correction"] = clock
            manifest["clock_gate_pass"] = all(clock[n]["within_50ms_proven"] for n in NODES if n != "AP")
            print("After correction (ms):", {n: (round(v["best"]["offset_ns"]/1e6,2),
                  round(v["best"]["uncertainty_ns"]/1e6,2)) for n,v in clock.items()}, flush=True)
        # The 50 ms gate applies before any traffic, not only to formal runs.
        if not manifest["clock_gate_pass"] and not args.abort_test:
            raise RuntimeError("STA/controller clock offset cannot be proven within 50 ms; no traffic started")
        ap = remotes["AP"]
        interfaces={name:discover_wireless_interface(remote,name) for name,remote in remotes.items()}
        for name,remote in remotes.items():remote.iface=interfaces[name]
        manifest['wireless_interfaces']=interfaces
        ap_ip_result = ap.run("ip -j addr show dev "+shlex.quote(interfaces['AP']))
        ap_ip = [v["local"] for v in json.loads(ap_ip_result["stdout"])[0]["addr_info"] if v["family"]=="inet"][0]
        phys = {name: sta_phy(remotes[name]) for name in ("STA1", "STA2", "STA3", "STA4")}
        manifest['driver_preflight']={}
        for name in phys:
            identity=remotes[name].run("sh -c " + shlex.quote(
                "uname -r; for m in mac80211 ath9k_hw ath9k_common ath9k; do printf '%s ' \"$m\"; cat /sys/module/\"$m\"/srcversion; done"))
            override=name in args.fifo_candidate_hosts
            verification=remotes[name].run('python3 /usr/local/libexec/apstaselect-driver-persist.py verify'+('' if override else ' --live'),sudo=True)
            manifest['driver_preflight'][name]={'identity':identity,'persistent_verify':verification}
            if identity['code'] or verification['code']:
                raise RuntimeError(name+' persistent/live module verification failed')
            if override:
                candidate=remotes[name].run('sha256sum '+shlex.quote(args.fifo_candidate)+'; modinfo -F srcversion '+shlex.quote(args.fifo_candidate)+'; cat /sys/module/ath9k/srcversion /sys/module/ath9k/parameters/diag_tx_fifo_trigger')
                lines=candidate['stdout'].splitlines()
                if candidate['code'] or len(lines)!=4 or lines[0].split()[0]!=args.fifo_candidate_sha or lines[1]!=lines[2] or lines[3]!='20':raise RuntimeError(name+' FIFO candidate identity/parameter failed')
                # Dependencies must remain the approved original stack.
                disk=remotes[name].run('cat /lib/modules/5.15.0-139-generic/updates/apstaselect/bundle.json')
                bundle=json.loads(disk['stdout'])
                actual=dict(line.split() for line in identity['stdout'].splitlines()[1:])
                if any(actual.get(key)!=bundle['modules'][key]['srcversion'] for key in ['mac80211','ath9k_hw','ath9k_common']):raise RuntimeError(name+' dependency changed')
                manifest['driver_preflight'][name]['explicit_FIFO_candidate']=candidate
        original_access = {name: access_config(remotes[name], phys[name]) for name in phys}
        manifest["original_access_config"] = original_access
        manifest["sta_phy"] = phys
        if args.access_mode:
            desired = {"rts": "off", "override": 1} if args.access_mode == "basic" else {"rts": "500", "override": 0}
            for name in phys:
                set_access_config(remotes[name], phys[name], desired)
            manifest["requested_access_mode"] = args.access_mode
            manifest["access_config_after_setup"] = {name: access_config(remotes[name], phys[name]) for name in phys}
        if radio is not None:
            from radio_plan import matches as radio_matches
            manifest['explicit_radio_plan_readbacks']={}
            for name,remote in remotes.items():
                z=remote.run('iw dev '+shlex.quote(interfaces[name])+' info')
                manifest['explicit_radio_plan_readbacks'][name]=z
                if z['code'] or not radio_matches(z['stdout'],radio,name):raise RuntimeError(name+' explicit radio plan mismatch before arming')
        # Naming is finalized after inspecting current configuration; no CW is changed here.
        sigs = {}
        for name in ("STA1", "STA2", "STA3", "STA4"):
            reads = {}
            phy = phys[name]
            for field, command in {"edca": "cat /sys/kernel/debug/ieee80211/" + phy + "/ath9k/edca_ctrl",
                                   "basic_access_override": "cat /sys/kernel/debug/ieee80211/" + phy + "/ath9k/basic_access_override",
                                   "rate_mask_2ghz": "cat /sys/kernel/debug/ieee80211/" + phy + "/netdev:"+interfaces[name]+"/rc_rateidx_mask_2ghz",
                                   "rts": "iwconfig "+shlex.quote(interfaces[name]), "wireless": "iw dev "+shlex.quote(interfaces[name])+" info"}.items():
                value = remotes[name].run(command, sudo=field in ("edca", "basic_access_override", "rate_mask_2ghz"))
                if value["code"]:
                    raise RuntimeError(name + " " + field + " readback failed")
                reads[field] = value
            sigs[name] = current_signature(reads,cohort_diagnostic=args.cohort_diagnostic)
            if args.cohort_diagnostic:manifest.setdefault('pre_arming_radio_readbacks',{})[name]=reads
        if args.cohort_diagnostic:
            manifest['rate_validation_basis']='0x100 fixed-24-Mbps mask before arming; EVERY actual business descriptor and independent air DATA must be 24 Mbps; idle iwconfig display retained raw, not a waveform truth'
        if args.access_mode and any(s["mode"] != ("basic" if args.access_mode == "basic" else "rts-cts") for s in sigs.values()):
            raise RuntimeError("requested access mode was not read back on every STA: " + str(sigs))
        if len({json.dumps(s, sort_keys=True) for s in sigs.values()}) != 1:
            raise RuntimeError("STA DCF configuration differs: " + str(sigs))
        s = sigs["STA1"]
        label = {"rts-cts": "RTS", "basic": "Basic", "unverified": "Unverified"}[s["mode"]] + "_CW" + s["cwmin"] + "-" + s["cwmax"] + ("_dryrun" if args.dry_run else "_4STA")
        if args.cohort_diagnostic:label+='_cohort'
        if args.fifo_diagnostic:label+='_FIFO_'+args.fifo_diagnostic
        if args.bianchi_dai:label+='_BianchiDai'
        if args.single_sta:label+='_'+args.single_sta+'_single'
        if args.active_stas is not None:label+='_active_'+'-'.join(selected)
        if args.output_dir:
            run_dir=args.output_dir.resolve()
            if not run_dir.is_relative_to(OUTPUT_ROOT.resolve()):raise RuntimeError('output directory outside experiment root')
            run_dir.mkdir(parents=True,exist_ok=False)
        else:run_dir = choose_run_dir(label)
        (run_dir / "controller").mkdir()
        shutil.copy2(Path(__file__), run_dir / "controller" / "sync_run.py")
        shutil.copy2(NODE_SCRIPT, run_dir / "controller" / "sync_node.py")
        shutil.copy2(ROOT/'experiments/sender_selection.py',run_dir/'controller/sender_selection.py')
        for name in NODES:
            (run_dir / "raw" / name).mkdir(parents=True)
        write(run_dir / "manifest.json", manifest)
        print("Run directory:", run_dir, flush=True)
        if args.cohort_diagnostic:
            manifest['idle_survey_before']=ap.run('iw dev '+shlex.quote(interfaces['AP'])+' survey dump')
            time.sleep(5)  # idle characterization, never a traffic trigger
            manifest['idle_survey_after']=ap.run('iw dev '+shlex.quote(interfaces['AP'])+' survey dump')
        run_id = remote_run_id(run_dir)
        manifest['remote_run_identity'] = run_id
        dirs = {name: remote_dir(run_id,name) for name in NODES}
        manifest['remote_directories']=dirs
        require_unused_remote_directories(remotes, dirs)
        for name, remote in remotes.items():
            result = remote.run("mkdir -p %s %s" % (shlex.quote(posixpath.dirname(REMOTE_SCRIPT)),
                                               shlex.quote(posixpath.dirname(dirs[name]))))
            if result["code"]:
                raise RuntimeError(name + " mkdir failed: " + str(result))
            remote.sftp.put(str(NODE_SCRIPT), REMOTE_SCRIPT)
            remote.sftp.put(str(ROOT/'experiments/sender_selection.py'),'/home/EXPERIMENT_USER/apsta-sync/sender_selection.py')
            remote.sftp.put(str(ROOT/'experiments/reg_access.py'),'/home/EXPERIMENT_USER/apsta-sync/reg_access.py')
            if radio is not None:remote.sftp.put(str(ROOT/'experiments/window_observation.py'),'/home/EXPERIMENT_USER/apsta-sync/window_observation.py')
            if args.cohort_diagnostic and name!='AP':
                from cohort_probe_controller import prepare
                from driver_profile import CANONICAL
                spec=prepare(remote,args.fifo_candidate if name in args.fifo_candidate_hosts else CANONICAL)
                local_spec=run_dir/'controller'/('cohort_spec_'+name+'.json')
                write(local_spec,spec)
                remote.sftp.put(str(local_spec),posixpath.join(posixpath.dirname(dirs[name]),name+'_cohort_spec.json'))
                remote.sftp.put(str(ROOT/'experiments/cohort_node_capture.py'),'/home/EXPERIMENT_USER/apsta-sync/cohort_node_capture.py')
        def arm(name):
            remote = remotes[name]
            command = "python3 %s arm --out %s --role %s --iface %s --ap-ip %s" % tuple(
                map(shlex.quote, (REMOTE_SCRIPT, dirs[name], "AP" if name == "AP" else "STA", interfaces[name], ap_ip)))
            if name == "AP":
                command += " --ports " + " ".join(map(str, PORTS)) + " --monitor-iface apsta_mon0"
            else:
                command += " --port %d --debugfs %s --bitrate %s --payload 1200" % (
                    PORTS[int(name[-1])-1], "/sys/kernel/debug/ieee80211/" + phys[name] + "/ath9k/dcf_stats",shlex.quote(args.bitrate))
            if args.cohort_diagnostic:
                command+=' --cohort-diagnostic'
                if name!='AP':command+=' --cohort-spec '+shlex.quote(posixpath.join(posixpath.dirname(dirs[name]),name+'_cohort_spec.json'))
            if args.fifo_diagnostic:command+=' --fifo-diagnostic --cohort-trace-profile '+args.fifo_diagnostic
            if inactive_worker(name,selected):command+=' --idle-sta'
            if inactive_worker(name,selected) and args.idle_association_grace_policy=='per_episode':command+=' --idle-association-readiness-wait-s 5'
            # sudo owns the autonomous worker; stdout/stderr remain local diagnostics.
            # Node worker itself creates its output directory, so redirect to parent.
            wrapped = "sh -c " + shlex.quote("nohup " + command + " >" +
                      shlex.quote(posixpath.join(posixpath.dirname(dirs[name]), name+".stdout")) + " 2>" +
                      shlex.quote(posixpath.join(posixpath.dirname(dirs[name]), name+".stderr")) + " </dev/null &")
            return remote.run(wrapped, sudo=True)
        if args.passive_listener_spec:
            from passive_listener import PassiveSession
            passive=PassiveSession(args.passive_listener_spec,run_dir,password);passive.arm()
            manifest['passive_listener_spec']=json.loads(args.passive_listener_spec.read_text(encoding='utf-8-sig'))
        worker_launch_begin_ns = time.time_ns()
        manifest['worker_launch_begin_ns'] = worker_launch_begin_ns
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = dict(zip(NODES, pool.map(arm, NODES)))
        if any(v["code"] for v in results.values()):
            raise RuntimeError("worker launch failed: " + str(results))
        deadline = time.monotonic() + 25
        states = {}
        while time.monotonic() < deadline:
            states = {n: read_state(remotes[n], dirs[n]) for n in NODES}
            if workers_ready(states, worker_launch_begin_ns):
                break
            time.sleep(0.25)  # readiness only; never used to trigger traffic
        else:
            raise RuntimeError("not all AP/STA workers armed before deadline")
        if passive:passive.ready()
        configs, confirmed_ap_ip, signature = inspect_readbacks(remotes, dirs,args.cohort_diagnostic)
        if passive:
            from listener_selection import verify_listener_readbacks
            manifest['passive_target']=verify_listener_readbacks(passive.spec,selected,configs)
        manifest["node_config"] = configs
        manifest["ap_wireless_ip"] = confirmed_ap_ip
        manifest["dcf_signature"] = signature
        write(run_dir / "manifest.json", manifest)
        if args.abort_test:
            manifest['abort_diagnostics']=abort_workers(remotes, dirs)
            write(run_dir / 'controller/abort_diagnostics.json',manifest['abort_diagnostics'])
            manifest['abort_states']=manifest['abort_diagnostics']['final_states']
            if not manifest['abort_diagnostics']['all_finished']:
                raise RuntimeError('abort-test workers did not finish cleanup; no automatic retry')
            manifest["status"] = "invalid_abort_test"
            manifest["valid"] = False
            return
        # T0 is chosen only after AP+4 STA ready and all config readbacks agree.
        if passive:passive.ready()
        t0 = time.time_ns() + 25_000_000_000
        plan = {"t0_ns": t0, **manifest["parameters"], "expected_signature": signature}
        manifest["plan"] = plan
        manifest["measurement_start_ns"] = t0 + warmup*1_000_000_000
        manifest["measurement_stop_ns"] = t0 + (warmup+measure)*1_000_000_000
        manifest["traffic_stop_ns"] = t0 + (warmup+measure+tail)*1_000_000_000
        write(run_dir / "manifest.json", manifest)
        # Delivery can be sequential: it contains the same future absolute T0.
        acknowledgements = {'MON15':passive.send('plan',plan)} if passive else {}
        acknowledgements.update({name: node_send(remotes[name], dirs[name], "plan", plan) for name in NODES})
        write(run_dir / "controller" / "plan_ack.json", acknowledgements)
        if time.time_ns() > t0 - 10_000_000_000:
            raise RuntimeError("plan delivery left less than 10 seconds before T0")
        print("All planned for", datetime.fromtimestamp(t0/1e9).astimezone().isoformat(), flush=True)
        # Polling here is monitoring/abort detection only, never synchronization.
        deadline = time.monotonic() + max(0, (manifest["traffic_stop_ns"] - time.time_ns())/1e9) + 20
        while time.monotonic() < deadline:
            if passive:passive.poll()
            states = {n: read_state(remotes[n], dirs[n]) for n in NODES}
            if any(v and v.get("errors") for v in states.values()):
                raise RuntimeError("node failure during run: " + str({n:v.get("errors") for n,v in states.items() if v}))
            if all(v and v.get("finished") for v in states.values()):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("workers did not finish before timeout")
        if passive:passive.finish()
        manifest["clock_after"] = {name: probe(remote, 5) for name,remote in remotes.items()}
        if args.continuous_ntp_source:
            manifest['continuous_NTP_after']={n:check_ntp_source(r,args.continuous_ntp_source) for n,r in remotes.items()}
            if not all(v['pass'] for v in manifest['continuous_NTP_after'].values()):
                raise RuntimeError('continuous NTP source/status gate after traffic')
        manifest["status"] = "completed_pending_analysis"
    except Exception as exc:
        manifest["status"] = "invalid"
        manifest["errors"] = [repr(exc)]
        print("INVALID:", repr(exc), file=sys.stderr, flush=True)
        if run_dir is None:
            run_dir = choose_run_dir("preflight_invalid_dryrun")
            (run_dir / "controller").mkdir()
        if 'worker_launch_begin_ns' in manifest:
            try:
                manifest['abort_diagnostics'] = abort_workers(remotes, dirs)
            except Exception as cleanup_exc:
                manifest['abort_diagnostics'] = {'error': repr(cleanup_exc), 'pending_nodes': list(remotes), 'all_finished': False}
            write(run_dir / 'controller' / 'abort_diagnostics.json', manifest['abort_diagnostics'])
    finally:
        if passive:
            try:passive.cleanup()
            except Exception as e:
                manifest.setdefault('collection_errors',{})['MON15']=repr(e);manifest['status']='invalid';manifest['valid']=False
        if "original_access" in locals():
            for name, original in original_access.items():
                if name in manifest.get('abort_diagnostics', {}).get('pending_nodes', []):
                    manifest.setdefault('restore_errors', {})[name] = 'deferred: worker cleanup not confirmed after abort; no concurrent configuration write'
                    continue
                try:
                    set_access_config(remotes[name], phys[name], original)
                    manifest.setdefault("restored_access_config", {})[name] = access_config(remotes[name], phys[name])
                except Exception as exc:
                    manifest.setdefault("restore_errors", {})[name] = repr(exc)
            if manifest.get("restore_errors"):
                manifest["status"] = "invalid_restore_failed"
                manifest["valid"] = False
        if run_dir:
            if 'worker_launch_begin_ns' in manifest:
                for name,remote in remotes.items():
                    try:
                        remote.get_tree(dirs[name], run_dir / "raw" / name)
                        for stream in ['stdout','stderr']:
                            value=remote.file(posixpath.join(posixpath.dirname(dirs[name]),name+'.'+stream))
                            if value is not None:(run_dir/'raw'/name/('worker_'+stream+'.txt')).write_text(value,encoding='utf-8')
                    except Exception as exc:
                        manifest.setdefault("collection_errors", {})[name] = repr(exc)
            write(run_dir / "manifest.json", manifest)
        for remote in remotes.values():
            remote.close()
    print(run_dir, flush=True)
    return 0 if manifest["status"] == "completed_pending_analysis" else 2


if __name__ == "__main__":
    sys.exit(main())
