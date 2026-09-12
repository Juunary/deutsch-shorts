// Renders the subtitle panel of a card for the current mode + active segment.
import { renderGermanLine } from './gloss.js';
import { el, escapeHtml } from './util.js';
import { t } from './i18n.ko.js';

// card: { subs: {segments, glosses, tr_source}, glossMap, idx, revealed, showTr, settings }
export function renderPanel(card, subsEl) {
  const { settings } = card;
  const segs = card.subs ? card.subs.segments : null;
  subsEl.innerHTML = '';
  if (card.subsLoading) { subsEl.append(el('div', { class: 'empty', text: t.loading_subs })); return; }
  if (!segs || !segs.length) { subsEl.append(el('div', { class: 'empty', text: t.no_subs })); return; }
  const idx = card.idx;
  if (idx < 0) { subsEl.append(el('div', { class: 'empty', text: t.waiting })); return; }
  const seg = segs[idx];
  const de = seg.de_clean || seg.de;
  const tr = seg.tr;
  const ruby = settings.mode === 'reading';

  if (settings.mode === 'listening') {
    subsEl.append(el('div', { class: 'subs-tr', text: tr || t.no_subs, dataset: { idx: String(idx) } }));
    const revealed = card.revealed || !settings.reveal_de_on_tap;
    if (revealed) {
      subsEl.append(el('div', { class: 'subs-de', html: renderGermanLine(de, card.glossMap, { ruby: false, lang: settings.subtitle_lang }) }));
    } else {
      subsEl.append(el('button', { class: 'reveal', text: t.show_de, onclick: () => { card.revealed = true; renderPanel(card, subsEl); } }));
    }
  } else {
    const prev = idx > 0 ? (segs[idx - 1].de_clean || segs[idx - 1].de) : '';
    const next = idx + 1 < segs.length ? (segs[idx + 1].de_clean || segs[idx + 1].de) : '';
    if (prev) subsEl.append(el('div', { class: 'subs-ctx prev', text: prev }));
    subsEl.append(el('div', { class: 'subs-de', html: renderGermanLine(de, card.glossMap, { ruby, lang: settings.subtitle_lang }), dataset: { idx: String(idx) } }));
    if (card.showTr) subsEl.append(el('div', { class: 'subs-tr', text: tr || t.no_subs, dataset: { idx: String(idx) } }));
    else subsEl.append(el('button', { class: 'reveal', text: t.show_tr, onclick: () => { card.showTr = true; renderPanel(card, subsEl); } }));
    if (next) subsEl.append(el('div', { class: 'subs-ctx next', text: next }));
  }
  const src = card.subs.tr_source;
  if (src && src !== 'model') subsEl.append(el('div', { class: 'source', text: t.level_source[src] || src }));
}

// Correction editor (long-press on a translation line).
export function correctionEditor({ current, onSave, onCancel }) {
  const ta = el('textarea', { text: current || '' });
  return el('div', { class: 'sheet' }, [
    el('div', { class: 'muted', text: t.fix_translation }),
    ta,
    el('div', { class: 'row' }, [
      el('button', { class: 'btn small primary', text: t.save, onclick: () => onSave(ta.value.trim()) }),
      el('button', { class: 'btn small', text: t.cancel, onclick: () => onCancel() }),
    ]),
  ]);
}

export function dubSheet({ pairId, onWatchPair, onReport, onClose }) {
  return el('div', { class: 'sheet' }, [
    el('div', { class: 'meaning', text: t.dub_help }),
    el('div', { class: 'row' }, [
      pairId ? el('button', { class: 'btn small primary', text: t.watch_en, onclick: () => onWatchPair() }) : null,
      el('button', { class: 'btn small', text: t.no_dub_report, onclick: (e) => { onReport(); e.currentTarget.textContent = t.reported; e.currentTarget.disabled = true; } }),
      el('button', { class: 'btn small', text: t.close, onclick: () => onClose() }),
    ]),
  ]);
}

export function sourceLabel(src) { return escapeHtml(t.level_source[src] || src || ''); }
