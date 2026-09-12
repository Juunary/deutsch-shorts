// The vertical feed: cards, the shared player, subtitle sync, events, word/correction sheets.
import * as player from './player.js';
import { createSync } from './subtitles.js';
import { buildGlossMap, lookupGloss, wordSheet } from './gloss.js';
import { renderPanel, correctionEditor, dubSheet } from './modes.js';
import { getFeed, getSubtitles, sendEvents, addVocab, postCorrection, putSettings } from './api.js';
import { state, set, setSettings } from './state.js';
import { el, toast, fmtDuration, debounce, longPress } from './util.js';
import { t } from './i18n.ko.js';

const cards = [];
const loadedIds = [];
const subsCache = new Map();
let rail = null;
let observer = null;
let active = null;
let started = false;      // first user tap happened (sound allowed)
let loading = false;
let exhausted = false;
let pendingLoad = null;   // card whose video could not be loaded outside a gesture
let playCheckTimer = 0;
let emptyNode = null;

const sync = createSync({ getTime: player.getTime, state: player.state }, onIndex);

// ---------------------------------------------------------------------------
export async function initFeed(railEl) {
  rail = railEl;
  observer = new IntersectionObserver(onIntersect, { root: rail, threshold: [0.6] });
  rail.addEventListener('touchend', flushPending, { passive: true });
  rail.addEventListener('click', flushPending);
  player.on('statechange', onPlayerState);
  player.on('error', onPlayerError);
  document.addEventListener('visibilitychange', () => { if (document.hidden) player.pause(); });
  await loadMore();
}

export function feedResume() {            // called from a click on the feed tab (user gesture)
  if (started && active && !active.pairMode) player.play();
}
export function feedPause() { player.pause(); }

export function onSettingsChanged(prev) {
  if (prev.subtitle_lang !== state.settings.subtitle_lang) {
    subsCache.clear();
    for (const c of cards) { c.subs = null; c.glossMap = new Map(); }
    if (active) ensureSubs(active).then(() => { if (active) { renderPanel(active, active.subsEl); if (started) sync.start(active.subs.segments); } });
  }
  if (prev.playback_rate !== state.settings.playback_rate) { player.setRate(state.settings.playback_rate); sync.setRate(state.settings.playback_rate); }
  for (const c of cards) updateCtrl(c);
  if (active) renderPanel(active, active.subsEl);
}

// ---------------------------------------------------------------------------
async function loadMore() {
  if (loading || exhausted) return;
  loading = true;
  try {
    const { items } = await getFeed(10, loadedIds.slice(-100));
    let added = 0;
    for (const item of items) {
      if (loadedIds.includes(item.video_id)) continue;
      loadedIds.push(item.video_id);
      const card = createCard(item);
      cards.push(card);
      rail.append(card.node);
      observer.observe(card.node);
      added++;
    }
    if (!added) exhausted = cards.length > 0;
    if (!cards.length) showEmpty();
    else if (emptyNode) { emptyNode.remove(); emptyNode = null; }
    if (cards.length && !player.isCreated()) {
      player.create(cards[0].item.video_id).catch((e) => console.warn('player create failed', e));
    }
  } catch (e) {
    if (e.status !== 401) toast(t.offline);
  } finally {
    loading = false;
  }
}

function showEmpty() {
  if (emptyNode) return;
  emptyNode = el('div', { class: 'card' }, [el('div', { class: 'subs' }, [el('div', { class: 'empty', text: t.empty_feed })])]);
  rail.append(emptyNode);
}

function maybeLoadMore() {
  const i = cards.indexOf(active);
  if (i >= 0 && cards.length - i <= 3) loadMore();
}

