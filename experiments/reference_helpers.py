"""Extracted reference-engine helpers; no campaign entry or automatic actions."""
import concurrent.futures
import re
import shlex
from sync_run import probe, correct_clock, write
IFACE = "wlp2s0"  # historical reference only; live integration must discover interface
MONITOR = "calmon0"

def need(result, label):
    if result["code"]:
        raise RuntimeError(label + ": " + str(result))
    return result["stdout"]


def root(remote, command, timeout=30):
    return need(remote.run(command, sudo=True, timeout=timeout), remote.name + " " + command)


def background(remote, command, directory, tag, sudo=False):
    wrapped = "sh -c " + shlex.quote(
        "nohup " + command + " >" + shlex.quote(directory + "/" + tag + ".stdout") +
        " 2>" + shlex.quote(directory + "/" + tag + ".stderr") + " </dev/null & echo $!")
    result = remote.run(wrapped, sudo=sudo)
    need(result, tag + " launch")
    return int(result["stdout"].strip())


def create_monitor(remote):
    exists = remote.run("iw dev " + MONITOR + " info")
    if not exists["code"]:
        raise RuntimeError(remote.name + " reserved monitor interface already exists")
    root(remote, "iw dev %s interface add %s type monitor" % (IFACE, MONITOR))
    root(remote, "ip link set %s up" % MONITOR)
    info = need(remote.run("iw dev %s info" % MONITOR), remote.name + " monitor info")
    if "type monitor" not in info:
        raise RuntimeError(remote.name + " monitor readback failed")
    return info


def delete_monitor(remote):
    down = remote.run("ip link set %s down" % MONITOR, sudo=True)
    deleted = remote.run("iw dev %s del" % MONITOR, sudo=True)
    return {"down": down, "delete": deleted}


def active_survey(text):
    blocks = re.split(r"\n(?=Survey data from)", text)
    block = next((value for value in blocks if "[in use]" in value), "")
    def field(label):
        match = re.search(r"%s:\s*(\d+) ms" % re.escape(label), block)
        return int(match.group(1)) if match else None
    frequency = re.search(r"frequency:\s*(\d+) MHz", block)
    return {"frequency_mhz": int(frequency.group(1)) if frequency else None,
            "active_ms": field("channel active time"),
            "busy_ms": field("channel busy time"),
            "receive_ms": field("channel receive time"),
            "transmit_ms": field("channel transmit time"), "raw": text}


def survey_delta(before, after):
    result = {key: (after[key] - before[key] if before.get(key) is not None and after.get(key) is not None else None)
              for key in ("active_ms", "busy_ms", "receive_ms", "transmit_ms")}
    result["busy_fraction"] = (result["busy_ms"] / result["active_ms"]
                               if result["busy_ms"] is not None and result["active_ms"] else None)
    return result


def stop_capture(remote, pid):
    # Cleanup waits inspect process state; they never synchronize traffic.
    code = ("import os,signal,time; p=%d; " % pid +
            "os.kill(p,signal.SIGINT); " +
            "exec(\"for _ in range(40):\\n try:\\n  s=open('/proc/%d/stat'%p).read().split()[2]\\n except FileNotFoundError: break\\n if s=='Z': break\\n time.sleep(.05)\")")
    result = remote.run("python3 -c " + shlex.quote(code), sudo=True)
    if result["code"] and "No such process" not in result["stderr"]:
        raise RuntimeError(remote.name + " capture stop failed: " + str(result))


def prefetched_text(remote, path):
    with remote.sftp.open(path, "rb") as handle:
        handle.prefetch()
        return handle.read().decode(errors="replace")


def clock_check(remotes, audit):
    history = []
    for attempt in range(3):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            clocks = dict(zip(remotes, pool.map(lambda r: probe(r, 9), remotes.values())))
        history.append({"attempt": attempt, "clocks": clocks})
        write(audit, history)
        if all(c["within_50ms_proven"] for c in clocks.values()):
            return clocks
        corrections = {}
        for name, value in clocks.items():
            if not value["within_50ms_proven"]:
                corrections[name] = correct_clock(remotes[name], value["best"]["offset_ns"])
        history[-1]["corrections"] = corrections
        write(audit, history)
    raise RuntimeError("clock gate failed before measurement; see " + str(audit))
