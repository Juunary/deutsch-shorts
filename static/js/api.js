// Fetch wrapper for the same-origin API (Bearer token, JSON bodies, typed errors).
import { state, set } from './state.js';

export class ApiError extends Error {
  constructor(status, detail) { super(detail || `HTTP ${status}`); this.status = status; this.detail = detail; }
}

function buildQuery(query) {
  if (!query) return '';
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') q.set(k, v);
  const s = q.toString();
  return s ? `?${s}` : '';
}

export async function api(path, { method = 'GET', body, query, form, keepalive = false } = {}) {
  const headers = { Authorization: `Bearer ${state.token || ''}` };
  const init = { method, headers, keepalive };
  if (form) init.body = form;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(path + buildQuery(query), init);
  } catch (e) {
    set({ online: false });
    throw new ApiError(0, 'offline');
  }
  set({ online: true });
  if (res.status === 401) {
    document.dispatchEvent(new CustomEvent('ds:unauthorized'));
    throw new ApiError(401, 'unauthorized');
  }
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).detail; } catch { /* ignore */ }
    throw new ApiError(res.status, detail || res.statusText);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

export const getHealth = () => api('/api/health');
export const getFeed = (n, exclude) => api('/api/feed', { query: { n, exclude: exclude.join(',') } });
export const getSubtitles = (id, lang) => api(`/api/videos/${encodeURIComponent(id)}/subtitles`, { query: { lang } });
export const enrichVideo = (id) => api(`/api/videos/${encodeURIComponent(id)}/enrich`, { method: 'POST' });
export const getSettings = () => api('/api/settings');
export const putSettings = (patch) => api('/api/settings', { method: 'PUT', body: patch });
export const getTopics = () => api('/api/topics');
export const getChannels = () => api('/api/channels');
export const putChannel = (id, enabled) => api(`/api/channels/${encodeURIComponent(id)}`, { method: 'PUT', body: { enabled } });
export const getVocab = () => api('/api/vocab');
export const addVocab = (item) => api('/api/vocab', { method: 'POST', body: item });
export const deleteVocab = (id) => api(`/api/vocab/${id}`, { method: 'DELETE' });
export const postCorrection = (c) => api('/api/corrections', { method: 'POST', body: c });
export const importTakeout = (file) => { const f = new FormData(); f.append('file', file); return api('/api/import/takeout', { method: 'POST', form: f }); };
export const runPipeline = (stage = 'all') => api('/api/admin/pipeline/run', { method: 'POST', query: { stage } });
export const pipelineStatus = () => api('/api/admin/pipeline/status');
export const vocabExportUrl = () => `/api/vocab/export.tsv?mark=1&token=${encodeURIComponent(state.token || '')}`;

// Events are fire-and-forget; batched a little so swipes don't spam the server.
let queue = [];
let flushTimer = 0;
export function sendEvents(list) {
  queue.push(...list);
  clearTimeout(flushTimer);
  flushTimer = setTimeout(flushEvents, 400);
}
export function flushEvents() {
  if (!queue.length) return;
  const batch = queue;
  queue = [];
  api('/api/events', { method: 'POST', body: batch, keepalive: true }).catch(() => { /* best effort */ });
}
document.addEventListener('visibilitychange', () => { if (document.hidden) flushEvents(); });
