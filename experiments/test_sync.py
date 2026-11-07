import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path


def load_script(name):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analysis = load_script("analyze_sync.py")
runner = load_script("sync_run.py")
calibration = load_script("reference_helpers.py")


class AccessModeTests(unittest.TestCase):
    def signature(self, rts, override):
        edca = ("AC=BE aifs=3 cwmin=15 cwmax=1023 hw_aifs=3 "
                "hw_cwmin=15 hw_cwmax=1023 status_name=verified")
        return runner.current_signature({
            "edca": {"stdout": edca},
            "rts": {"stdout": "Bit Rate=24 Mb/s RTS thr:" + rts},
            "wireless": {"stdout": "channel 1 (2412 MHz)"},
            "basic_access_override": {"stdout": str(override)},
            "rate_mask_2ghz": {"stdout": "0x100"},
        })

    def test_rts_off_alone_is_not_basic(self):
        self.assertEqual(self.signature("off", 0)["mode"], "unverified")

    def test_explicit_override_is_basic(self):
        self.assertEqual(self.signature("off", 1)["mode"], "basic")

    def test_threshold_with_override_off_is_rts(self):
        self.assertEqual(self.signature("500", 0)["mode"], "rts-cts")

    def test_override_requires_threshold_off(self):
        with self.assertRaises(RuntimeError):
            self.signature("500", 1)


class AbsoluteWindowTests(unittest.TestCase):
    def test_iperf_intervals_are_clipped_by_wall_clock_overlap(self):
        iperf = {"intervals": [{"sum": {"start": 0, "end": 1, "bytes": 1_000_000}},
                               {"sum": {"start": 1, "end": 2, "bytes": 1_000_000}}]}
        records, buckets = analysis.clip_intervals(iperf, 500_000_000,
                                                   1_000_000_000, 2_000_000_000, 0)
        self.assertEqual(len(records), 2)
        self.assertAlmostEqual(buckets[0], 1_000_000)

    def test_ap_pcap_timestamps_and_udp_payload(self):
        ethernet = b"\0"*12 + b"\x08\x00"
        ip = bytearray(20)
        ip[0] = 0x45
        ip[9] = 17
        udp = struct.pack("!HHHH", 40000, 5201, 1208, 0)
        frame = ethernet + ip + udp + b"\0"*32
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.pcap"
            path.write_bytes(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 128, 1) +
                        struct.pack("<IIII", 100, 123456, len(frame), len(frame)) + frame)
            self.assertEqual(analysis.pcap_udp(path, {5201})[5201],
                             [(100_123_456_000, 1200)])

    def test_air_monitor_audit_separates_each_sta_and_foreign_bss(self):
        header = ("frame.time_epoch\twlan.fc.type_subtype\twlan.ta\twlan.ra\t"
                  "wlan.bssid\tradiotap.datarate\tradiotap.dbm_antsignal\n")
        rows = [
            "1.0\t40\t00:11:22:33:44:01\taa:bb:cc:dd:ee:ff\taa:bb:cc:dd:ee:ff\t24\t-30\n",
            "1.1\t27\t00:11:22:33:44:01\taa:bb:cc:dd:ee:ff\t\t24\t-30\n",
            "1.2\t28\taa:bb:cc:dd:ee:ff\t00:11:22:33:44:01\t\t24\t-30\n",
            "1.3\t8\t66:55:44:33:22:11\tff:ff:ff:ff:ff:ff\t66:55:44:33:22:11\t1\t-70\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "air.tsv"
            path.write_text(header + "".join(rows), encoding="utf-8")
            value = analysis.air_monitor_audit(
                path, {"STA1": "00:11:22:33:44:01"}, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(value["stations"]["STA1"]["qos_data"], 1)
        self.assertEqual(value["stations"]["STA1"]["rts"], 1)
        self.assertEqual(value["stations"]["STA1"]["cts"], 1)
        self.assertEqual(value["stations"]["STA1"]["qos_rates_mbps"], {"24": 1})
        self.assertEqual(value["foreign_beacon_bssids"], ["66:55:44:33:22:11"])

    def test_tcpdump_monitor_drop_statistics(self):
        text = ("15031 packets captured\n15315 packets received by filter\n"
                "0 packets dropped by kernel\n49 packets dropped by interface\n")
        value = analysis.tcpdump_stats(text)
        self.assertEqual(value["kernel_dropped"], 0)
        self.assertEqual(value["interface_dropped"], 49)
        self.assertAlmostEqual(value["interface_drop_fraction"], 49 / 15315)

    def test_tcpdump_singular_and_optional_zero_interface_drops(self):
        text = "100 packets captured\n101 packets received by filter\n0 packets dropped by kernel\n"
        self.assertEqual(analysis.tcpdump_stats(text)["interface_dropped"], 0)
        self.assertEqual(analysis.tcpdump_stats(text + "1 packet dropped by interface\n")["interface_dropped"], 1)
        self.assertIsNone(analysis.tcpdump_stats("tcpdump: listening\n")["interface_dropped"])

    def test_active_survey_and_idle_delta(self):
        before = calibration.active_survey(
            "Survey data from wlan0\n\tfrequency: 2412 MHz [in use]\n"
            "\tchannel active time: 1000 ms\n\tchannel busy time: 100 ms\n"
            "\tchannel receive time: 30 ms\n\tchannel transmit time: 20 ms\n")
        after = calibration.active_survey(
            "Survey data from wlan0\n\tfrequency: 2412 MHz [in use]\n"
            "\tchannel active time: 11000 ms\n\tchannel busy time: 600 ms\n"
            "\tchannel receive time: 200 ms\n\tchannel transmit time: 80 ms\n")
        value = calibration.survey_delta(before, after)
        self.assertEqual(value["active_ms"], 10000)
        self.assertEqual(value["busy_ms"], 500)
        self.assertAlmostEqual(value["busy_fraction"], 0.05)


if __name__ == "__main__":
    unittest.main()
