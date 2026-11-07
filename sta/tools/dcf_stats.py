"""Read ath9k DCF/HoL measurements from debugfs/dcf_stats.

The old xmit snapshot/delta commands remain for archived experiment records.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("dcf_attempts", "dcf_successes", "dcf_drops", "dcf_timed_successes", "dcf_success_delay_ns")
ACS = ("BE", "BK", "VI", "VO")
HOL_FIELDS = (
    "hol_service_samples", "hol_service_sum_us", "hol_service_sum_sq_us2",
    "hol_service_min_us", "hol_service_max_us", "hol_service_anchor_skips",
    "hol_service_missing_enqueue", "hol_service_order_breaks",
    "hol_service_drop_breaks", "hol_service_config_breaks",
    "hol_service_excluded_breaks", "hol_service_invalid_deltas",
    "hol_service_overflow_samples",
)
ATTEMPT_FIELDS = (
    "packets", "packet_successes", "packet_failures",
    "exchange_attempts", "exchange_successes", "rts_attempts",
    "rts_successes", "data_attempts", "data_successes",
    "short_retries", "long_retries",
)


def _tokens(line: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in line.split() if "=" in part)


def parse_dcf_stats(text: str, peer: str, ac: str = "BE") -> dict:
    """Parse one STA's destination-AP row from the 04 debugfs ABI."""
    if ac not in ACS:
        raise ValueError(f"unknown AC: {ac}")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", peer):
        raise ValueError("peer must be the AP BSSID MAC address")
    lines = text.splitlines()
    header = next((_tokens(line) for line in lines if line.startswith("version=")), None)
    if not header or header.get("version") != "2" or header.get("hol_abi") != "1":
        raise ValueError("dcf_stats hol_abi=1 is unavailable; load matching mac80211 + ath9k 04 modules")
    ac_row = next((_tokens(line) for line in lines if line.startswith(f"AC={ac} ")), None)
    row = next((_tokens(line) for line in lines if line.startswith("peer=")
                and _tokens(line).get("peer", "").lower() == peer.lower()
                and _tokens(line).get("AC") == ac), None)
    result = {
        "source": "ath9k_dcf_stats_hol_abi_1",
        "peer": peer.lower(), "ac": ac,
        "generation": int(header["generation"]),
        "running": bool(int(header["running"])),
        "elapsed_us": int(header["elapsed_us"]),
        "capture": header.get("capture", "unknown"),
        "basic_retry_abi": int(header.get("basic_retry_abi", "1")),
        "rts_retry_abi": int(header.get("rts_retry_abi", "1")),
        "configuration": ac_row,
        "counters": None, "hol": None,
    }
    if row is None:
        return result
    try:
        counters = {key: int(row[key]) for key in ATTEMPT_FIELDS}
        hol = {key: int(row[key]) for key in HOL_FIELDS}
    except (KeyError, ValueError) as exc:
        raise ValueError(f"incomplete dcf_stats peer/AC row: {exc}") from exc
    if any(value < 0 for value in (*counters.values(), *hol.values())):
        raise ValueError("dcf_stats counters must be nonnegative")
    if (counters["packet_successes"] + counters["packet_failures"] != counters["packets"]
            or counters["exchange_successes"] > counters["exchange_attempts"]
            or hol["hol_service_samples"] > counters["packet_successes"]):
        raise ValueError("inconsistent dcf_stats packet, attempt or HoL counters")
    result["counters"] = counters
    result["hol"] = hol
    return result


