// The vertical feed: a swipe pager under the shared player (like Shorts: swipe up = next, swipe down = previous),
// subtitle sync, implicit feedback events, word/correction sheets.
import * as player from './player.js';
import { createSync } from './subtitles.js';
import { buildGlossMap, lookupGloss, wordSheet } from './gloss.js';
import { renderPanel, correctionEditor, dubSheet } from './modes.js';
import { getFeed, getSubtitles, sendEvents, addVocab, postCorrection, putSettings } from './api.js';
import { state, set, setSettings } from './state.js';
import { el, toast, fmtDuration, longPress } from './util.js';
import { t } from './i18n.ko.js';

const COMMIT_RATIO = 0.18;      // drag this share of the panel height to change cards
const COMMIT_VELOCITY = 0.5;    // or flick faster than this (px per ms)
const SLOP = 8;                 // px of movement before a touch counts as a drag
const ANIM_MS = 300;
const KEEP_BEHIND = 25;         // cards kept behind the current one (swipe down to go back)
const PREFETCH = 2;             // upcoming cards whose subtitles (and on-demand translation) are fetched early

const cards = [];
const loadedIds = [];
const subsCache = new Map();
let rail = null;
let cur = -1;               // index of the active card in `cards`
let active = null;
let started = false;        // first user tap happened (sound allowed)
let loading = false;
let exhausted = false;
let pendingLoad = null;     // card whose video could not be loaded outside a gesture
let playCheckTimer = 0;
let emptyNode = null;
let animating = false;
let drag = null;            // pointer gesture in progress
let suppressClick = false;  // swallow the click that follows a mouse drag
let settleTimer = 0;

const sync = createSync({ getTime: player.getTime, state: player.state }, onIndex);

// ---------------------------------------------------------------------------
export async function initFeed(railEl) {
  rail = railEl;
  wirePager();
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
  if (loading || exhausted) return false;
  loading = true;
  let added = 0;
  try {
    const { items } = await getFeed(10, loadedIds.slice(-100));
    for (const item of items) {
      if (loadedIds.includes(item.video_id)) continue;
      loadedIds.push(item.video_id);
      const card = createCard(item);
      cards.push(card);
      rail.append(card.node);
      added++;
    }
    if (!added) exhausted = cards.length > 0;
    if (!cards.length) showEmpty();
    else if (emptyNode) { emptyNode.remove(); emptyNode = null; }
    if (cards.length && !player.isCreated()) {
      player.create(cards[0].item.video_id).catch((e) => console.warn('player create failed', e));
    }
    layout();
    if (cur < 0 && cards.length) show(0);
  } catch (e) {
    if (e.status !== 401) toast(t.offline);
  } finally {
    loading = false;
  }
  return added > 0;
}

function showEmpty() {
  if (emptyNode) return;
  emptyNode = el('div', { class: 'card', style: 'transform:none' }, [el('div', { class: 'subs' }, [el('div', { class: 'empty', text: t.empty_feed })])]);
  rail.append(emptyNode);
}

function maybeLoadMore() {
  if (cards.length - cur <= 3) loadMore();
}

function prefetch() {                     // warm the next cards (the server translates missing lines on demand)
  for (let i = cur + 1; i <= cur + PREFETCH && i < cards.length; i++) {
    const c = cards[i];
    if (!c.subs && !c.subsPromise) ensureSubs(c);
  }
}

// ---------------------------------------------------------------------------
function createCard(item) {
  const card = {
    item, node: null, subsEl: null, sheetHost: null, ctrl: {},
    subs: null, subsPromise: null, subsLoading: false, glossMap: new Map(),
    idx: -1, revealed: false, showTr: false, maxTime: 0, playedSent: false, pairMode: false,
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
    el('button', { text: t.next + ' ›', onclick: () => { sendEvents([{ video_id: item.video_id, type: 'skip', mode: state.settings.mode }]); go(1); } }),
  ]);
  const sheetHost = el('div', { class: 'sheet-host' });
  card.node = el('article', { class: 'card', dataset: { id: item.video_id } }, [head, title, subsEl, ctrl, sheetHost]);
  card.node.hidden = true;
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
// Pager: only the current card and its two neighbours are laid out; the cards follow the finger.
function wirePager() {
  rail.addEventListener('pointerdown', onPointerDown);
  window.addEventListener('pointermove', onPointerMove, { passive: true });
  window.addEventListener('pointerup', onPointerUp);
  window.addEventListener('pointercancel', onPointerCancel);
  rail.addEventListener('click', (e) => { if (suppressClick) { suppressClick = false; e.stopPropagation(); e.preventDefault(); } }, true);
  rail.addEventListener('wheel', onWheel, { passive: false });
  document.addEventListener('keydown', onKey);
}