// ---------------------------------------------------------------------------
function createCard(item) {
  const card = {
    item, node: null, subsEl: null, sheetHost: null, ctrl: {},
    subs: null, subsLoading: false, glossMap: new Map(),
    idx: -1, revealed: false, showTr: false, maxTime: 0, playedSent: false, pairMode: false, visible: false,
    get settings() { return state.settings; },
  };
  const head = el('header', { class: 'card-head' }, [
    el('span', { class: 'chan', text: item.channel.title || item.channel.handle || '' }),
    item.cefr ? el('span', { class: 'chip cefr', text: item.cefr }) : null,
    item.duration_s ? el('span', { class: 'chip', text: fmtDuration(item.duration_s) }) : null,
    item.has_model ? el('span', { class: 'chip model', text: 'AI' }) : null,
    item.has_dub > 0 ? el('span', { class: 'chip action', text: t.dub_badge, onclick: () => openDubSheet(card) }) : null,
    item.pair_video_id && !(item.has_dub > 0) ? el('span', { class: 'chip action', text: t.watch_en, onclick: () => togglePair(card) }) : null,
    el('a', { class: 'ytlink', href: item.youtube_url, target: '_blank', rel: 'noopener', text: t.on_youtube }),
  ]);
  const subsEl = el('div', { class: 'subs' });
  const title = item.title ? el('div', { class: 'muted', text: item.summary_ko || item.title }) : null;
  const ctrl = el('div', { class: 'ctrl' }, [
    card.ctrl.mode = el('button', { text: '', onclick: () => toggleMode() }),
    card.ctrl.speed = el('button', { text: '', onclick: () => toggleSpeed() }),
    card.ctrl.like = el('button', { text: '♥ ' + t.like, onclick: (e) => { sendEvents([{ video_id: item.video_id, type: 'like', mode: state.settings.mode }]); e.currentTarget.classList.add('liked'); } }),
    el('button', { text: t.skip + ' ›', onclick: () => { sendEvents([{ video_id: item.video_id, type: 'skip', mode: state.settings.mode }]); advance(); } }),
    el('button', { text: '🔺 ' + t.too_hard_short, onclick: () => { sendEvents([{ video_id: item.video_id, type: 'too_hard' }]); toast(t.too_hard + ' 반영'); } }),
    el('button', { text: '🔻 ' + t.too_easy_short, onclick: () => { sendEvents([{ video_id: item.video_id, type: 'too_easy' }]); toast(t.too_easy + ' 반영'); } }),
  ]);
  const sheetHost = el('div', { class: 'sheet-host' });
  card.node = el('article', { class: 'card', dataset: { id: item.video_id } }, [head, title, subsEl, ctrl, sheetHost]);
  card.subsEl = subsEl;
  card.sheetHost = sheetHost;
  subsEl.addEventListener('click', (e) => {
    const w = e.target.closest('.w');
    if (w) { e.preventDefault(); openWordSheet(card, w.dataset.w); }
  });
  longPress(subsEl, (e) => { if (e.target.closest('.subs-tr')) openCorrection(card); });
  updateCtrl(card);
  return card;
}

function updateCtrl(card) {
  const m = state.settings.mode;
  card.ctrl.mode.textContent = m === 'listening' ? '👂 ' + t.mode_listening : '📖 ' + t.mode_reading;
  card.ctrl.speed.textContent = state.settings.playback_rate === 0.75 ? '0.75×' : '1×';
  card.ctrl.speed.classList.toggle('on', state.settings.playback_rate === 0.75);
}

function toggleMode() {
  const mode = state.settings.mode === 'listening' ? 'reading' : 'listening';
  const prev = { ...state.settings };
  setSettings({ mode });
  putSettings({ mode }).catch(() => {});
  onSettingsChanged(prev);
}

function toggleSpeed() {
  const rate = state.settings.playback_rate === 0.75 ? 1.0 : 0.75;
  const prev = { ...state.settings };
  setSettings({ playback_rate: rate });
  putSettings({ playback_rate: rate }).catch(() => {});
  onSettingsChanged(prev);
}

// ---------------------------------------------------------------------------
function onIntersect(entries) {
  for (const en of entries) {
    const card = cards.find((c) => c.node === en.target);
    if (card) card.visible = en.isIntersecting && en.intersectionRatio >= 0.6;
  }
  scheduleActivate();
}
const scheduleActivate = debounce(() => {
  const vis = cards.find((c) => c.visible);
  if (vis && vis !== active) activate(vis);
}, 150);

async function activate(card) {
  if (active) deactivate(active);
  active = card;
  card.maxTime = 0; card.playedSent = false; card.idx = -1; card.revealed = false; card.showTr = false; card.pairMode = false;
  card.sheetHost.innerHTML = '';
  if (!started) {
    if (player.isCreated()) player.cue(card.item.video_id);
    renderStart(card);
  } else {
    startPlayback(card);
  }
  sendEvents([{ video_id: card.item.video_id, type: 'impression', mode: state.settings.mode }]);
  await ensureSubs(card);
  if (active !== card) return;
  renderPanel(card, card.subsEl);
  if (started) sync.start(card.subs.segments);
  if (!started) renderStart(card);
  maybeLoadMore();
}

