#!/usr/bin/python3
import glob
import json
import os
import subprocess
import tempfile
import time
import unittest

import docker
import numpy as np
import requests

from gamutrf.birdseye_rssi import BirdsEyeRSSI


class FakeArgs:
    def __init__(self, filename):
        self.sdr = "file:" + filename
        self.threshold = -100
        self.mean_window = 4096
        self.rssi_threshold = -100
        self.gain = 10


class BirdseyeRSSITestCase(unittest.TestCase):
    def test_birdseye_smoke(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "gamutrf_recording1_1000Hz_1000sps.raw")
            subprocess.check_call(
                ["dd", "if=/dev/zero", "of=" + filename, "bs=1M", "count=1"]
            )
            tb = BirdsEyeRSSI(FakeArgs(filename), 1e3, 1e3)
            tb.start()
            time.sleep(10)
            tb.stop()
            tb.wait()

    def verify_birdseye_stream(self, gamutdir, freq):
        sample_rate = 1000000
        duration = 10
        sample_count = duration * sample_rate
        for _ in range(15):
            try:
                response = requests.get(
                    f"http://localhost:8000/v1/rssi/{int(freq)}/{sample_count}/{sample_rate}"
                )
                self.assertEqual(200, response.status_code, response)
                break
            except requests.exceptions.ConnectionError:
                time.sleep(1)
        mqtt_logs = None
        line_json = None
        for _ in range(duration):
            self.assertTrue(os.path.exists(gamutdir))
            mqtt_logs = glob.glob(os.path.join(gamutdir, "mqtt-rssi-*log"))
            for log_name in mqtt_logs:
                with open(log_name, "r") as log:
                    for line in log.readlines():
                        line_json = json.loads(line)
                        self.assertGreater(-10, line_json["rssi"])
                        if line_json["center_freq"] == freq:
                            self.assertEqual(freq, line_json["center_freq"], line_json)
                            return
            time.sleep(1)

    # TODO(josh): disable to move to ubuntu 24, then re-enable.
    def disabled_test_birdseye_endtoend_rssi(self):
        test_tag = "iqtlabs/gamutrf:latest"
        with tempfile.TemporaryDirectory() as tempdir:
            testraw = os.path.join(tempdir, "gamutrf_recording1_1000Hz_1000sps.raw")
            gamutdir = os.path.join(tempdir, "gamutrf")
            testdata = (
                np.random.random(int(1e6)).astype(np.float32)
                + np.random.random(int(1e6)).astype(np.float32) * 1j
            )
            with open(testraw, "wb") as testrawfile:
                testdata.tofile(testrawfile)
            os.mkdir(gamutdir)
            client = docker.from_env()
            client.images.build(dockerfile="Dockerfile", path=".", tag=test_tag)
            container = client.containers.run(
                test_tag,
                command=[
                    "gamutrf-worker",
                    "--rssi_threshold=-100",
                    f"--sdr=file:{testraw}",
                ],
                ports={"8000/tcp": 8000},
                volumes={tempdir: {"bind": "/data", "mode": "rw"}},
                detach=True,
            )
            self.verify_birdseye_stream(gamutdir, 10e6)
            self.verify_birdseye_stream(gamutdir, 99e6)
            try:
                container.kill()
            except requests.exceptions.HTTPError as err:
                print(str(err))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
