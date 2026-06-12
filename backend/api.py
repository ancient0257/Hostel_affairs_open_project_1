"""
api.py — FastAPI backend

Responsibilities:
  • Subscribe to MQTT and persist every reading to SQLite
  • REST endpoints consumed by the React dashboard
  • Alert detection with configurable thresholds
  • CSV export of any time window
"""

import asyncio
import csv
import io
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import paho.mqtt.client as mqtt
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

# ── Config ───────────────────────────────────────────────────────────
MQTT_HOST   = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))
DB_PATH     = os.getenv("DB_PATH", "./lab.db")
NODE_ID     = os.getenv("NODE_ID", "pi-lab-01")

# InfluxDB (optional — graceful skip if not configured)
INFLUXDB_URL    = os.getenv("INFLUXDB_URL", "")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN", "")
INFLUXDB_ORG    = os.getenv("INFLUXDB_ORG", "iotlab-org")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "sensor-data")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [api] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Default alert thresholds ─────────────────────────────────────────
DEFAULT_THRESHOLDS = {
    "temperature": 35.0,
    "pressure":    1025.0,
    "light":       900.0,
    "humidity":    75.0,
}
thresholds: dict[str, float] = dict(DEFAULT_THRESHOLDS)

# Shared DB connection (set during startup)
db_conn: Optional[aiosqlite.Connection] = None

# InfluxDB async client (set during startup if configured)
influx_client: Optional[InfluxDBClientAsync] = None
influx_write_api = None

# In-memory queue for MQTT → async bridge
mqtt_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)


# ── Database setup ───────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,        -- Unix epoch milliseconds
    sensor    TEXT    NOT NULL,        -- temperature | pressure | light | humidity
    value     REAL    NOT NULL,
    unit      TEXT    NOT NULL,
    node_id   TEXT    NOT NULL DEFAULT 'pi-lab-01'
);

CREATE INDEX IF NOT EXISTS idx_readings_ts_sensor
    ON readings (ts, sensor);

CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    sensor    TEXT    NOT NULL,
    value     REAL    NOT NULL,
    threshold REAL    NOT NULL,
    severity  TEXT    NOT NULL         -- WARNING | CRITICAL
);

