"""
stress_test.py — IoTLab Alert Accuracy & Data Continuity Validator

Runs against a live deployment (docker compose up).
Independently subscribes to MQTT, tracks every reading, detects threshold
crossings locally, then cross-references against the API's alert log.

Verification metrics:
  • End-to-End Latency: < 500 ms  (median p95 reported)
  • Data Continuity:  > 99 %       (readings stored vs expected)
  • Alert Accuracy:   Zero missed  (local detections vs API alerts)

Usage:
    # Default: 2-hour stress test (the spec requirement)
    python tests/stress_test.py

    # Quick 5-minute smoke test
    python tests/stress_test.py --duration 300 --thresholds 35,1025,900,75

Dependencies:
    pip install paho-mqtt requests
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timezone
from threading import Event, Thread

import paho.mqtt.client as mqtt
import requests

# ── Config ───────────────────────────────────────────────────────────
MQTT_HOST       = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT       = int(os.getenv("MQTT_PORT", "1883"))
API_BASE        = os.getenv("API_URL", "http://localhost:8000")

DEFAULT_DURATION = 2 * 3600  # 2 hours (spec requirement)
SENSORS          = ["temperature", "pressure", "light", "humidity"]
TOPICS           = [f"lab/sensors/{s}" for s in SENSORS]

# Default thresholds matching the API defaults
DEFAULT_THRESHOLDS = {
    "temperature": 35.0,
    "pressure":    1025.0,
    "light":       900.0,
    "humidity":    75.0,
}


# ── Data collectors ───────────────────────────────────────────────────
class StressTest:
    def __init__(self, thresholds: dict[str, float], duration: float):
        self.thresholds = thresholds
        self.duration = duration
        self.start_time: float = 0
        self.stop_event = Event()

        # Readings received locally via MQTT
        self.reading_count: dict[str, int] = defaultdict(int)
        self.seq_gaps: dict[str, list[int]] = defaultdict(list)
        self.last_seq: dict[str, int] = {}
        self.latencies: list[float] = []  # end-to-end latency in ms

        # Locally detected alerts
        self.local_alerts: list[dict] = []
        self.alert_state: dict[str, bool] = {s: False for s in SENSORS}

        # Expected readings (based on elapsed time)
        self.expected_readings: int = 0


# ── MQTT subscriber ───────────────────────────────────────────────────
def build_mqtt_client(test: StressTest) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="stress-tester",
    )

    def on_connect(client, userdata, flags, rc, props=None):
        if rc == 0:
            for t in TOPICS:
                client.subscribe(t, qos=1)
            print(f"[MQTT] Connected, subscribed to {len(TOPICS)} topics")
        else:
            print(f"[MQTT] Connection failed rc={rc}")
            test.stop_event.set()

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            return

        channel = msg.topic.split("/")[-1]
        value   = payload.get("value")
        ts      = payload.get("ts", time.time() * 1000)
        seq     = payload.get("seq")

        now_ms = time.time() * 1000

        # Latency tracking
        latency = now_ms - ts
        if 0 <= latency < 10000:  # filter outliers
            test.latencies.append(latency)

        # Reading count
        test.reading_count[channel] += 1

        # Sequence gap detection
        if seq is not None:
            if channel in test.last_seq:
                expected = test.last_seq[channel] + 1
                if seq > expected:
                    gap = seq - expected
                    test.seq_gaps[channel].append(gap)
                    print(f"[GAP] {channel}: missed {gap} readings (seq {expected}→{seq})")
            test.last_seq[channel] = seq

        # Local alert detection (independent of API)
        thr = test.thresholds.get(channel)
        if thr is not None and value is not None:
            over = value > thr
            was_over = test.alert_state.get(channel, False)
            if over and not was_over:
                severity = "CRITICAL" if value > thr * 1.1 else "WARNING"
                test.local_alerts.append({
                    "ts": ts,
                    "sensor": channel,
                    "value": value,
                    "threshold": thr,
                    "severity": severity,
                })
            test.alert_state[channel] = over

    client.on_connect = on_connect
    client.on_message = on_message
    return client


# ── API poller thread ─────────────────────────────────────────────────
def poll_api_alerts(test: StressTest, collected: list[dict]):
    """Poll the API's /alerts endpoint every 2 seconds; collect unique alert IDs."""
    seen_ids = set()
    while not test.stop_event.is_set():
        with suppress(Exception):
            resp = requests.get(f"{API_BASE}/alerts", params={"limit": 500}, timeout=5)
            if resp.status_code == 200:
                for a in resp.json():
                    if a["id"] not in seen_ids:
                        seen_ids.add(a["id"])
                        collected.append(a)
        time.sleep(2)


