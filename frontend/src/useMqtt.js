import { useEffect, useRef, useState } from 'react';
import mqtt from 'mqtt';

const MQTT_URL = process.env.REACT_APP_MQTT_URL || 'ws://localhost:9001';
const TOPICS   = ['lab/sensors/#'];
const HISTORY  = 60; // points kept per sensor in live buffer

/**
 * useMqtt — returns:
 *   readings  : { temperature: {value, unit, ts}, pressure: …, … }
 *   history   : { temperature: [{value, ts}, …], … }   (last HISTORY points)
 *   connected : boolean
 *   latencyMs : number
 */
export function useMqtt() {
  const [readings,  setReadings]  = useState({});
  const [history,   setHistory]   = useState({});
  const [connected, setConnected] = useState(false);
  const [latencyMs, setLatencyMs] = useState(null);

  const histRef = useRef({});

  useEffect(() => {
    const client = mqtt.connect(MQTT_URL, {
      clientId: `dashboard-${Math.random().toString(16).slice(2, 8)}`,
      clean: true,
      reconnectPeriod: 2000,
    });

    client.on('connect', () => {
      setConnected(true);
      TOPICS.forEach(t => client.subscribe(t, { qos: 1 }));
    });

    client.on('disconnect', () => setConnected(false));
    client.on('error',      () => setConnected(false));

    client.on('message', (topic, payload) => {
      try {
        const data    = JSON.parse(payload.toString());
        const channel = topic.split('/').at(-1);
        const now     = Date.now();
        const latency = data.ts ? now - data.ts : null;
        if (latency !== null && latency >= 0) setLatencyMs(latency);

        const point = { value: data.value, unit: data.unit, ts: data.ts || now };

        setReadings(prev => ({ ...prev, [channel]: point }));

        // Append to rolling history
        histRef.current[channel] = [
          ...(histRef.current[channel] || []).slice(-(HISTORY - 1)),
          point,
        ];
        setHistory({ ...histRef.current });
      } catch (_) {}
    });

    return () => client.end();
  }, []);

  return { readings, history, connected, latencyMs };
}