function deactivate(card) {
  sync.stop();
  clearTimeout(playCheckTimer);
  if (started && !card.pairMode) {
    card.maxTime = Math.max(card.maxTime, player.getTime() || 0);
    const dur = player.getDuration() || 0;
    if (dur > 0) sendEvents([{ video_id: card.item.video_id, type: 'watch', value: Math.min(1, card.maxTime / dur), mode: state.settings.mode }]);
  }
}

function renderStart(card) {
  if (card.subsEl.querySelector('.start-btn')) return;
  const btn = el('button', { class: 'start-btn', text: t.tap_to_start, onclick: () => {
    started = true;
    btn.remove();
    if (!player.isCreated()) {
      player.create(card.item.video_id).then(() => player.play());
    } else {
      if (player.current() !== card.item.video_id) player.load(card.item.video_id); else player.play();
    }
    player.setRate(state.settings.playback_rate);
    sync.setRate(state.settings.playback_rate);
    armPlayCheck(card);
    if (card.subs) sync.start(card.subs.segments);
  } });
  card.subsEl.prepend(btn);
}

function startPlayback(card) {
  const ok = player.load(card.item.video_id);
  if (!ok) { pendingLoad = card; return; }
  armPlayCheck(card);
}

function flushPending() {          // runs inside a user gesture (touchend/click on the rail)
  if (pendingLoad && pendingLoad === active) {
    const card = pendingLoad; pendingLoad = null;
    if (player.load(card.item.video_id)) armPlayCheck(card);
  } else if (started && active && player.state() !== player.STATE.PLAYING && player.state() !== player.STATE.BUFFERING && !active.pairMode) {
    // nothing
  }
}

function armPlayCheck(card) {
  clearTimeout(playCheckTimer);
  playCheckTimer = setTimeout(() => {
    if (active !== card) return;
    const s = player.state();
    if (s !== player.STATE.PLAYING && s !== player.STATE.BUFFERING) showPlayButton(card);
  }, 1200);
}

function showPlayButton(card) {
  if (card.subsEl.querySelector('.play-btn')) return;
  const btn = el('button', { class: 'play-btn', text: t.play, onclick: () => { player.play(); btn.remove(); } });
  card.subsEl.prepend(btn);
}

function onPlayerState(e) {
  const s = e.detail;
  if (!active) return;
  if (s === player.STATE.PLAYING) {
    const pb = active.subsEl.querySelector('.play-btn'); if (pb) pb.remove();
    sync.reanchor();
    if (!active.playedSent && !active.pairMode) { active.playedSent = true; sendEvents([{ video_id: active.item.video_id, type: 'play', mode: state.settings.mode }]); }
  }
  if (s === player.STATE.ENDED && !active.pairMode) {
    active.maxTime = Math.max(active.maxTime, player.getDuration() || 0);
    sendEvents([{ video_id: active.item.video_id, type: 'complete', value: 1, mode: state.settings.mode }]);
    advance();
  }
  if (s === player.STATE.PAUSED || s === player.STATE.BUFFERING) active.maxTime = Math.max(active.maxTime, player.getTime() || 0);
}

function onPlayerError(e) {
  if (!active) return;
  console.warn('player error', e.detail);
  sendEvents([{ video_id: active.pairMode ? active.item.pair_video_id : active.item.video_id, type: 'embed_error', value: e.detail }]);
  toast(t.error + ` (YouTube ${e.detail})`);
  setTimeout(advance, 800);
}

function onIndex(idx) {
  if (!active || active.pairMode) return;
  active.idx = idx;
  if (state.settings.reveal_de_on_tap) active.revealed = false;
  active.maxTime = Math.max(active.maxTime, player.getTime() || 0);
  renderPanel(active, active.subsEl);
}

function advance() {
  const i = cards.indexOf(active);
  const next = cards[i + 1];
  if (next) next.node.scrollIntoView({ behavior: 'smooth', block: 'start' });
  else loadMore();
}

