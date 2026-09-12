// Gloss map + German line rendering (plain spans or <ruby> in reading mode) + the word sheet.
import { tokenize } from './subtitles.js';
import { el, escapeHtml } from './util.js';
import { t } from './i18n.ko.js';

export function buildGlossMap(glosses) {
  const map = new Map();
  for (const g of glosses || []) {
    const key = String(g.surface || '').toLowerCase();
    if (key && !map.has(key)) map.set(key, g);
  }
  return map;
}

export function lookupGloss(map, word) {
  const w = String(word || '').toLowerCase();
  return map.get(w) || map.get(w.replace(/^[^a-zäöüß0-9]+|[^a-zäöüß0-9]+$/g, '')) || null;
}

// Returns an HTML string; glossed words get class "g" (and <ruby> when ruby=true).
export function renderGermanLine(text, map, { ruby = false, lang = 'ko' } = {}) {
  let html = '';
  for (const tok of tokenize(text)) {
    if (!tok.word) { html += escapeHtml(tok.w); continue; }
    const g = lookupGloss(map, tok.w);
    const safe = escapeHtml(tok.w);
    if (g && ruby) {
      const rt = escapeHtml(lang === 'en' ? (g.en || g.ko || '') : (g.ko || g.en || ''));
      html += `<ruby class="w g" data-w="${safe}">${safe}<rt>${rt}</rt></ruby>`;
    } else {
      html += `<span class="w${g ? ' g' : ''}" data-w="${safe}">${safe}</span>`;
    }
  }
  return html;
}

// Build the bottom-sheet content for a tapped word.
export function wordSheet({ word, gloss, sentenceDe, sentenceTr, videoId, segIdx, onSave, onWrong, onClose }) {
  const lemma = gloss ? gloss.lemma : word;
  const q = encodeURIComponent(gloss ? gloss.lemma.replace(/^(der|die|das)\s+/i, '').replace(/,.*$/, '') : word);
  const naver = `https://dict.naver.com/dekodict/#/search?query=${q}`;
  const meaning = gloss ? [gloss.ko, gloss.en].filter(Boolean).join(' · ') : '';
  const sheet = el('div', { class: 'sheet' }, [
    el('div', {}, [el('span', { class: 'lemma', text: lemma }), gloss ? el('span', { class: 'pos', text: `${t.pos[gloss.pos] || gloss.pos}${gloss.level ? ' · ' + gloss.level : ''}` }) : null]),
    meaning ? el('div', { class: 'meaning', text: meaning }) : el('div', { class: 'meaning muted', text: '뜻 정보가 없어요. 사전에서 찾아보세요.' }),
    sentenceDe ? el('div', { class: 'sentence', text: sentenceDe }) : null,
    el('div', { class: 'row' }, [
      el('button', { class: 'btn small primary', text: t.save_word, onclick: (e) => onSave(e.currentTarget) }),
      gloss ? el('button', { class: 'btn small', text: t.wrong_gloss, onclick: () => onWrong() }) : null,
      el('a', { class: 'btn small', href: naver, target: '_blank', rel: 'noopener', text: t.naver_dict }),
      el('button', { class: 'btn small', text: t.close, onclick: () => onClose() }),
    ]),
  ]);
  return sheet;
}
