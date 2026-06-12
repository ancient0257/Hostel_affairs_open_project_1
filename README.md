# IoTLab · Real-Time Sensor Dashboard

End-to-end IoT pipeline with MQTT, Python, FastAPI, SQLite, and React.  
Runs fully in simulation — swap one import to plug in real hardware.

---

## Architecture

```
Sensor Layer (Python)
  └─ sensors.py          — Ornstein-Uhlenbeck simulated sensors
  └─ sensor_publisher.py — Publishes JSON at 1 Hz via MQTT QoS-1

MQTT Broker (Mosquitto 2)
  ├─ TCP  port 1883  — Python publisher
  └─ WS   port 9001  — React browser client

Backend (FastAPI + aiosqlite + InfluxDB)
  └─ api.py              — Subscribes to MQTT, writes SQLite & InfluxDB, REST API

Time-Series (InfluxDB 2.7)
  └─ bucket: sensor-data — Flux-queryable, 30-day retention

Visualization (Grafana 10.4)
  └─ Pre-provisioned dashboard with live gauges + 24h stats table

Frontend (React + Recharts)
  └─ useMqtt.js          — Live MQTT subscription (WebSocket)
  └─ api.js              — REST calls for history / alerts / export
  └─ App.js              — Dashboard UI with browser notifications

Validation
  └─ stress_test.py      — 2-hour alert accuracy & data continuity validator

## Topic Schema

| Topic                       | Payload                                                         | QoS |
|-----------------------------|-----------------------------------------------------------------|-----|
| `lab/sensors/temperature`   | `{"ts":…, "value":27.3, "unit":"degC", "node":"pi-lab-01", "seq":…}` | 1 |
| `lab/sensors/pressure`      | same shape, unit `hPa`                                         | 1   |
| `lab/sensors/light`         | same shape, unit `lux`                                         | 1   |
| `lab/sensors/humidity`      | same shape, unit `pct`                                         | 1   |

## Quick Start

### Prerequisites
- Docker + Docker Compose

### Run

```bash
git clone <your-repo>
cd iotlab
docker compose up --build
```

| Service     | URL                        |
|-------------|----------------------------|
| Dashboard   | http://localhost:3000      |
| API docs    | http://localhost:8000/docs |
| Grafana     | http://localhost:3030      |
| InfluxDB UI | http://localhost:8086      |
| MQTT broker | localhost:1883             |

### Grafana

Pre-configured dashboard with live time-series charts, per-sensor gauges, and a 24h statistics table.

- **URL:** http://localhost:3030
- **Login:** `admin` / `iotlab`
- **Dashboard:** "IoTLab · Sensor Dashboard" (auto-provisioned)
- **Datasource:** InfluxDB (auto-provisioned, Flux language)

### API Endpoints

| Method | Path                       | Description                     |
|--------|----------------------------|---------------------------------|
| GET    | `/readings`                | Time-series readings (24h)      |
| GET    | `/readings/latest`         | Most recent per sensor          |
| GET    | `/alerts`                  | Alert history                   |
| GET    | `/thresholds`              | Current thresholds              |
| PUT    | `/thresholds/{sensor}`     | Update a threshold              |
| GET    | `/export`                  | Download CSV                    |
| GET    | `/stats`                   | 24h min/max/avg per sensor      |

### CSV Export

```bash
# All sensors, last 24h
curl http://localhost:8000/export --output readings.csv

# Single sensor
curl "http://localhost:8000/export?sensor=temperature" --output temp.csv
```

## Switching to Real Hardware

In `backend/sensors.py`, replace `SimulatedSensor` instances with Adafruit drivers:

```python
import board
import adafruit_bme280.basic as bme_lib
import adafruit_tsl2591

i2c    = board.I2C()
bme280 = bme_lib.Adafruit_BME280_I2C(i2c)
tsl    = adafruit_tsl2591.TSL2591(i2c)

class RealTemperatureSensor:
    @property
    def value(self): return bme280.temperature

SENSORS = {
    "temperature": RealTemperatureSensor(),
    "pressure":    ...,
    "light":       ...,
    "humidity":    ...,
}
```

Everything downstream is identical.

## Verification Metrics

| Metric             | Target      | How achieved                              |
|--------------------|-------------|-------------------------------------------|
| End-to-end latency | < 500 ms    | QoS-1 MQTT + async SQLite writes          |
| Data continuity    | > 99%       | QoS-1 retain, reconnect loop in publisher |
| Alert accuracy     | Zero missed | Edge-triggered detection in api.py        |

## Stress Test

Validates the three verification metrics from the spec:

```bash
# Install test dependencies
pip install paho-mqtt requests

# Full 2-hour stress test (spec requirement)
python tests/stress_test.py

# Quick 5-minute smoke test
python tests/stress_test.py --duration 300
```

The script independently:
1. Subscribes to all MQTT sensor topics
2. Tracks every reading, sequence gaps, and end-to-end latency
3. Detects threshold crossings locally
4. Cross-references local detections against API-stored alerts
5. Reports: data continuity %, P50/P95/P99 latency, missed alerts

## Tech Stack

Python 3.11 · paho-mqtt 2.x · FastAPI · aiosqlite · SQLite · InfluxDB 2.7  
Mosquitto 2 · React 18 · Recharts 2 · mqtt.js 5 · Grafana 10.4 · Docker Compose
