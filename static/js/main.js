// Boot: token, service worker, settings, views/tabs, feed.
import { getSettings } from './api.js';
import { state, set, subscribe, takeTokenFromHash } from './state.js';
import { initFeed, feedResume, feedPause, onSettingsChanged } from './feed.js';
import { renderVocab } from './vocab.js';
import { renderSettings } from './settings.js';
import { renderOnboarding } from './onboarding.js';
import { $, $$, toast } from './util.js';
import { t } from './i18n.ko.js';

const views = { feed: $('#view-feed'), vocab: $('#view-vocab'), settings: $('#view-settings'), onboarding: $('#view-onboarding'), token: $('#view-token') };
let feedReady = false;
let current = 'feed';

function showView(name) {
  for (const [k, v] of Object.entries(views)) v.hidden = k !== name;
  for (const tab of $$('#tabbar .tab')) tab.classList.toggle('active', tab.dataset.view === name);
  if (name !== 'feed') feedPause();
  current = name;
  if (name === 'feed' || name === 'vocab' || name === 'settings') set({ lastTab: name });
}

async function openTab(name) {
  if (name === 'feed') {
    showView('feed');
    if (!feedReady) { feedReady = true; await initFeed($('#rail')); }
    feedResume();
  } else if (name === 'vocab') {
    showView('vocab');
    renderVocab(views.vocab);
  } else if (name === 'settings') {
    showView('settings');
    renderSettings(views.settings, { onChange: (prev) => onSettingsChanged(prev) });
  }
}

function wireTabs() {
  for (const tab of $$('#tabbar .tab')) tab.addEventListener('click', () => openTab(tab.dataset.view));
  window.addEventListener('hashchange', () => {
    const name = location.hash.replace('#', '');
    if (['feed', 'vocab', 'settings'].includes(name) && name !== current) openTab(name);
  });
}

function wireToken() {
  $('#token-save').addEventListener('click', () => {
    const v = $('#token-input').value.trim();
    if (!v) return;
    set({ token: v });
    location.reload();
  });
  document.addEventListener('ds:unauthorized', () => showView('token'));
}

function registerSW() {
  if (!('serviceWorker' in navigator)) return;
  if (location.protocol !== 'https:' && !['localhost', '127.0.0.1'].includes(location.hostname)) return;
  navigator.serviceWorker.register('/sw.js').catch((e) => console.warn('sw', e));
}

function updateBadge() {
  const b = $('#vocab-count');
  b.hidden = !state.vocabCount;
  b.textContent = String(state.vocabCount);
}

async function boot() {
  takeTokenFromHash();
  wireTabs();
  wireToken();
  registerSW();
  subscribe((_s, patch) => { if ('vocabCount' in patch) updateBadge(); });
  document.title = t.app;
  if (!state.token) { showView('token'); return; }
  let settings;
  try {
    settings = await getSettings();
  } catch (e) {
    if (e.status === 401) { showView('token'); return; }
    toast(t.offline);
    settings = state.settings;
  }
  set({ settings });
  if (!settings.onboarded) {
    showView('onboarding');
    await renderOnboarding(views.onboarding, () => openTab('feed'));
    return;
  }
  const initial = ['feed', 'vocab', 'settings'].includes(location.hash.replace('#', '')) ? location.hash.replace('#', '') : 'feed';
  openTab(initial);
}

boot().catch((e) => { console.error(e); toast(t.error); });