function layout(dy = 0, animate = false) {
  for (let i = 0; i < cards.length; i++) {
    const node = cards[i].node;
    const off = i - cur;
    if (off < -1 || off > 1) { node.hidden = true; continue; }
    node.hidden = false;
    node.classList.toggle('anim', animate);
    node.style.transform = `translateY(calc(${off * 100}% + ${Math.round(dy)}px))`;
  }
}

function settle() {                       // animate back to rest, then drop the transition class
  layout(0, true);
  clearTimeout(settleTimer);
  settleTimer = setTimeout(() => layout(), ANIM_MS + 20);
}

function show(index, animate = false) {
  cur = index;
  animating = animate;
  layout(0, animate);
  clearTimeout(settleTimer);
  if (animate) settleTimer = setTimeout(() => { animating = false; layout(); prune(); }, ANIM_MS + 20);
  else prune();
  activate(cards[cur]);
}

function go(dir) {                        // +1 next, -1 previous; false when nothing to show yet
  if (animating || !cards.length) return false;
  const target = cur + dir;
  if (target < 0) return false;
  if (target >= cards.length) {
    if (exhausted) toast(t.end_of_feed);
    else loadMore().then((ok) => { if (ok && cur === target - 1 && !animating) show(target, true); });
    return false;
  }
  show(target, true);
  return true;
}

function prune() {                        // keep the DOM small: drop cards far behind the current one
  const extra = cur - KEEP_BEHIND;
  if (extra <= 0) return;
  for (const c of cards.splice(0, extra)) c.node.remove();
  cur -= extra;
}

function scrollableAncestor(node) {
  let n = node instanceof Element ? node : null;
  while (n && n !== rail) {
    if (n.scrollHeight > n.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(n).overflowY)) return n;
    n = n.parentElement;
  }
  return null;
}

function onPointerDown(e) {
  if (!cards.length || animating) return;
  if (e.pointerType === 'mouse' && e.button !== 0) return;
  if (e.target.closest('input, textarea, select')) return;
  drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, y: e.clientY, t: performance.now(), v: 0, mode: null, scrollEl: scrollableAncestor(e.target) };
}

function onPointerMove(e) {
  if (!drag || e.pointerId !== drag.id) return;
  const dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
  if (!drag.mode) {
    if (Math.abs(dx) < SLOP && Math.abs(dy) < SLOP) return;
    if (Math.abs(dx) > Math.abs(dy)) { drag = null; return; }             // horizontal: not ours
    const sc = drag.scrollEl;
    const canScroll = sc && ((dy > 0 && sc.scrollTop > 0) || (dy < 0 && sc.scrollTop + sc.clientHeight < sc.scrollHeight - 1));
    drag.mode = canScroll ? 'scroll' : 'page';
  }
  const now = performance.now();
  const step = e.clientY - drag.y;
  drag.v = 0.6 * drag.v + 0.4 * (step / Math.max(1, now - drag.t));
  drag.y = e.clientY; drag.t = now;
  if (drag.mode === 'scroll') { drag.scrollEl.scrollTop -= step; return; }
  const atEnd = (dy < 0 && cur >= cards.length - 1) || (dy > 0 && cur <= 0);
  layout(atEnd ? dy * 0.3 : dy);                                          // rubber band at both ends
}

function onPointerUp(e) {
  if (!drag || e.pointerId !== drag.id) { flushPending(); return; }
  const d = drag; drag = null;
  if (!d.mode) { flushPending(); return; }                                // a tap: let the click through
  if (e.pointerType === 'mouse') { suppressClick = true; setTimeout(() => { suppressClick = false; }, 50); }
  if (d.mode !== 'page') return;
  const dy = e.clientY - d.y0;
  const h = rail.clientHeight || 1;
  const flick = Math.abs(d.v) > COMMIT_VELOCITY && Math.sign(d.v) === Math.sign(dy);
  if ((Math.abs(dy) > h * COMMIT_RATIO || flick) && go(dy < 0 ? 1 : -1)) return;
  settle();
}

function onPointerCancel(e) {
  if (!drag || e.pointerId !== drag.id) return;
  const d = drag; drag = null;
  if (d.mode === 'page') settle();
}

