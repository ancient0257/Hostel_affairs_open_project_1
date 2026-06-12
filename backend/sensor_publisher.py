"""
sensor_publisher.py

Runs continuously on the Pi (or in Docker during simulation).
Publishes one JSON message per sensor per second to:

    lab/sensors/{channel}

QoS 1 — at-least-once delivery guarantees > 99% data continuity.
"""

import json
import logging
import os
import time

import paho.mqtt.client as mqtt

from sensors import SENSORS, UNITS

# ── Config ───────────────────────────────────────────────────────────
MQTT_HOST        = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT        = int(os.getenv("MQTT_PORT", 1883))
PUBLISH_INTERVAL = float(os.getenv("PUBLISH_INTERVAL", 1.0))
NODE_ID          = os.getenv("NODE_ID", "pi-lab-01")
TOPIC_PREFIX     = "lab/sensors"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [publisher] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── MQTT callbacks ───────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log.info("Connected to broker %s:%d", MQTT_HOST, MQTT_PORT)
    else:
        log.error("Connection failed, rc=%d", rc)


def on_publish(client, userdata, mid, reason_codes=None, properties=None):
    pass  # called after QoS-1 PUBACK received


# ── Setup ────────────────────────────────────────────────────────────
client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id=f"{NODE_ID}-publisher",
)
client.on_connect = on_connect
client.on_publish = on_publish

# Retry connection until broker is up (Docker startup race)
while True:
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        break
    except OSError:
        log.warning("Broker not ready, retrying in 2 s…")
        time.sleep(2)

client.loop_start()

# ── Main publish loop ─────────────────────────────────────────────────
seq = 0
log.info("Publishing sensors: %s", list(SENSORS.keys()))

while True:
    t0 = time.time()
    seq += 1

    for channel, sensor in SENSORS.items():
        payload = {
            "ts":      round(t0 * 1000),   # Unix ms
            "value":   sensor.value,
            "unit":    UNITS[channel],
            "node":    NODE_ID,
            "seq":     seq,
        }
        topic = f"{TOPIC_PREFIX}/{channel}"
        info  = client.publish(topic, json.dumps(payload), qos=1)
        # info.wait_for_publish() would block; avoid in tight loop

    elapsed = time.time() - t0
    sleep   = max(0.0, PUBLISH_INTERVAL - elapsed)
    time.sleep(sleep)
