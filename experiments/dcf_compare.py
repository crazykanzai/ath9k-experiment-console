"""Compare four saturated uplink STAs with a finite-n DCF collision model.

All timing inputs are measured wall-clock microseconds for the same PHY and
payload as the receiver-side UDP throughput measurements.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path


def prediction(n: int, cwmin: int, cwmax: int, slot_us: float,
               success_us: float, collision_us: float,
               payload_bytes: int) -> dict[str, float]:
    if n < 1 or cwmin < 0 or cwmax < cwmin or min(slot_us, success_us, collision_us) <= 0 or payload_bytes <= 0:
        raise ValueError("station count, CW, timing and payload must be positive and valid")
    w = cwmin + 1
    ratio = (cwmax + 1) / w
    k = round(math.log2(ratio))
    if k < 0 or w * 2 ** k != cwmax + 1:
        raise ValueError("CWmax + 1 must equal (CWmin + 1) * 2^K")

    def attempt_probability(p: float) -> float:
        r = 1 - p
        denominator = 1 + p * sum(r ** i * w * 2 ** i for i in range(k)) + r ** k * w * 2 ** k
        return 2 / denominator

    # tau - attempt_probability(p(tau)) is increasing: contention lowers p,
    # raising the mean backoff stage and reducing the attempt probability.
    lo, hi = 0.0, 1.0
    for _ in range(100):
        tau = (lo + hi) / 2
        p = (1 - tau) ** (n - 1)
        if tau < attempt_probability(p):
            lo = tau
        else:
            hi = tau
    tau = (lo + hi) / 2
    p = (1 - tau) ** (n - 1)
    idle = (1 - tau) ** n
    success = n * tau * p
    collision = max(0.0, 1 - idle - success)
    mean_slot_us = idle * slot_us + success * success_us + collision * collision_us
    return {
        "tau": tau, "p": p, "idle_probability": idle,
        "success_probability": success, "collision_probability": collision,
        "predicted_mbps": success * payload_bytes * 8 / mean_slot_us,
    }


def compare(rows: list[dict[str, str]], *, n: int, slot_us: float,
            success_us: float, collision_us: float,
            payload_bytes: int,
            records: dict[str, dict] | None = None) -> list[dict[str, object]]:
    seen: set[tuple[str, int]] = set()
    run_ports: dict[str, set[int]] = defaultdict(set)
    run_cw: dict[str, tuple[int, int]] = {}
    for row in rows:
        run = row["run_id"].strip()
        cw = (int(row["cwmin"]), int(row["cwmax"]))
        port = int(row["port"])
        if not run or (run, port) in seen or row["access"].strip().lower() != "basic":
            raise ValueError("each run needs a unique port and confirmed Basic access")
        if run in run_cw and run_cw[run] != cw:
            raise ValueError(f"{run}: all STAs must use the same CW")
        value = float(row["receiver_mbps"])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{run}: invalid receiver_mbps")
        seen.add((run, port))
        run_ports[run].add(port)
        run_cw[run] = cw
    if not seen:
        raise ValueError("no trial rows")
    totals: dict[tuple[int, int], list[float]] = defaultdict(list)
    empirical: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for run, ports in run_ports.items():
        if len(ports) != n:
            raise ValueError(f"{run}: expected {n} distinct STA ports, got {len(ports)}")
        cw = run_cw[run]
        totals[cw].append(sum(float(row["receiver_mbps"]) for row in rows if row["run_id"].strip() == run))
        if records is not None:
            if run not in records:
                raise ValueError(f"missing run record for {run}")
            record = records[run]
            if (record["cwmin"], record["cwmax"]) != cw or record["access"]["air_verified"] != "Basic":
                raise ValueError(f"{run}: run record disagrees with trial CSV")
            if {int(s["port"]) for s in record["stations"]} != ports:
                raise ValueError(f"{run}: run record STA ports disagree with trial CSV")
            empirical[cw].append(record)
    result = []
    for cw in sorted(totals):
        measured = totals[cw]
        model = prediction(n, *cw, slot_us, success_us, collision_us, payload_bytes)
        mean = sum(measured) / len(measured)
        sd = math.sqrt(sum((x - mean) ** 2 for x in measured) / (len(measured) - 1)) if len(measured) > 1 else float("nan")
        row: dict[str, object] = {"cwmin": cw[0], "cwmax": cw[1], "runs": len(measured),
                       "receiver_mean_mbps": mean, "receiver_sd_mbps": sd,
                       **model, "relative_error_pct": 100 * (mean / model["predicted_mbps"] - 1)}
        if records is not None:
            stations = [s for record in empirical[cw] for s in record["stations"]]
            attempts = sum(int(s["attempts"]) for s in stations)
            successes = sum(int(s["successes"]) for s in stations)
            if attempts <= 0 or successes > attempts:
                raise ValueError(f"CW={cw}: invalid attempt counters")
            measured_p = successes / attempts
            hol_probability_ready = all(
                s.get("measurement_source") == "dcf_stats_hol_abi_1" for s in stations
            )
            packet_total = (
                sum(int(s.get("hol_packets", int(s["successes"]) + int(s["drops"])))
                    for s in stations) if hol_probability_ready else 0
            )
            packet_success_total = (
                sum(int(s.get("hol_packet_successes", s["successes"])) for s in stations)
                if hol_probability_ready else 0
            )
            if hol_probability_ready and (packet_total <= 0 or
                                          not 0 <= packet_success_total <= packet_total):
                raise ValueError(f"CW={cw}: invalid HoL packet success counters")
            hol_samples = [s["hol"] for s in stations]
            complete_hol = all(sample is not None for sample in hol_samples)
            hol_count = sum(int(sample["success_count"]) for sample in hol_samples) if complete_hol else 0
            row.update({
                "measured_p": measured_p,
                "p_error_pct": 100 * (measured_p / model["p"] - 1),
                "measured_hol_request_p": measured_p if hol_probability_ready else None,
                "measured_hol_final_packet_success_probability": (
                    packet_success_total / packet_total if hol_probability_ready else None
                ),
                "measured_drops": sum(int(s["drops"]) for s in stations),
                "hol_mean_us": (
                    sum(int(sample["sum_ns"]) for sample in hol_samples) / hol_count / 1000
                    if hol_count else None
                ),
                "hol_second_moment_us2": (
                    sum(int(sample["sum_square_ns2"]) for sample in hol_samples) / hol_count / 1_000_000
                    if hol_count else None
                ),
                "hol_success_samples": hol_count if complete_hol else None,
            })
        result.append(row)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trials", type=Path, help="CSV: run_id,cwmin,cwmax,port,access,receiver_mbps")
    parser.add_argument("--stations", type=int, default=4)
    parser.add_argument("--slot-us", type=float, required=True)
    parser.add_argument("--success-us", type=float, required=True)
    parser.add_argument("--collision-us", type=float, required=True)
    parser.add_argument("--payload-bytes", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records-dir", type=Path,
                        help="directory of run_id.json files from record_run.py; adds measured p and HoL columns")
    args = parser.parse_args()
    with args.trials.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    records = None
    if args.records_dir is not None:
        records = {}
        for run in {row["run_id"].strip() for row in rows}:
            path = args.records_dir / f"{run}.json"
            records[run] = json.loads(path.read_text(encoding="utf-8"))
    result = compare(rows, n=args.stations, slot_us=args.slot_us,
                     success_us=args.success_us, collision_us=args.collision_us,
                     payload_bytes=args.payload_bytes, records=records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result[0]))
        writer.writeheader()
        writer.writerows(result)
    for row in result:
        hol_p = row.get("measured_hol_request_p")
        probability_note = (
            f" HoL_request_p={hol_p:.4f} final_packet_p="
            f"{row['measured_hol_final_packet_success_probability']:.4f}"
            if hol_p is not None else ""
        )
        print(f"CW={row['cwmin']}/{row['cwmax']} measured={row['receiver_mean_mbps']:.3f} "
              f"predicted={row['predicted_mbps']:.3f} Mbps error={row['relative_error_pct']:+.1f}%"
              f"{probability_note}")


if __name__ == "__main__":
    main()