CREATE TABLE IF NOT EXISTS thresholds_cfg (
    sensor    TEXT PRIMARY KEY,
    value     REAL NOT NULL
);
"""


async def init_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    await conn.commit()
    # Load saved thresholds
    async with conn.execute("SELECT sensor, value FROM thresholds_cfg") as cur:
        async for row in cur:
            thresholds[row["sensor"]] = row["value"]
    log.info("Database ready at %s", DB_PATH)
    return conn


# ── MQTT subscriber (runs in thread, bridges to asyncio queue) ────────
def make_mqtt_client(loop: asyncio.AbstractEventLoop) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{NODE_ID}-api-subscriber",
    )

    def on_connect(client, userdata, flags, rc, props=None):
        if rc == 0:
            client.subscribe("lab/sensors/#", qos=1)
            log.info("MQTT subscriber connected, subscribed to lab/sensors/#")
        else:
            log.error("MQTT subscriber connection failed rc=%d", rc)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            channel = msg.topic.split("/")[-1]
            payload["channel"] = channel
            asyncio.run_coroutine_threadsafe(
                mqtt_queue.put(payload), loop
            )
        except Exception as e:
            log.warning("Bad MQTT message: %s", e)

    client.on_connect = on_connect
    client.on_message = on_message
    return client


# ── Message processor (runs as async task) ────────────────────────────
async def process_messages():
    global db_conn
    alert_state: dict[str, bool] = {}

    while True:
        payload = await mqtt_queue.get()
        channel = payload.get("channel")
        value   = payload.get("value")
        ts      = payload.get("ts", time.time() * 1000)
        unit    = payload.get("unit", "")

        if None in (channel, value):
            continue

        # Persist reading
        await db_conn.execute(
            "INSERT INTO readings (ts, sensor, value, unit, node_id) VALUES (?,?,?,?,?)",
            (ts, channel, value, unit, payload.get("node", NODE_ID)),
        )

        # Persist to InfluxDB (non-blocking, fire-and-forget)
        if influx_write_api is not None:
            with suppress(Exception):
                point = (
                    Point("sensor_reading")
                    .tag("sensor", channel)
                    .tag("node", payload.get("node", NODE_ID))
                    .field("value", float(value))
                    .time(int(ts * 1_000_000))  # nanoseconds
                )
                await influx_write_api.write(
                    bucket=INFLUXDB_BUCKET,
                    org=INFLUXDB_ORG,
                    record=point,
                )

        # Alert detection
        thr = thresholds.get(channel)
        if thr is not None:
            over = value > thr
            was_over = alert_state.get(channel, False)

            if over and not was_over:
                severity = "CRITICAL" if value > thr * 1.1 else "WARNING"
                await db_conn.execute(
                    "INSERT INTO alerts (ts, sensor, value, threshold, severity) VALUES (?,?,?,?,?)",
                    (ts, channel, value, thr, severity),
                )
                log.warning("ALERT %s: %s=%.2f > %.2f", severity, channel, value, thr)

            alert_state[channel] = over

        await db_conn.commit()


# ── Lifespan (startup / shutdown) ────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_conn, influx_client, influx_write_api
    loop = asyncio.get_running_loop()

    db_conn = await init_db()

    # Initialize InfluxDB client if configured
    if INFLUXDB_URL:
        try:
            influx_client = InfluxDBClientAsync(
                url=INFLUXDB_URL,
                token=INFLUXDB_TOKEN,
                org=INFLUXDB_ORG,
            )
            influx_write_api = influx_client.write_api()
            log.info("InfluxDB connected at %s", INFLUXDB_URL)
        except Exception as e:
            log.warning("InfluxDB unavailable — skipping: %s", e)
            influx_client = None
            influx_write_api = None

    mqtt_client = make_mqtt_client(loop)
    while True:
        try:
            mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            break
        except OSError:
            log.warning("Waiting for broker…")
            await asyncio.sleep(2)
    mqtt_client.loop_start()

    task = asyncio.create_task(process_messages())

    yield  # app runs here

    task.cancel()
    mqtt_client.loop_stop()
    if influx_client:
        await influx_client.close()
    await db_conn.close()


# ── App ───────────────────────────────────────────────────────────────
app = FastAPI(title="IoTLab API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Endpoints ─────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "ts": time.time()}


@app.get("/readings")
async def get_readings(
    sensor:  Optional[str] = None,
    since:   Optional[float] = Query(None, description="Unix ms"),
    until:   Optional[float] = Query(None, description="Unix ms"),
    limit:   int = Query(3600, le=86400),
):
    """Return time-series readings, newest first."""
    now_ms = time.time() * 1000
    since  = since or (now_ms - 24 * 3600 * 1000)   # default: last 24 h
    until  = until or now_ms

    clauses = ["ts BETWEEN ? AND ?"]
    params  = [since, until]

    if sensor:
        clauses.append("sensor = ?")
        params.append(sensor)

    where = " AND ".join(clauses)
    params.append(limit)

    async with db_conn.execute(
        f"SELECT ts, sensor, value, unit, node_id FROM readings "
        f"WHERE {where} ORDER BY ts DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()

    return [dict(r) for r in rows]


@app.get("/readings/latest")
async def get_latest():
    """Most recent reading per sensor."""
    async with db_conn.execute(
        """SELECT r.ts, r.sensor, r.value, r.unit
           FROM readings r
           INNER JOIN (
               SELECT sensor, MAX(ts) AS max_ts FROM readings GROUP BY sensor
           ) m ON r.sensor = m.sensor AND r.ts = m.max_ts"""
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/alerts")
async def get_alerts(limit: int = Query(50, le=500)):
    async with db_conn.execute(
        "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/thresholds")
async def get_thresholds():
    return thresholds


@app.put("/thresholds/{sensor}")
async def set_threshold(sensor: str, value: float):
    if sensor not in DEFAULT_THRESHOLDS:
        return {"error": f"Unknown sensor '{sensor}'"}
    thresholds[sensor] = value
    await db_conn.execute(
        "INSERT OR REPLACE INTO thresholds_cfg (sensor, value) VALUES (?,?)",
        (sensor, value),
    )
    await db_conn.commit()
    log.info("Threshold updated: %s = %.2f", sensor, value)
    return {"sensor": sensor, "threshold": value}


@app.get("/export")
async def export_csv(
    sensor: Optional[str] = None,
    since:  Optional[float] = Query(None, description="Unix ms"),
    until:  Optional[float] = Query(None, description="Unix ms"),
):
    """Download all readings in the given window as CSV."""
    now_ms = time.time() * 1000
    since  = since or (now_ms - 24 * 3600 * 1000)
    until  = until or now_ms

    clauses = ["ts BETWEEN ? AND ?"]
    params  = [since, until]
    if sensor:
        clauses.append("sensor = ?")
        params.append(sensor)

    async with db_conn.execute(
        f"SELECT ts, sensor, value, unit, node_id FROM readings "
        f"WHERE {' AND '.join(clauses)} ORDER BY ts ASC",
        params,
    ) as cur:
        rows = await cur.fetchall()

    def iso(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["timestamp_utc", "sensor", "value", "unit", "node_id"])
    for r in rows:
        w.writerow([iso(r["ts"]), r["sensor"], r["value"], r["unit"], r["node_id"]])

    buf.seek(0)
    filename = f"lab_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/stats")
async def get_stats():
    """Summary statistics per sensor over the last 24 h."""
    now_ms = time.time() * 1000
    since  = now_ms - 24 * 3600 * 1000

    async with db_conn.execute(
        """SELECT sensor,
                  COUNT(*)      AS count,
                  MIN(value)    AS min,
                  MAX(value)    AS max,
                  AVG(value)    AS avg,
                  MIN(ts)       AS first_ts,
                  MAX(ts)       AS last_ts
           FROM readings
           WHERE ts >= ?
           GROUP BY sensor""",
        (since,),
    ) as cur:
        rows = await cur.fetchall()

    return [dict(r) for r in rows]
