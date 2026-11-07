import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dcf_stats import FIELDS, HOL_FIELDS, delta, main, parse_dcf_stats, parse_xmit, summarize_dcf_stats


class DcfStatsTests(unittest.TestCase):
    def test_rts_abi_and_terminal_failure_gate(self):
        raw = self.hol_raw().replace('mode=basic', 'mode=rts-cts')
        self.assertIsNone(summarize_dcf_stats(parse_dcf_stats(raw,'aa:bb:cc:dd:ee:ff'))['attempt_success_probability'])
        raw = raw.replace('hol_abi=1', 'hol_abi=1 basic_retry_abi=2 rts_retry_abi=2')
        result = summarize_dcf_stats(parse_dcf_stats(raw,'aa:bb:cc:dd:ee:ff'))
        self.assertFalse(result['attempt_probability_valid'])
        raw = raw.replace('packet_failures=1','packet_failures=0').replace('packet_successes=4','packet_successes=5')
        result = summarize_dcf_stats(parse_dcf_stats(raw,'aa:bb:cc:dd:ee:ff'))
        self.assertFalse(result['attempt_probability_valid'])
        self.assertIsNone(result['attempt_success_probability'])
        self.assertAlmostEqual(result['diagnostic_uncalibrated_attempt_probability'],0.4)
    def test_retry_abi_metadata_and_corrected_attempts_are_preserved(self):
        raw=self.hol_raw().replace('hol_abi=1','hol_abi=1 basic_retry_abi=2 rts_retry_abi=1')
        raw=raw.replace('exchange_attempts=10','exchange_attempts=13').replace('data_attempts=10','data_attempts=13')
        result=summarize_dcf_stats(parse_dcf_stats(raw,'aa:bb:cc:dd:ee:ff'))
        self.assertEqual(result['basic_retry_abi'],2)
        self.assertEqual(result['rts_retry_abi'],1)
        self.assertEqual(result['counters']['long_retries'],6)
        self.assertIsNone(result['attempt_success_probability'])
        self.assertAlmostEqual(result['diagnostic_uncalibrated_attempt_probability'],4/13)
        self.assertFalse(result['attempt_probability_valid'])
        self.assertEqual(result['hol_mean_us'],30)

    def test_basic_abi2_acked_only_window_remains_available(self):
        raw=self.hol_raw().replace('hol_abi=1','hol_abi=1 basic_retry_abi=2')
        raw=raw.replace('packets=5','packets=4').replace('packet_failures=1','packet_failures=0')
        result=summarize_dcf_stats(parse_dcf_stats(raw,'aa:bb:cc:dd:ee:ff'))
        self.assertTrue(result['attempt_probability_valid'])
        self.assertAlmostEqual(result['attempt_success_probability'],.4)

    @staticmethod
    def hol_raw():
        hol = {field: 0 for field in HOL_FIELDS}
        hol.update({"hol_service_samples": 3, "hol_service_sum_us": 90,
                    "hol_service_sum_sq_us2": 2900,
                    "hol_service_drop_breaks": 1})
        fields = {"packets": 5, "packet_successes": 4, "packet_failures": 1,
                  "exchange_attempts": 10, "exchange_successes": 4,
                  "rts_attempts": 0, "rts_successes": 0,
                  "data_attempts": 10, "data_successes": 4,
                  "short_retries": 0, "long_retries": 6, **hol}
        return ("version=2 running=0 generation=3 elapsed_us=1000000 capture=off hol_abi=1\n"
                "AC=BE ready=1 mode=basic aifsn=2 cwmin=15 cwmax=63\n"
                "peer=AA:BB:CC:DD:EE:FF AC=BE " +
                " ".join(f"{key}={value}" for key, value in fields.items()) + "\n")

    def test_04_peer_hol_is_separate_from_attempt_success(self):
        raw = self.hol_raw()
        snapshot = parse_dcf_stats(raw, "aa:bb:cc:dd:ee:ff")
        result = summarize_dcf_stats(snapshot)
        self.assertIn('omits',result['hol_probability_caveat'])
        self.assertEqual(result["hol_mean_us"], 30)
        self.assertAlmostEqual(result["hol_second_moment_us2"], 2900 / 3)
        self.assertEqual(result["attempt_success_probability"], 0.4)
        self.assertEqual(result["packet_success_probability"], 0.8)
        self.assertEqual(result["hol_request_success_probability"], 0.4)
        self.assertEqual(result["hol_final_packet_success_probability"], 0.8)
        self.assertNotEqual(result["hol_request_success_probability"],
                            result["hol_final_packet_success_probability"])
        self.assertEqual(result["hol"]["hol_service_drop_breaks"], 1)
        with self.assertRaisesRegex(ValueError, "hol_abi=1"):
            parse_dcf_stats(raw.replace("hol_abi=1", "hol_abi=0"), "aa:bb:cc:dd:ee:ff")

    def test_hol_snapshot_json_saves_both_probability_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "dcf_stats"
            output = Path(tmp) / "sta_hol.json"
            raw.write_text(self.hol_raw(), encoding="utf-8")
            with patch.object(sys, "argv", ["dcf_stats.py", "hol-snapshot", "--dcf-stats",
                                            str(raw), "--peer", "aa:bb:cc:dd:ee:ff",
                                            "--output", str(output)]):
                main()
            snapshot = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["hol_request_success_probability"], 0.4)
            self.assertEqual(snapshot["hol_final_packet_success_probability"], 0.8)
            self.assertEqual(snapshot["counters"]["exchange_attempts"], 10)

    def test_parse_and_delta(self):
        values_by_field = {"dcf_attempts": 15, "dcf_successes": 10,
                           "dcf_drops": 1, "dcf_timed_successes": 10,
                           "dcf_success_delay_ns": 5000000}
        raw = "\n".join(f"{name} {values_by_field[name]} 0 0 0" for name in FIELDS)
        counters = parse_xmit(raw)
        before = {"counters": {"BE": {name: 0 for name in counters["BE"]}}}
        after = {"counters": counters}
        values = delta(before, after)
        self.assertAlmostEqual(values["conditional_success_probability"], 10 / 15)
        self.assertAlmostEqual(values["packet_final_success_probability"], 10 / 11)
        self.assertEqual(values["mean_success_delay_us"], 500)


if __name__ == "__main__":
    unittest.main()
