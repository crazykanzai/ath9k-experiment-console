"""Read-only analysis of a synchronized run, with absolute-time AP pcap buckets.

The AP UDP pcap is the authoritative exact-window goodput source. iperf3
intervals are independently mapped onto wall time and overlap-clipped for
diagnostics; their integer-second timestamp alone is not subsecond-accurate.
"""

import argparse
import csv
import hashlib
import json
import re
import shutil
import struct
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sta.tools.dcf_stats import parse_dcf_stats, summarize_dcf_stats  # noqa: E402


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def pcap_udp(path, ports):
    with path.open("rb") as f:
        header = f.read(24)
        magic = header[:4]
        if magic == b"\xd4\xc3\xb2\xa1":
            endian, factor = "<", 1000
        elif magic == b"\xa1\xb2\xc3\xd4":
            endian, factor = ">", 1000
        elif magic == b"\x4d\x3c\xb2\xa1":
            endian, factor = "<", 1
        elif magic == b"\xa1\xb2\x3c\x4d":
            endian, factor = ">", 1
        else:
            raise ValueError("unsupported pcap format")
        linktype = struct.unpack(endian + "I", header[20:24])[0]
        if linktype != 1:
            raise ValueError("expected Ethernet AP pcap; got linktype %d" % linktype)
        packets = defaultdict(list)
        while True:
            h = f.read(16)
            if not h:
                break
            if len(h) != 16:
                raise ValueError("truncated pcap packet header")
            seconds, frac, included, original = struct.unpack(endian + "IIII", h)
            frame = f.read(included)
            if len(frame) != included:
                raise ValueError("truncated pcap packet")
            if len(frame) < 42 or frame[12:14] != b"\x08\x00":
                continue
            ihl = (frame[14] & 15) * 4
            if frame[23] != 17 or len(frame) < 14 + ihl + 8:
                continue
            udp = 14 + ihl
            src, dst, length = struct.unpack("!HHH", frame[udp:udp+6])
            if dst in ports and length >= 8:
                packets[dst].append((seconds * 1_000_000_000 + frac * factor, length-8))
        return packets


def air_monitor_audit(path, station_macs, ap_mac):
    """Summarize tshark's tabular export without requiring tshark on Windows."""
    subtype_names = {8: "beacon", 27: "rts", 28: "cts", 32: "data", 40: "qos_data"}
    totals = defaultdict(int)
    stations = {name: {"mac": mac, "rts": 0, "cts": 0, "data": 0,
                       "qos_data": 0, "qos_rates_mbps": defaultdict(int)}
                for name, mac in station_macs.items()}
    beacon_bssids = set()
    rows = 0
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            raw = (row.get("wlan.fc.type_subtype") or "").split(",", 1)[0].strip()
            if not raw:
                continue
            try:
                subtype = int(raw, 0)
            except ValueError:
                continue
            rows += 1
            name = subtype_names.get(subtype, "subtype_%d" % subtype)
            totals[name] += 1
            ta = (row.get("wlan.ta") or "").split(",", 1)[0].strip().lower()
            ra = (row.get("wlan.ra") or "").split(",", 1)[0].strip().lower()
            bssid = (row.get("wlan.bssid") or "").split(",", 1)[0].strip().lower()
            rate = (row.get("radiotap.datarate") or "").split(",", 1)[0].strip()
            if subtype == 8 and bssid and bssid != ap_mac:
                beacon_bssids.add(bssid)
            for station, values in stations.items():
                mac = values["mac"]
                if subtype == 27 and ta == mac:
                    values["rts"] += 1
                elif subtype == 28 and ra == mac:
                    values["cts"] += 1
                elif subtype == 32 and ta == mac:
                    values["data"] += 1
                elif subtype == 40 and ta == mac:
                    values["qos_data"] += 1
                    if rate:
                        values["qos_rates_mbps"][rate] += 1
    for values in stations.values():
        values["qos_rates_mbps"] = dict(values["qos_rates_mbps"])
    return {"parsed_rows": rows, "subtype_counts": dict(totals), "stations": stations,
            "foreign_beacon_bssids": sorted(beacon_bssids),
            "foreign_beacon_bss_count": len(beacon_bssids)}