def summarize_dcf_stats(snapshot: dict) -> dict:
    """Return UI/record metrics; HoL is conditional on valid ACKed samples."""
    counters = snapshot["counters"] or {}
    hol = snapshot["hol"] or {}
    samples = hol.get("hol_service_samples", 0)
    packets = counters.get("packets", 0)
    attempts = counters.get("exchange_attempts", 0)
    request_p = counters["exchange_successes"] / attempts if attempts else None
    final_p = counters["packet_successes"] / packets if packets else None
    mode = (snapshot.get("configuration") or {}).get("mode")
    # 2026-10-17 air audit disproved even the candidate prior-DATA-only fix:
    # terminal shortretry loses preceding-series failed RTS for ACKed MPDUs.
    # No production RTS accounting ABI is currently approved.
    rts_rejected = mode == "rts-cts"
    # Cohort campaign observed exhausted Basic frames with raw_long equal to
    # the terminal budget. Applying the ACK-success +1 overcounts each such
    # failure; current ABI 2 has not been revised/deployed for this path.
    basic_terminal_rejected = (mode == 'basic' and snapshot.get('basic_retry_abi',1)>=2
                               and counters.get('packet_failures',0)>0)
    diagnostic_p = request_p
    if rts_rejected or basic_terminal_rejected:
        request_p = None
    return {
        **snapshot,
        "hol_mean_us": hol["hol_service_sum_us"] / samples if samples else None,
        "hol_second_moment_us2": hol["hol_service_sum_sq_us2"] / samples if samples else None,
        "hol_samples": samples,
        "hol_request_success_probability": request_p,
        "hol_final_packet_success_probability": final_p,
        # Historical API names remain available for existing consumers.
        "packet_success_probability": final_p,
        "attempt_success_probability": request_p,
        "diagnostic_uncalibrated_attempt_probability": diagnostic_p if rts_rejected or basic_terminal_rejected else None,
        "attempt_probability_valid": not (rts_rejected or basic_terminal_rejected),
        "hol_probability_definition": "ACKed exchanges / reconstructed HoL transmission requests",
        "hol_probability_caveat": (
            "RTS probability unavailable: preceding-series failed RTS are not recoverable from terminal retry fields; candidate ABI 2 failed air audit"
            if rts_rejected else
            "Basic probability unavailable with terminal failures: ABI 2 applies ACK-success +1 to exhausted frames; failure path not calibrated"
            if basic_terminal_rejected else
            "Legacy Basic counter omits preceding hardware rate series; attempt p is not calibrated"
            if mode == "basic" and snapshot.get("basic_retry_abi", 1) < 2 else
            "Empirical estimator of paper p only when requests and failures match the collision model"
        ),
        "definition": "max(mac80211 TXQ enqueue, previous TX-status callback) to ACKed TX-status callback",
        "clock": "host_monotonic",
    }


def parse_xmit(text: str) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {ac: {} for ac in ACS}
    for field in FIELDS:
        match = re.search(rf"(?m)^{field}\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", text)
        if not match:
            raise ValueError(f"xmit is missing {field}; load the instrumented ath9k driver")
        for ac, value in zip(ACS, match.groups()):
            result[ac][field] = int(value)
    return result


def delta(before: dict, after: dict, ac: str = "BE") -> dict[str, float | int | None]:
    if ac not in ACS:
        raise ValueError(f"unknown AC: {ac}")
    values = {field: after["counters"][ac][field] - before["counters"][ac][field] for field in FIELDS}
    if any(value < 0 for value in values.values()):
        raise ValueError("counter decreased; driver was reset or counters wrapped")
    attempts = values["dcf_attempts"]
    successes = values["dcf_successes"]
    timed = values["dcf_timed_successes"]
    if successes + values["dcf_drops"] > attempts or timed > successes:
        raise ValueError("inconsistent success or timing counters")
    values["conditional_success_probability"] = successes / attempts if attempts else None
    finished = successes + values["dcf_drops"]
    values["packet_final_success_probability"] = successes / finished if finished else None
    values["mean_success_delay_us"] = values["dcf_success_delay_ns"] / timed / 1000 if timed else None
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="save a debugfs/xmit counter snapshot")
    snap.add_argument("--xmit", type=Path, required=True)
    snap.add_argument("--output", type=Path, required=True)
    report = sub.add_parser("delta", help="compute STA attempt p and successful frame delay")
    report.add_argument("--before", type=Path, required=True)
    report.add_argument("--after", type=Path, required=True)
    report.add_argument("--ac", choices=ACS, default="BE")
    hol = sub.add_parser("hol-snapshot", help="save one STA's 04 dcf_stats HoL window")
    hol.add_argument("--dcf-stats", type=Path, required=True)
    hol.add_argument("--peer", required=True, help="associated AP BSSID")
    hol.add_argument("--ac", choices=ACS, default="BE")
    hol.add_argument("--output", type=Path, required=True)
    control = sub.add_parser("hol-control", help="start, stop or reset the driver measurement window")
    control.add_argument("--dcf-stats", type=Path, required=True)
    control.add_argument("command", choices=("start", "stop", "reset"))
    args = parser.parse_args()
    if args.command == "hol-control":
        args.dcf_stats.write_text(args.command + "\n", encoding="ascii")
        print(args.command)
    elif args.command == "hol-snapshot":
        value = summarize_dcf_stats(
            parse_dcf_stats(args.dcf_stats.read_text(encoding="utf-8"), args.peer, args.ac)
        )
        value["captured_utc"] = datetime.now(timezone.utc).isoformat()
        value["debugfs_path"] = str(args.dcf_stats)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        print(args.output)
    elif args.command == "snapshot":
        value = {"captured_utc": datetime.now(timezone.utc).isoformat(),
                 "source": str(args.xmit),
                 "counters": parse_xmit(args.xmit.read_text(encoding="utf-8"))}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        print(args.output)
    else:
        before = json.loads(args.before.read_text(encoding="utf-8"))
        after = json.loads(args.after.read_text(encoding="utf-8"))
        print(json.dumps(delta(before, after, args.ac), indent=2))


if __name__ == "__main__":
    main()