// ---------------------------------------------------------------------------
async function ensureSubs(card) {
  if (card.subs) return card.subs;
  const key = `${card.item.video_id}:${state.settings.subtitle_lang}`;
  if (subsCache.has(key)) {
    card.subs = subsCache.get(key);
  } else {
    card.subsLoading = true;
    renderPanel(card, card.subsEl);
    try {
      card.subs = await getSubtitles(card.item.video_id, state.settings.subtitle_lang);
    } catch (e) {
      card.subs = { segments: [], glosses: [], tr_source: null };
      if (e.status !== 401) toast(t.offline);
    }
    card.subsLoading = false;
    subsCache.set(key, card.subs);
  }
  card.glossMap = buildGlossMap(card.subs.glosses);
  return card.subs;
}

function openWordSheet(card, word) {
  const gloss = lookupGloss(card.glossMap, word);
  const seg = card.subs && card.idx >= 0 ? card.subs.segments[card.idx] : null;
  const sheet = wordSheet({
    word, gloss,
    sentenceDe: seg ? (seg.de_clean || seg.de) : '',
    sentenceTr: seg ? seg.tr : '',
    onSave: async (btn) => {
      try {
        await addVocab({
          lemma: gloss ? gloss.lemma : word, surface: word, pos: gloss ? gloss.pos : null,
          gloss_ko: gloss ? gloss.ko : null, gloss_en: gloss ? gloss.en : null,
          video_id: card.item.video_id, seg_idx: seg ? seg.idx : null,
          sentence_de: seg ? (seg.de_clean || seg.de) : null, sentence_ko: seg ? seg.tr : null,
        });
        btn.textContent = t.saved; btn.disabled = true;
        set({ vocabCount: state.vocabCount + 1 });
        toast(t.word_saved);
      } catch (e) { toast(t.offline); }
    },
    onWrong: async () => {
      try {
        await postCorrection({ video_id: card.item.video_id, seg_idx: seg ? seg.idx : null, kind: 'gloss', after: { surface: gloss.surface, wrong: true } });
        card.glossMap.delete(String(gloss.surface).toLowerCase());
        renderPanel(card, card.subsEl);
        card.sheetHost.innerHTML = '';
        toast(t.gloss_reported);
      } catch (e) { toast(t.offline); }
    },
    onClose: () => { card.sheetHost.innerHTML = ''; },
  });
  card.sheetHost.replaceChildren(sheet);
}

function openCorrection(card) {
  if (!card.subs || card.idx < 0) return;
  const seg = card.subs.segments[card.idx];
  const editor = correctionEditor({
    current: seg.tr || '',
    onSave: async (text) => {
      if (!text) return;
      try {
        await postCorrection({ video_id: card.item.video_id, seg_idx: seg.idx, kind: 'translation', before: { text: seg.tr }, after: { lang: state.settings.subtitle_lang, text } });
        seg.tr = text;
        renderPanel(card, card.subsEl);
        card.sheetHost.innerHTML = '';
        toast(t.correction_saved);
      } catch (e) { toast(t.offline); }
    },
    onCancel: () => { card.sheetHost.innerHTML = ''; },
  });
  card.sheetHost.replaceChildren(editor);
}

function openDubSheet(card) {
  const sheet = dubSheet({
    pairId: card.item.pair_video_id,
    onWatchPair: () => togglePair(card),
    onReport: () => sendEvents([{ video_id: card.item.video_id, type: 'no_dub' }]),
    onClose: () => { card.sheetHost.innerHTML = ''; },
  });
  card.sheetHost.replaceChildren(sheet);
}

function togglePair(card) {
  if (!card.item.pair_video_id || card !== active || !started) return;
  card.sheetHost.innerHTML = '';
  if (!card.pairMode) {
    card.pairMode = true;
    sync.stop();
    player.load(card.item.pair_video_id);
    card.subsEl.replaceChildren(
      el('div', { class: 'empty', text: t.watch_en }),
      el('button', { class: 'reveal', text: t.back_de, onclick: () => togglePair(card) }),
    );
  } else {
    card.pairMode = false;
    player.load(card.item.video_id);
    renderPanel(card, card.subsEl);
    if (card.subs) sync.start(card.subs.segments);
  }
}
