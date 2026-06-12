"""
sensors.py — Simulated sensor layer.

To switch to real hardware (Raspberry Pi + BME280 + TSL2591), replace
the SimulatedSensor classes with the Adafruit driver equivalents:

    import board, adafruit_bme280.basic as bme_lib, adafruit_tsl2591
    i2c    = board.I2C()
    bme280 = bme_lib.Adafruit_BME280_I2C(i2c)
    tsl    = adafruit_tsl2591.TSL2591(i2c)

    class RealTemperatureSensor:
        @property
        def value(self): return bme280.temperature

    # ... etc.

Everything downstream (sensor_publisher.py) uses .value — no other changes needed.
"""

import math
import random
import time


class SimulatedSensor:
    """
    Ornstein-Uhlenbeck mean-reverting process with Gaussian noise.
    Produces realistic drift rather than pure random walks.
    """

    def __init__(
        self,
        mean: float,
        std: float,
        theta: float = 0.05,   # mean-reversion speed
        sigma: float = None,   # noise amplitude (defaults to std * 0.3)
        low: float = None,
        high: float = None,
    ):
        self.mean  = mean
        self.std   = std
        self.theta = theta
        self.sigma = sigma or std * 0.3
        self.low   = low
        self.high  = high
        self._x    = mean  # current state

    @property
    def value(self) -> float:
        # OU update: dx = theta*(mean - x)*dt + sigma*dW
        dt = 1.0
        dW = random.gauss(0, 1) * math.sqrt(dt)
        self._x += self.theta * (self.mean - self._x) * dt + self.sigma * dW
        if self.low  is not None: self._x = max(self.low,  self._x)
        if self.high is not None: self._x = min(self.high, self._x)
        return round(self._x, 3)


# ── Sensor instances ─────────────────────────────────────────────────
# These replicate typical physics-lab conditions:
#   Temperature : lab room ~22–28 °C, occasional spike during experiments
#   Pressure    : standard atmospheric ~1010–1016 hPa
#   Light       : bench illumination ~400–900 lux
#   Humidity    : climate-controlled lab ~45–65 % RH

SENSORS = {
    "temperature": SimulatedSensor(mean=25.0, std=2.0,  sigma=0.15, low=18.0, high=45.0),
    "pressure":    SimulatedSensor(mean=1013.0, std=3.0, sigma=0.8,  low=980.0, high=1040.0),
    "light":       SimulatedSensor(mean=620.0, std=80.0, sigma=12.0, low=0.0, high=1200.0),
    "humidity":    SimulatedSensor(mean=55.0, std=5.0,  sigma=0.6,  low=20.0, high=95.0),
}

UNITS = {
    "temperature": "degC",
    "pressure":    "hPa",
    "light":       "lux",
    "humidity":    "pct",
}