let wheelAcc = 0, wheelLast = 0, wheelLock = 0;
function onWheel(e) {                     // desktop: one burst of wheel notches = one card
  e.preventDefault();
  const now = performance.now();
  if (now - wheelLast > 250) wheelAcc = 0;
  wheelLast = now;
  wheelAcc += e.deltaY;
  if (now < wheelLock || Math.abs(wheelAcc) < 80) return;
  wheelLock = now + 500;
  const dir = wheelAcc > 0 ? 1 : -1;
  wheelAcc = 0;
  go(dir);
}

function onKey(e) {
  if (!rail || rail.closest('section').hidden) return;
  if (e.target.closest && e.target.closest('input, textarea')) return;
  if (['ArrowDown', 'PageDown', 'j'].includes(e.key)) { e.preventDefault(); go(1); }
  else if (['ArrowUp', 'PageUp', 'k'].includes(e.key)) { e.preventDefault(); go(-1); }
}

// ---------------------------------------------------------------------------
async function activate(card) {
  if (active && active !== card) deactivate(active);
  active = card;
  card.maxTime = 0; card.playedSent = false; card.idx = -1; card.revealed = false; card.showTr = false; card.pairMode = false;
  card.sheetHost.innerHTML = '';
  if (card.subs && card.subs.error) card.subs = null;                     // retry a failed fetch
  if (!started) {
    if (player.isCreated()) player.cue(card.item.video_id);
    renderStart(card);
  } else {
    startPlayback(card);
  }
  sendEvents([{ video_id: card.item.video_id, type: 'impression', mode: state.settings.mode }]);
  maybeLoadMore();
  await ensureSubs(card);
  if (active !== card) return;
  renderPanel(card, card.subsEl);
  if (started) sync.start(card.subs.segments);
  else renderStart(card);
  prefetch();
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
  const hint = el('div', { class: 'hint', text: t.swipe_hint });
  const btn = el('button', { class: 'start-btn', text: t.tap_to_start, onclick: () => {
    started = true;
    btn.remove(); hint.remove();
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
  card.subsEl.prepend(btn, hint);
}

function startPlayback(card) {
  const ok = player.load(card.item.video_id);
  if (!ok) { pendingLoad = card; return; }
  armPlayCheck(card);
}

function flushPending() {                 // runs inside a user gesture (pointerup on the rail)
  if (pendingLoad && pendingLoad === active) {
    const card = pendingLoad; pendingLoad = null;
    if (player.load(card.item.video_id)) armPlayCheck(card);
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
    go(1);
  }
  if (s === player.STATE.PAUSED || s === player.STATE.BUFFERING) active.maxTime = Math.max(active.maxTime, player.getTime() || 0);
}

function onPlayerError(e) {
  if (!active) return;
  console.warn('player error', e.detail);
  sendEvents([{ video_id: active.pairMode ? active.item.pair_video_id : active.item.video_id, type: 'embed_error', value: e.detail }]);
  toast(t.error + ` (YouTube ${e.detail})`);
  setTimeout(() => go(1), 800);
}

function onIndex(idx) {
  if (!active || active.pairMode) return;
  active.idx = idx;
  if (state.settings.reveal_de_on_tap) active.revealed = false;
  active.maxTime = Math.max(active.maxTime, player.getTime() || 0);
  renderPanel(active, active.subsEl);
}

// ---------------------------------------------------------------------------
function ensureSubs(card) {
  if (card.subs) return Promise.resolve(card.subs);
  if (card.subsPromise) return card.subsPromise;
  const lang = state.settings.subtitle_lang;
  const key = `${card.item.video_id}:${lang}`;
  if (subsCache.has(key)) {
    card.subs = subsCache.get(key);
    card.glossMap = buildGlossMap(card.subs.glosses);
    return Promise.resolve(card.subs);
  }
  card.subsLoading = true;
  renderPanel(card, card.subsEl);
  card.subsPromise = getSubtitles(card.item.video_id, lang)
    .then((subs) => { subsCache.set(key, subs); return subs; })
    .catch((e) => { if (e.status !== 401 && card === active) toast(t.offline); return { segments: [], glosses: [], tr_source: null, error: true }; })
    .then((subs) => {
      card.subsLoading = false; card.subsPromise = null;
      if (state.settings.subtitle_lang !== lang) return subs;              // language changed meanwhile: ignore
      card.subs = subs;
      card.glossMap = buildGlossMap(subs.glosses);
      return subs;
    });
  return card.subsPromise;
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
