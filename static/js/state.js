// Minimal store: token + settings + a few per-viewer conveniences (localStorage, best effort).
import { storageGet, storageSet, storageDel } from './util.js';

const DEFAULT_SETTINGS = {
  subtitle_lang: 'ko', mode: 'listening', stretch: false, playback_rate: 1.0,
  reveal_de_on_tap: true, prefer_dub: false, llm_enabled: true, onboarded: false, topics: [],
};

const listeners = new Set();
export const state = {
  token: storageGet('ds_token', null),
  settings: { ...DEFAULT_SETTINGS },
  lastTab: storageGet('ds_tab', 'feed'),
  vocabCount: 0,
  online: true,
};

export function set(patch) {
  Object.assign(state, patch);
  if ('token' in patch) { patch.token ? storageSet('ds_token', patch.token) : storageDel('ds_token'); }
  if ('lastTab' in patch) storageSet('ds_tab', patch.lastTab);
  for (const fn of listeners) { try { fn(state, patch); } catch (e) { console.error(e); } }
}

export function setSettings(patch) {
  set({ settings: { ...state.settings, ...patch } });
}

export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }

// Pull a token from the URL hash on first visit: https://host/#token=XYZ
export function takeTokenFromHash() {
  const m = /(?:^#|[#&])token=([^&]+)/.exec(location.hash || '');
  if (!m) return false;
  set({ token: decodeURIComponent(m[1]) });
  history.replaceState(null, '', location.pathname + location.search);
  return true;
}