def tcpdump_stats(text):
    def number(label):
        match = re.search(r"(\d+) packets? " + label, text)
        return int(match.group(1)) if match else None
    received = number("received by filter")
    interface = number("dropped by interface")
    # tcpdump info() prints ps_ifdrop only when nonzero.  A complete summary
    # without that optional line therefore reports zero, not missing data.
    # https://github.com/the-tcpdump-group/tcpdump/blob/master/tcpdump.c
    if interface is None and received is not None and number("dropped by kernel") is not None:
        interface = 0
    return {"captured": number("captured"), "received_by_filter": received,
            "kernel_dropped": number("dropped by kernel"),
            "interface_dropped": interface,
            "interface_drop_fraction": interface / received if interface is not None and received else None}


def clip_intervals(iperf, anchor_ns, start_ns, stop_ns, offset_ns):
    buckets = defaultdict(float)
    records = []
    for item in iperf.get("intervals", []):
        total = item.get("sum") or (item.get("streams") or [{}])[0]
        a = anchor_ns + round(float(total["start"]) * 1e9) - offset_ns
        b = anchor_ns + round(float(total["end"]) * 1e9) - offset_ns
        size = float(total.get("bytes", 0))
        records.append({"start_ns": a, "stop_ns": b, "bytes": size})
        for second in range(max(0, (a-start_ns)//1_000_000_000),
                            min((stop_ns-start_ns)//1_000_000_000,
                                (b-start_ns+999_999_999)//1_000_000_000)):
            x = max(a, start_ns + second*1_000_000_000)
            y = min(b, start_ns + (second+1)*1_000_000_000)
            if y > x and b > a:
                buckets[second] += size * (y-x)/(b-a)
    return records, [buckets[i] for i in range((stop_ns-start_ns)//1_000_000_000)]


def analyze(folder):
    manifest = load(folder / "manifest.json")
    plan = manifest.get("plan")
    if not plan:
        raise ValueError("no T0 plan; preflight/abort-only run")
    t0 = int(plan["t0_ns"])
    warm, measure, tail = (int(plan[x]) for x in ("warmup_s", "measure_s", "tail_s"))
    start, stop = t0 + warm*1_000_000_000, t0 + (warm+measure)*1_000_000_000
    end = t0 + (warm+measure+tail)*1_000_000_000
    raw_files = [p for p in (folder / "raw").rglob("*") if p.is_file()]
    digests = {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in raw_files}
    clock = manifest.get("clock_after_correction", manifest["clock_before"])
    offsets = {name: value["best"]["offset_ns"] for name,value in clock.items()}
    packets = pcap_udp(folder / "raw" / "AP" / "ap_udp.pcap", set(manifest["ports"]))
    per_sta, timeseries, issues = {}, {}, []
    capture_log = (folder / "raw" / "AP" / "tcpdump.stderr").read_text(errors="replace")
    dropped = re.search(r"(\d+) packets dropped by kernel", capture_log)
    if dropped is None or int(dropped.group(1)):
        issues.append("AP capture loss unknown/nonzero")
    ap_mac = re.search(r"addr ([0-9a-f:]{17})", manifest["node_config"]["AP"]["wireless"]["stdout"], re.I).group(1).lower()
    station_macs = {name: re.search(r"addr ([0-9a-f:]{17})", manifest["node_config"][name]["wireless"]["stdout"], re.I).group(1).lower()
                    for name in ("STA1", "STA2", "STA3", "STA4")}
    air_audit = air_monitor_audit(folder / "raw" / "AP" / "air_monitor_frames.tsv",
                                  station_macs, ap_mac)
    air_log = (folder / "raw" / "AP" / "monitor_tcpdump.stderr").read_text(errors="replace")
    air_capture_stats = tcpdump_stats(air_log)
    if air_capture_stats["kernel_dropped"] is None or air_capture_stats["kernel_dropped"]:
        issues.append("802.11 monitor capture loss unknown/nonzero")
    if (air_capture_stats["interface_drop_fraction"] is None or
            air_capture_stats["interface_drop_fraction"] > 0.01):
        issues.append("802.11 monitor interface drop fraction unknown/exceeds 1%")
    tshark_export = load(folder / "raw" / "AP" / "air_monitor_tshark.json")
    if tshark_export.get("exit_code") != 0:
        issues.append("802.11 monitor tshark export failed")
    ap_state = load(folder / "raw" / "AP" / "state.json")
    monitor_events = ap_state.get("events", {})
    monitor_plan = ap_state.get("monitor", {})
    expected_monitor_start = int(monitor_plan.get("planned_start_ns", 0))
    expected_monitor_stop = int(monitor_plan.get("planned_stop_ns", 0))
    monitor_deviation_ms = {}
    for key, target in (("monitor_start_call", expected_monitor_start),
                        ("monitor_stop_call", expected_monitor_stop)):
        if target and key in monitor_events:
            monitor_deviation_ms[key] = (monitor_events[key]["epoch_ns"] - offsets["AP"] - target) / 1e6
        else:
            issues.append("missing 802.11 monitor timing event: " + key)
    if any(abs(value) > 100 for value in monitor_deviation_ms.values()):
        issues.append("802.11 monitor boundary deviation exceeds 100 ms")
    expected_mode = plan["expected_signature"]["mode"]
    for name, values in air_audit["stations"].items():
        if values["qos_data"] < 10:
            issues.append(name + " monitor capture has too few QoS data frames")
        if expected_mode == "basic" and (values["rts"] or values["cts"]):
            issues.append(name + " Basic air audit observed RTS/CTS")
        if expected_mode == "rts-cts" and values["rts"] == 0:
            issues.append(name + " RTS/CTS air audit observed no RTS")
    for i in range(1,5):
        name, port = "STA%d" % i, manifest["ports"][i-1]
        base = folder / "raw" / name
        state = load(base / "state.json")
        ap_json = load(folder / "raw" / "AP" / ("iperf_server_%d.json" % port))
        client_json = load(base / "iperf_client.json")
        sta_ip = manifest["node_config"][name]["wireless_ipv4"][0]
        ap_ip = manifest["ap_wireless_ip"]
        client_connection = client_json.get("start", {}).get("connected", [{}])[0]
        ap_connection = ap_json.get("start", {}).get("connected", [{}])[0]
        if (client_connection.get("local_host") != sta_ip or
                client_connection.get("remote_host") != ap_ip or
                ap_connection.get("local_host") != ap_ip or
                ap_connection.get("remote_host") != sta_ip):
            issues.append(name + " iperf did not use read-back wireless IPs")
        dcf_text = (base / "dcf_stats.txt").read_text()
        stats = summarize_dcf_stats(parse_dcf_stats(dcf_text, ap_mac))
        if stats["running"] or abs(stats["elapsed_us"] - measure*1_000_000) > 100_000:
            issues.append(name + " driver window duration/running mismatch")
        signature = plan["expected_signature"]
        for key in ("aifsn", "cwmin", "cwmax", "mode"):
            if stats["configuration"].get(key) != signature[key]:
                issues.append(name + " driver config mismatch: " + key)
        if state["errors"]:
            issues.append(name + " worker errors: " + str(state["errors"]))
        stopped = load(base / "config_window_stop.json")
        if stats['configuration'].get('mode')=='rts-cts':
            for configuration in (load(base/'config_before.json'),stopped):
                reset=configuration.get('retry_counter_reset',{})
                if reset.get('exit_code')!=0 or reset.get('reset_enabled') is not False or not reset.get('selector_restored'):
                    issues.append(name+' RTS retry-counter reset readback failed/enabled')
        if "Connected to" not in stopped["link"]["stdout"]:
            issues.append(name + " association missing at window stop")
        events = state["events"]
        expected = {"iperf_launch_call": t0, "driver_start_call": start,
                    "driver_stop_call": stop, "iperf_stop_call": end}
        deviations_ms = {key: (events[key]["epoch_ns"] - offsets[name] - target)/1e6
                         for key,target in expected.items() if key in events}
        if len(deviations_ms) != len(expected):
            issues.append(name + " missing timing event")
        if any(abs(x) > 100 for x in deviations_ms.values()):
            issues.append(name + " boundary deviation exceeds 100 ms")
        spawn_deviation_ms=(events['iperf_spawn_return']['epoch_ns']-offsets[name]-t0)/1e6
        if abs(spawn_deviation_ms)>100:
            issues.append(name+' iperf spawn-return deviation exceeds 100 ms')
        native = packets[port]
        adjusted = [(ts - offsets["AP"], size) for ts,size in native]
        full_buckets = [0]*(warm+measure+tail)
        for ts,size in adjusted:
            if t0 <= ts < end:
                full_buckets[(ts-t0)//1_000_000_000] += size
        buckets = full_buckets[warm:warm+measure]
        timeseries[name] = full_buckets
        bytes_window = sum(buckets)
        if not native:
            issues.append(name + " no AP UDP capture packets")
            anchor = t0
        else:
            anchor = native[0][0]
        mapped, clipped = clip_intervals(ap_json, anchor, start, stop, offsets["AP"])
        counts = stats["counters"] or {}
        if not stats["attempt_probability_valid"]:
            issues.append(name + " RTS attempt probability outside validated ABI/ACKed scope")
        hol = stats["hol"] or {}
        count = hol.get("hol_service_samples",0)
        per_sta[name] = {
            "port": port, "ap_captured_window_bytes": bytes_window,
            "goodput_mbps": bytes_window*8/measure/1e6,
            "hol_mean_us": stats["hol_mean_us"], "hol_sample_count": count,
            "hol_p50_us": None, "hol_p90_us": None, "hol_p95_us": None, "hol_p99_us": None,
            "hol_quantile_status": "unavailable: driver exports aggregate moments, not per-packet distribution",
            "hol_min_us": hol.get("hol_service_min_us"), "hol_max_us": hol.get("hol_service_max_us"),
            "exchange_attempts": counts.get("exchange_attempts",0),
            "exchange_successes": counts.get("exchange_successes",0),
            "exchange_success_probability": stats["hol_request_success_probability"],
            "attempt_probability_valid": stats["attempt_probability_valid"],
            "attempt_probability_caveat": stats["hol_probability_caveat"],
            "packet_failures": counts.get("packet_failures", 0),
            "driver_elapsed_us": stats["elapsed_us"],
            "boundary_deviation_ms": deviations_ms,
            "iperf_spawn_return_deviation_ms": spawn_deviation_ms,
            "boundary_operation_brackets_ms": {label:[(events[k]['epoch_ns']-offsets[name]-target)/1e6 for k in pair]
                for label,pair,target in [('iperf_process_launch',('iperf_launch_call','iperf_spawn_return'),t0),
                    ('driver_start_write',('driver_start_call','driver_start_return'),start),
                    ('driver_stop_write',('driver_stop_call','driver_stop_return'),stop)]},
            "first_AP_business_UDP_arrival_deviation_ms": next(((ts-offsets['AP']-t0)/1e6 for ts,size in native if size==manifest['parameters']['payload_bytes']),None),
            "startup_timestamp_scope": 'launch-call/spawn-return bracket; first AP UDP arrival is a receive-side diagnostic, not exact PHY transmit time',
            "clock_offset_ms": offsets[name]/1e6,
            "clock_uncertainty_ms": clock[name]["best"]["uncertainty_ns"]/1e6,
            "iperf_interval_anchor_method": "first AP UDP pcap packet; approximate interval phase",
            "iperf_intervals_absolute": mapped,
            "iperf_intervals_clipped_bytes_by_common_second": clipped,
            "client_error": client_json.get("error"), "ap_error": ap_json.get("error"),
            "worker_processes": state["processes"],
        }
        client_error, ap_error = client_json.get("error"), ap_json.get("error")
        planned_interrupt = (client_error == "interrupt - the client has terminated" and
                             ap_error == "the client has terminated" and
                             state["processes"].get("client", {}).get("exit_code") == 1 and
                             "iperf_stop_call" in events and
                             abs(events["iperf_stop_call"]["epoch_ns"] - end) < 100_000_000 and
                             len(client_json.get("intervals", [])) >= measure and
                             len(ap_json.get("intervals", [])) >= measure)
        per_sta[name]["iperf_termination_class"] = "expected_absolute_deadline_SIGINT" if planned_interrupt else "normal_or_unexpected"
        if (client_error or ap_error) and not planned_interrupt:
            issues.append(name + " unexpected iperf JSON error")
    values = [per_sta["STA%d" % i]["goodput_mbps"] for i in range(1,5)]
    aggregate = sum(values)
    attempts = sum(v["exchange_attempts"] for v in per_sta.values())
    successes = sum(v["exchange_successes"] for v in per_sta.values())
    samples = sum(v["hol_sample_count"] for v in per_sta.values())
    weighted_hol = sum((v["hol_mean_us"] or 0)*v["hol_sample_count"] for v in per_sta.values())/samples if samples else None
    times = {key: [load(folder / "raw" / ("STA%d" % i) / "state.json")["events"].get(key,{}).get("epoch_ns")
                    for i in range(1,5)] for key in ("iperf_launch_call","driver_start_call","driver_stop_call","iperf_stop_call")}
    spreads = {key: (max(v)-min(v))/1e6 if all(x is not None for x in v) else None for key,v in times.items()}
    if any(v is None or v > 100 for v in spreads.values()):
        issues.append("four-STA boundary spread > 100 ms/missing")
    after_clock = manifest.get("clock_after", {})
    if any(not v.get("within_50ms_proven",False) for k,v in after_clock.items() if k != "AP"):
        issues.append("post-run clock gate failed")
    if manifest["status"] not in ("completed_pending_analysis", "valid_dry_run", "valid_formal_run", "invalid_analysis"):
        issues.append("controller status: " + manifest["status"])
    if not manifest["parameters"].get("dry_run", False):
        issues.append("formal completeness gate: collision/interference calibration is not yet recorded")
    if ap_state["errors"] or any(v.get("exit_code") != 0 for v in ap_state["processes"].values()):
        issues.append("AP worker/server/capture process error")
    metrics = {"valid": not issues, "issues": issues,
               "formal_ready": False,
               "formal_blockers": [
                   "Collision-only interpretation still needs medium/interference calibration",
               ],
               "common_window": {"t0_ns": t0, "measurement_start_ns": start,
                                 "measurement_stop_ns": stop, "traffic_end_ns": end,
                                 "measure_seconds": measure},
               "throughput_source": "AP wlp2s0 UDP pcap, exact shared absolute bucket, IP/UDP payload bytes",
               "air_monitor_audit": {**air_audit, "expected_mode": expected_mode,
                                     "capture_duration_s": monitor_plan.get("duration_s"),
                                     "boundary_deviation_ms": monitor_deviation_ms,
                                     "capture_stats": air_capture_stats,
                                     "cts_observation_note": "same-PHY AP monitor may not expose AP-transmitted CTS; per-STA RTS plus driver mode is the RTS audit gate"},
               "per_sta": per_sta, "aggregate_goodput_mbps": aggregate,
               "jain_fairness": aggregate**2/(4*sum(x*x for x in values)) if any(values) else None,
               "combined_exchange_success_probability": successes/attempts if attempts and all(v["attempt_probability_valid"] for v in per_sta.values()) else None,
               "combined_exchange_successes": successes, "combined_exchange_attempts": attempts,
               "combined_hol_mean_us": weighted_hol, "combined_hol_samples": samples,
               "combined_hol_quantiles": None,
               "start_stop_spread_ms": spreads,
               "clock_before": manifest["clock_before"], "clock_after": after_clock,
               "raw_sha256": digests, "timeseries_mbps": {n:[v*8/1e6 for v in xs] for n,xs in timeseries.items()}}
    return metrics


def render(folder, metrics):
    import matplotlib.pyplot as plt
    window = metrics["common_window"]
    measure = window["measure_seconds"]
    warm = int((window["measurement_start_ns"]-window["t0_ns"])/1e9)
    tail = int((window["traffic_end_ns"]-window["measurement_stop_ns"])/1e9)
    fig, ax = plt.subplots(figsize=(11,5))
    series = metrics["timeseries_mbps"]
    for name, values in series.items():
        ax.step(range(-warm, measure+tail), values, where="post", label=name)
    total = [sum(series[n][i] for n in series) for i in range(warm+measure+tail)]
    ax.step(range(-warm, measure+tail), total, where="post", color="black", linewidth=2, label="Aggregate")
    ax.axvspan(-warm, 0,
               alpha=.12, color="orange", label="Warm-up (excluded)")
    ax.axvspan(0, measure, alpha=.09, color="green", label="Common measurement")
    ax.axvspan(measure, measure+tail,
               alpha=.12, color="gray", label="Tail (excluded)")
    ax.axvline(0, color="green", linestyle="--")
    ax.axvline(measure, color="green", linestyle="--")
    ax.set(xlabel="Seconds from common measurement start", ylabel="AP UDP goodput (Mbps)",
           title="Four-STA synchronized run: exact wall-clock buckets")
    ax.legend(ncol=3, fontsize=8)
    ax.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(folder / "full_timeline.png", dpi=160)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    args = p.parse_args()
    folder = args.run_dir.resolve()
    manifest = load(folder / "manifest.json")
    metrics = analyze(folder)
    write = lambda path, value: path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    write(folder / "metrics.json", metrics)
    render(folder, metrics)
    if Path(__file__).resolve() != (folder / "analyze_sync.py").resolve():
        shutil.copy2(Path(__file__), folder / "analyze_sync.py")
    status = "VALID" if metrics["valid"] else "INVALID"
    lines = ["# 同步四 STA %s 分析" % ("dry-run" if manifest["parameters"].get("dry_run") else "正式运行"),
             "", "- 判定：**%s**；%s" % (status, "; ".join(metrics["issues"]) or "无异常"),
             "- 正式实验就绪：**否**；运行期短时 monitor 审计已接入，仍需完成信道/碰撞环境校准。",
             "- 公共窗口：%d s；吞吐来源：AP 侧 UDP pcap 绝对时间桶，不使用每个 iperf 自身的第 n 秒对齐。" % metrics["common_window"]["measure_seconds"],
             "- 聚合吞吐：%.3f Mbps；Jain 公平指数：%.4f。" % (metrics["aggregate_goodput_mbps"], metrics["jain_fairness"] or 0),
             "- 合并交换成功概率：%s；加权 HoL 均值：%s us，样本数 %d。" % (metrics["combined_exchange_success_probability"], metrics["combined_hol_mean_us"], metrics["combined_hol_samples"]),
             "- 本阶段只比较 HoL 均值曲线；驱动没有逐包分布，分位数为 null，但不构成本阶段阻碍。",
             "- 空口审计：期望模式 `%s`，抓包 %s s，发现外部 Beacon BSSID %d 个。" %
             (metrics["air_monitor_audit"]["expected_mode"], metrics["air_monitor_audit"]["capture_duration_s"],
              metrics["air_monitor_audit"]["foreign_beacon_bss_count"]), "",
             "| 节点 | 吞吐 Mbps | HoL 均值 us | HoL 样本 | 交换成功率 |", "|---|---:|---:|---:|---:|"]
    for name,v in metrics["per_sta"].items():
        lines.append("| %s | %.3f | %s | %d | %s |" % (name,v["goodput_mbps"],v["hol_mean_us"],v["hol_sample_count"],v["exchange_success_probability"]))
    lines += ["", "| 节点 | 打流启动偏差 ms | 驱动开始偏差 ms | 驱动停止偏差 ms | 打流结束偏差 ms | 时钟偏差 ± 不确定度 ms |",
              "|---|---:|---:|---:|---:|---:|"]
    for name,v in metrics["per_sta"].items():
        d = v["boundary_deviation_ms"]
        lines.append("| %s | %.3f | %.3f | %.3f | %.3f | %.2f ± %.2f |" % (
            name, d["iperf_launch_call"], d["driver_start_call"],
            d["driver_stop_call"], d["iperf_stop_call"],
            v["clock_offset_ms"], v["clock_uncertainty_ms"]))
    lines += ["", "| 节点 | QoS DATA | RTS | 发给 STA 的可见 CTS* | 观测到的 QoS 速率 |",
              "|---|---:|---:|---:|---|"]
    for name, value in metrics["air_monitor_audit"]["stations"].items():
        lines.append("| %s | %d | %d | %d | %s |" %
                     (name, value["qos_data"], value["rts"], value["cts"],
                      ", ".join(sorted(value["qos_rates_mbps"])) or "unknown"))
    lines += ["", "\\* AP 同 PHY monitor 可能看不到 AP 自身发送的 CTS；RTS 模式验收以每台 STA 均有 RTS+DATA，并与驱动 `mode=rts-cts` 交叉为准。"]
    lines += ["", "四台本地边界时间离散度 (ms)：" + str(metrics["start_stop_spread_ms"]),
              "", "正式实验前待解决：" + "；".join(metrics["formal_blockers"]),
              "", "完整时序图：`full_timeline.png`。`raw/` 下文件只读处理，SHA-256 见 `metrics.json`。",
              "", "注意：iperf 逐秒 interval 已映射到绝对时间并裁剪到公共桶作为诊断；其起点由 AP 抓包第一包估计。正式吞吐使用 AP 抓包每包时间戳直接裁剪。"]
    (folder / "analysis.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    manifest["valid"] = metrics["valid"]
    manifest["status"] = ("valid_dry_run" if manifest["parameters"].get("dry_run") else "valid_formal_run") if metrics["valid"] else "invalid_analysis"
    manifest["analysis_issues"] = metrics["issues"]
    write(folder / "manifest.json", manifest)
    print(status, folder)


if __name__ == "__main__":
    main()
