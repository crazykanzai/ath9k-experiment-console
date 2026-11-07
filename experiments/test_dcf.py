import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dcf_compare import compare, prediction


class DcfModelTests(unittest.TestCase):
    def test_probabilities_and_one_station(self):
        one = prediction(1, 15, 63, 9, 100, 100, 1000)
        self.assertEqual(one["p"], 1.0)
        self.assertAlmostEqual(one["tau"], 2 / 17)
        four = prediction(4, 15, 63, 9, 100, 100, 1000)
        self.assertAlmostEqual(sum(four[key] for key in ("idle_probability", "success_probability", "collision_probability")), 1)
        self.assertLess(four["p"], 1)

    def test_complete_four_port_run_required(self):
        rows = [{"run_id": "r1", "cwmin": "15", "cwmax": "63", "port": str(p), "access": "Basic", "receiver_mbps": "1"} for p in range(5201, 5205)]
        result = compare(rows, n=4, slot_us=9, success_us=100, collision_us=100, payload_bytes=1000)
        self.assertEqual(result[0]["receiver_mean_mbps"], 4)
        with self.assertRaises(ValueError):
            compare(rows[:-1], n=4, slot_us=9, success_us=100, collision_us=100, payload_bytes=1000)

    def test_empirical_attempt_p_and_hol_are_kept_distinct(self):
        rows = [{"run_id": "r1", "cwmin": "15", "cwmax": "63", "port": str(p),
                 "access": "Basic", "receiver_mbps": "1"} for p in range(5201, 5205)]
        record = {"cwmin": 15, "cwmax": 63, "access": {"air_verified": "Basic"},
                  "stations": [{"port": p, "attempts": 10, "successes": 8, "drops": 1,
                                "hol": {"success_count": 2, "sum_ns": 4000,
                                        "sum_square_ns2": 10_000_000}}
                               for p in range(5201, 5205)]}
        result = compare(rows, n=4, slot_us=9, success_us=100, collision_us=100,
                         payload_bytes=1000, records={"r1": record})[0]
        self.assertAlmostEqual(result["measured_p"], 0.8)
        self.assertEqual(result["hol_mean_us"], 2)
        self.assertEqual(result["hol_second_moment_us2"], 5)
        self.assertEqual(result["measured_drops"], 4)

    def test_04_hol_request_p_is_distinct_from_final_packet_delivery(self):
        rows = [{"run_id": "r1", "cwmin": "15", "cwmax": "63", "port": str(p),
                 "access": "Basic", "receiver_mbps": "1"} for p in range(5201, 5205)]
        record = {"cwmin": 15, "cwmax": 63, "access": {"air_verified": "Basic"},
                  "stations": [{"port": p, "attempts": 10, "successes": 8, "drops": 1,
                                "measurement_source": "dcf_stats_hol_abi_1",
                                "hol_packets": 9, "hol_packet_successes": 8,
                                "hol": {"success_count": 2, "sum_ns": 4000,
                                        "sum_square_ns2": 10_000_000}}
                               for p in range(5201, 5205)]}
        result = compare(rows, n=4, slot_us=9, success_us=100, collision_us=100,
                         payload_bytes=1000, records={"r1": record})[0]
        self.assertAlmostEqual(result["measured_hol_request_p"], 0.8)
        self.assertAlmostEqual(result["measured_hol_final_packet_success_probability"], 8 / 9)
        self.assertNotEqual(result["measured_hol_request_p"],
                            result["measured_hol_final_packet_success_probability"])


if __name__ == "__main__":
    unittest.main()