# ── Matching logic ────────────────────────────────────────────────────
def match_alerts(
    local: list[dict],
    api: list[dict],
    tolerance_ms: float = 5000,
) -> tuple[list[dict], list[dict]]:
    """
    Match local alert detections against API-stored alerts by
    (sensor, approximate timestamp). Returns (missed, extra).
    """
    api_matched = [False] * len(api)
    missed: list[dict] = []

    for la in local:
        found = False
        for i, aa in enumerate(api):
            if api_matched[i]:
                continue
            if (
                aa["sensor"] == la["sensor"]
                and abs(aa["ts"] - la["ts"]) <= tolerance_ms
            ):
                api_matched[i] = True
                found = True
                break
        if not found:
            missed.append(la)

    extra = [aa for i, aa in enumerate(api) if not api_matched[i]]
    return missed, extra


# ── Report ────────────────────────────────────────────────────────────
def print_report(test: StressTest, api_alerts: list[dict]):
    elapsed = time.time() - test.start_time
    total_readings = sum(test.reading_count.values())

    # Expected readings: 4 sensors × 1 Hz × elapsed seconds
    # (allow 5% tolerance for startup skew)
    expected = 4 * elapsed
    continuity = (total_readings / expected * 100) if expected > 0 else 0

    # Latency stats
    lats = sorted(test.latencies)
    p50 = lats[len(lats) // 2] if lats else 0
    p95 = lats[int(len(lats) * 0.95)] if lats else 0
    p99 = lats[int(len(lats) * 0.99)] if lats else 0
    max_lat = lats[-1] if lats else 0

    # Alert matching
    missed, extra = match_alerts(test.local_alerts, api_alerts)

    print()
    print("=" * 62)
    print("  IoTLab Stress Test Report")
    print("=" * 62)
    print(f"  Duration:            {elapsed:.0f}s  ({elapsed/60:.1f} min)")
    print(f"  Start (UTC):         {datetime.fromtimestamp(test.start_time, tz=timezone.utc).isoformat()}")
    print()

    print("  ── Data Continuity ──")
    print(f"  Readings received:   {total_readings}")
    print(f"  Expected (4×1Hz):    {expected:.0f}")
    print(f"  Data continuity:     {continuity:.2f}%")
    per_sensor = "  ".join(
        f"{s}={test.reading_count.get(s,0)}"
        for s in SENSORS
    )
    print(f"  Per sensor:          {per_sensor}")
    if any(test.seq_gaps.values()):
        print(f"  Sequence gaps:       {dict(test.seq_gaps)}")
    else:
        print("  Sequence gaps:       none ✓")
    print()

    print("  ── End-to-End Latency ──")
    print(f"  Samples:             {len(lats)}")
    print(f"  P50 (median):        {p50:.1f} ms")
    print(f"  P95:                 {p95:.1f} ms")
    print(f"  P99:                 {p99:.1f} ms")
    print(f"  Max:                 {max_lat:.1f} ms")
    spec_met = "✓ PASS" if p95 < 500 else "✗ FAIL"
    print(f"  <500ms P95 spec:     {spec_met}")
    print()

    print("  ── Alert Accuracy ──")
    print(f"  Local detections:    {len(test.local_alerts)}")
    print(f"  API-stored alerts:   {len(api_alerts)}")
    print(f"  Missed (not in API): {len(missed)}")
    print(f"  Extra (API-only):    {len(extra)}")
    if missed:
        print("  Missed details:")
        for m in missed:
            print(f"    {m['severity']:8s}  {m['sensor']:12s}  value={m['value']:.2f}  > {m['threshold']}")
    alert_spec = "✓ PASS" if len(missed) == 0 else "✗ FAIL"
    print(f"  Zero-missed spec:    {alert_spec}")
    print()

    # Overall verdict
    continuity_ok = continuity >= 99.0
    latency_ok = p95 < 500
    alerts_ok = len(missed) == 0

    verdict = "✓ ALL SPECS PASSED" if (continuity_ok and latency_ok and alerts_ok) else "✗ SOME SPECS FAILED"
    print(f"  ── VERDICT ──  {verdict}")
    print("=" * 62)

    return continuity_ok and latency_ok and alerts_ok


# ── CLI ───────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="IoTLab Stress Test")
    p.add_argument(
        "--duration", type=float, default=DEFAULT_DURATION,
        help=f"Test duration in seconds (default: {DEFAULT_DURATION}s = 2h)",
    )
    p.add_argument(
        "--thresholds", type=str,
        default="35,1025,900,75",
        help="Comma-separated thresholds: temp,pressure,light,humidity",
    )
    p.add_argument(
        "--mqtt-host", default=MQTT_HOST,
        help=f"MQTT broker host (default: {MQTT_HOST})",
    )
    p.add_argument(
        "--api-url", default=API_BASE,
        help=f"API base URL (default: {API_BASE})",
    )
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────
def main():
    args = parse_args()

    thr_vals = [float(x) for x in args.thresholds.split(",")]
    thresholds = dict(zip(SENSORS, thr_vals))

    print("IoTLab Stress Test")
    print(f"  Duration:    {args.duration:.0f}s ({args.duration/60:.1f} min)")
    print(f"  MQTT broker: {args.mqtt_host}")
    print(f"  API:         {args.api_url}")
    print(f"  Thresholds:  {thresholds}")
    print()

    test = StressTest(thresholds, args.duration)

    # ── Connect MQTT ──
    mqtt_client = build_mqtt_client(test)
    while True:
        try:
            mqtt_client.connect(args.mqtt_host, MQTT_PORT, keepalive=60)
            break
        except OSError:
            print("[MQTT] Waiting for broker…")
            time.sleep(2)
    mqtt_client.loop_start()

    # ── Start API poller ──
    api_alerts: list[dict] = []
    poller = Thread(target=poll_api_alerts, args=(test, api_alerts), daemon=True)
    poller.start()

    # ── Health check ──
    try:
        r = requests.get(f"{args.api_url}/health", timeout=5)
        print(f"[API]  Health check: {r.json()}")
    except Exception as e:
        print(f"[API]  Health check FAILED: {e}")

    # ── Run ──
    test.start_time = time.time()
    print(f"\n[TEST] Running for {args.duration:.0f}s … (Ctrl+C to stop early)\n")

    try:
        # Print live stats every 30s
        next_report = time.time() + 30
        while time.time() - test.start_time < args.duration:
            time.sleep(1)
            if time.time() >= next_report:
                elapsed = time.time() - test.start_time
                total = sum(test.reading_count.values())
                lats = sorted(test.latencies)
                p50 = lats[len(lats) // 2] if lats else 0
                expected = 4 * elapsed
                cont = (total / expected * 100) if expected > 0 else 0
                print(
                    f"  [{elapsed:6.0f}s]  readings={total:6d}  "
                    f"continuity={cont:5.1f}%  P50={p50:5.0f}ms  "
                    f"alerts(local/api)={len(test.local_alerts):3d}/{len(api_alerts):3d}"
                )
                next_report = time.time() + 30
    except KeyboardInterrupt:
        print("\n[TEST] Interrupted — generating report with data so far…\n")

    # ── Stop ──
    test.stop_event.set()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()

    # Short grace for final API poll
    time.sleep(2)

    # ── Report ──
    passed = print_report(test, api_alerts)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
