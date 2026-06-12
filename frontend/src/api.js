import axios from 'axios';

const API = axios.create({
  baseURL: process.env.REACT_APP_API_URL || 'http://localhost:8000',
  timeout: 5000,
});

export async function fetchHistory(sensor, hoursBack = 24) {
  const until = Date.now();
  const since = until - hoursBack * 3600 * 1000;
  const { data } = await API.get('/readings', {
    params: { sensor, since, until, limit: 86400 },
  });
  // API returns newest-first; reverse for charts
  return data.reverse();
}

export async function fetchLatest() {
  const { data } = await API.get('/readings/latest');
  return data;
}

export async function fetchAlerts(limit = 50) {
  const { data } = await API.get('/alerts', { params: { limit } });
  return data;
}

export async function fetchThresholds() {
  const { data } = await API.get('/thresholds');
  return data;
}

export async function setThreshold(sensor, value) {
  const { data } = await API.put(`/thresholds/${sensor}`, null, {
    params: { value },
  });
  return data;
}

export async function fetchStats() {
  const { data } = await API.get('/stats');
  return data;
}

export function exportUrl(sensor, hoursBack = 24) {
  const until = Date.now();
  const since = until - hoursBack * 3600 * 1000;
  const params = new URLSearchParams({ since, until });
  if (sensor) params.set('sensor', sensor);
  return `${API.defaults.baseURL}/export?${params}`;
}
