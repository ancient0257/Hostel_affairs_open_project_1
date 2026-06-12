import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer,
  Tooltip, XAxis, YAxis, ReferenceLine,
} from 'recharts';
import { format } from 'date-fns';
import { useMqtt } from './useMqtt';
import {
  fetchHistory, fetchAlerts, fetchThresholds,
  setThreshold as apiSetThreshold, exportUrl,
} from './api';
import './App.css';

// ── Sensor metadata ───────────────────────────────────────────────────
const SENSORS = {
  temperature: { label: 'Temperature', unit: '°C',  color: '#ff7b4f', icon: '🌡', dec: 1 },
  pressure:    { label: 'Pressure',    unit: ' hPa', color: '#4f9eff', icon: '⏱', dec: 1 },
  light:       { label: 'Light',       unit: ' lux', color: '#f7e04a', icon: '💡', dec: 0 },
  humidity:    { label: 'Humidity',    unit: '%',    color: '#a78bfa', icon: '💧', dec: 1 },
};

// ── Helpers ───────────────────────────────────────────────────────────
const fmt = (v, dec) => v != null ? Number(v).toFixed(dec) : '—';
const fmtTs = ms => {
  try { return format(new Date(ms), 'HH:mm:ss'); } catch { return ''; }
};
const fmtTsFull = ms => {
  try { return format(new Date(ms), 'HH:mm:ss dd/MM'); } catch { return ''; }
};

function useInterval(cb, delay) {
  const saved = useRef(cb);
  useEffect(() => { saved.current = cb; }, [cb]);
  useEffect(() => {
    if (delay === null) return;
    const id = setInterval(() => saved.current(), delay);
    return () => clearInterval(id);
  }, [delay]);
}

// ── Custom tooltip ────────────────────────────────────────────────────
function ChartTooltip({ active, payload, unit, dec }) {
  if (!active || !payload?.length) return null;
  const d = payload[0];
  return (
    <div style={{
      background: '#1a2035', border: '1px solid rgba(99,179,255,.2)',
      borderRadius: 6, padding: '8px 12px', fontSize: 11,
    }}>
      <div style={{ color: '#6b7fa8', marginBottom: 4 }}>
        {fmtTs(d.payload.ts)}
      </div>
      <div style={{ color: d.color, fontWeight: 600 }}>
        {fmt(d.value, dec)}{unit}
      </div>
    </div>
  );
}

// ── Sensor Chart ──────────────────────────────────────────────────────
function SensorChart({ channel, history, threshold, historyData }) {
  const meta = SENSORS[channel];
  // Merge live MQTT points + 24h DB history (deduplicated by ts)
  const seen = new Set();
  const combined = [...(historyData || []), ...(history || [])]
    .filter(p => { const k = p.ts; if (seen.has(k)) return false; seen.add(k); return true; })
    .sort((a, b) => a.ts - b.ts)
    .slice(-3600); // cap at 1 h for perf

  return (
    <ResponsiveContainer width="100%" height={120}>
      <AreaChart data={combined} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
        <defs>
          <linearGradient id={`grad-${channel}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%"  stopColor={meta.color} stopOpacity={0.25} />
            <stop offset="95%" stopColor={meta.color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(99,179,255,.08)" />
        <XAxis
          dataKey="ts"
          tickFormatter={fmtTs}
          tick={{ fill: '#6b7fa8', fontSize: 9 }}
          axisLine={false} tickLine={false}
          interval="preserveStartEnd"
        />
        <YAxis
          tick={{ fill: '#6b7fa8', fontSize: 9 }}
          axisLine={false} tickLine={false}
          domain={['auto', 'auto']}
        />
        <Tooltip content={<ChartTooltip unit={meta.unit} dec={meta.dec} />} />
        {threshold != null && (
          <ReferenceLine
            y={threshold}
            stroke="#ff4d6a"
            strokeDasharray="5 3"
            strokeWidth={1}
            label={{ value: `limit ${threshold}`, fill: '#ff4d6a', fontSize: 9, position: 'right' }}
          />
        )}
        <Area
          type="monotoneX"
          dataKey="value"
          stroke={meta.color}
          strokeWidth={1.5}
          fill={`url(#grad-${channel})`}
          dot={false}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ── Sensor Card ───────────────────────────────────────────────────────
function SensorCard({ channel, reading, history, threshold, historyData, onThresholdChange }) {
  const meta    = SENSORS[channel];
  const val     = reading?.value;
  const alerting = val != null && threshold != null && val > threshold;
  const [editing, setEditing] = useState(false);
  const [draft,   setDraft]   = useState('');

  const startEdit = () => { setDraft(String(threshold ?? '')); setEditing(true); };
  const commitEdit = () => {
    const n = parseFloat(draft);
    if (!isNaN(n)) onThresholdChange(channel, n);
    setEditing(false);
  };

  const buf = history || [];
  const mn  = buf.length ? Math.min(...buf.map(p => p.value)) : null;
  const mx  = buf.length ? Math.max(...buf.map(p => p.value)) : null;
  const avg = buf.length ? buf.reduce((s, p) => s + p.value, 0) / buf.length : null;

  return (
    <div className={`sensor-card${alerting ? ' alerting' : ''}`}
         style={{ '--cc': meta.color }}>
      <div className="card-top-bar" />
      <div className="card-label">{meta.icon} {meta.label} · CH{Object.keys(SENSORS).indexOf(channel) + 1}</div>
      <div className="card-value">
        {fmt(val, meta.dec)}
        <span className="card-unit">{meta.unit}</span>
      </div>
      <div className="card-meta">
        <span>min <b>{fmt(mn, meta.dec)}</b></span>
        <span>max <b>{fmt(mx, meta.dec)}</b></span>
        <span>avg <b>{fmt(avg, meta.dec)}</b></span>
      </div>
      <div className={`card-status ${alerting ? 'bad' : 'ok'}`}>
        {alerting ? '⚠ THRESHOLD EXCEEDED' : '● NOMINAL'}
      </div>
      <div className="card-chart">
        <SensorChart
          channel={channel}
          history={buf}
          threshold={threshold}
          historyData={historyData}
        />
      </div>
      <div className="card-threshold">
        <span className="th-label">Alert threshold</span>
        {editing ? (
          <span className="th-edit">
            <input
              autoFocus
              value={draft}
              onChange={e => setDraft(e.target.value)}
              onBlur={commitEdit}
              onKeyDown={e => e.key === 'Enter' && commitEdit()}
              className="th-input"
            />
            <span className="th-unit">{meta.unit}</span>
          </span>
        ) : (
          <span className="th-value" onClick={startEdit} title="Click to edit">
            {threshold != null ? `${threshold}${meta.unit}` : '—'}
            <span className="th-edit-icon">✎</span>
          </span>
        )}
      </div>
    </div>
  );
}

// ── Alert Log ─────────────────────────────────────────────────────────
function AlertLog({ alerts }) {
  if (!alerts.length) {
    return <div className="no-alerts">No alerts triggered yet</div>;
  }
  return (
    <div className="alert-list">
      {alerts.map(a => (
        <div key={a.id} className={`alert-entry ${a.severity === 'CRITICAL' ? 'crit' : 'warn'}`}>
          <div className="alert-dot" />
          <div>
            <div className="alert-msg">
              lab/sensors/{a.sensor} → {Number(a.value).toFixed(2)} &gt; {a.threshold}
            </div>
            <div className="alert-time">{fmtTsFull(a.ts)}</div>
          </div>
          <div className="alert-badge">{a.severity}</div>
        </div>
      ))}
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────
export default function App() {
  const { readings, history, connected, latencyMs } = useMqtt();

  const [thresholds,   setThresholds]   = useState({});
  const [alerts,       setAlerts]       = useState([]);
  const [historyData,  setHistoryData]  = useState({});
  const [pktCount,     setPktCount]     = useState(0);
  const [clock,        setClock]        = useState('');
  const [sessionStart] = useState(Date.now());
  const [uptime,       setUptime]       = useState('00:00');

  // Clock
  useInterval(() => {
    setClock(format(new Date(), 'HH:mm:ss'));
    const s = Math.floor((Date.now() - sessionStart) / 1000);
    setUptime(`${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`);
  }, 1000);

  // Packet counter
  useEffect(() => {
    if (Object.keys(readings).length) setPktCount(n => n + 1);
  }, [readings]);

  // Load thresholds once
  useEffect(() => {
    fetchThresholds().then(setThresholds).catch(() => {});
  }, []);

  // Poll alerts every 5 s — also fire browser notifications for new alerts
  const prevAlertIds = useRef(new Set());
  const notifyGranted  = useRef(false);

  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission().then(p => {
        notifyGranted.current = p === 'granted';
      });
    } else if ('Notification' in window && Notification.permission === 'granted') {
      notifyGranted.current = true;
    }
  }, []);

  const loadAlerts = useCallback(() => {
    fetchAlerts(30).then(newAlerts => {
      setAlerts(prev => {
        const prevIds = new Set(prev.map(a => a.id));
        // Fire notification for alerts not seen before
        if (notifyGranted.current && window.document.visibilityState !== 'visible') {
          for (const a of newAlerts) {
            if (!prevIds.has(a.id) && !prevAlertIds.current.has(a.id)) {
              new window.Notification(`⚠ ${a.severity} — ${a.sensor}`, {
                body: `Value ${Number(a.value).toFixed(2)} exceeds threshold ${a.threshold}`,
                icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><text y="28" font-size="28">⚠</text></svg>',
                tag: `alert-${a.id}`,
                requireInteraction: a.severity === 'CRITICAL',
              });
            }
          }
        }
        newAlerts.forEach(a => prevAlertIds.current.add(a.id));
        return newAlerts;
      });
    }).catch(() => {});
  }, []);
  useInterval(loadAlerts, 5000);
  useEffect(() => { loadAlerts(); }, [loadAlerts]);

  // Load 24h history per sensor on mount
  useEffect(() => {
    Promise.all(
      Object.keys(SENSORS).map(ch =>
        fetchHistory(ch, 24).then(data => [ch, data])
      )
    ).then(pairs => {
      const obj = {};
      pairs.forEach(([ch, data]) => { obj[ch] = data; });
      setHistoryData(obj);
    }).catch(() => {});
  }, []);

  const handleThresholdChange = async (channel, value) => {
    try {
      await apiSetThreshold(channel, value);
      setThresholds(prev => ({ ...prev, [channel]: value }));
    } catch (e) {
      console.error('Failed to update threshold', e);
    }
  };

  return (
    <div className="app">

      {/* ── TOP BAR ── */}
      <header className="topbar">
        <div className="topbar-left">
          <span className="logo">IOT<span>LAB</span> · MONITOR</span>
          <span className="badge-live">
            <span className={`live-dot ${connected ? 'on' : 'off'}`} />
            {connected ? 'LIVE' : 'CONNECTING…'}
          </span>
        </div>
        <div className="topbar-right">
          <span className="meta-item">mqtt://lab.local:1883</span>
          {latencyMs != null && (
            <span className="meta-item">latency <b style={{ color: '#00e5b4' }}>{latencyMs}ms</b></span>
          )}
          <span className="meta-item">uptime <b>{uptime}</b></span>
          <span className="meta-item">pkts <b>{pktCount}</b></span>
          <span className="clock">{clock}</span>
          <a
            href={exportUrl(null, 24)}
            download
            className="export-btn"
          >
            ⬇ Export CSV
          </a>
        </div>
      </header>

      {/* ── SENSOR CARDS ── */}
      <div className="cards-grid">
        {Object.keys(SENSORS).map(ch => (
          <SensorCard
            key={ch}
            channel={ch}
            reading={readings[ch]}
            history={history[ch]}
            threshold={thresholds[ch]}
            historyData={historyData[ch]}
            onThresholdChange={handleThresholdChange}
          />
        ))}
      </div>

      {/* ── ALERT LOG ── */}
      <div className="bottom-section">
        <div className="alert-panel">
          <div className="panel-title">
            Alert Log
            {alerts.length > 0 && (
              <span className="alert-count">{alerts.length}</span>
            )}
          </div>
          <AlertLog alerts={alerts} />
        </div>

        <div className="topic-panel">
          <div className="panel-title">MQTT Topics</div>
          <div className="topic-list">
            {[
              ['lab/sensors/temperature', '1 Hz'],
              ['lab/sensors/pressure',    '1 Hz'],
              ['lab/sensors/light',       '1 Hz'],
              ['lab/sensors/humidity',    '1 Hz'],
              ['lab/alerts/threshold',    'on-event'],
            ].map(([t, hz]) => (
              <div key={t} className="topic-row">
                <span className="topic-name">{t}</span>
                <span className="topic-hz">{hz}</span>
              </div>
            ))}
          </div>
          <div className="panel-title" style={{ marginTop: 16 }}>Quick Export</div>
          <div className="export-links">
            {Object.keys(SENSORS).map(ch => (
              <a key={ch} href={exportUrl(ch, 24)} download className="export-link">
                {SENSORS[ch].icon} {SENSORS[ch].label} CSV
              </a>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
