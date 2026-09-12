// 단어장 view.
import { getVocab, deleteVocab, vocabExportUrl } from './api.js';
import { state, set } from './state.js';
import { el, toast } from './util.js';
import { t } from './i18n.ko.js';

export async function renderVocab(root) {
  root.replaceChildren(el('h2', { text: t.vocab }), el('div', { class: 'muted', text: '불러오는 중…' }));
  let items = [];
  try {
    items = (await getVocab()).items;
  } catch (e) {
    root.replaceChildren(el('h2', { text: t.vocab }), el('div', { class: 'muted', text: t.offline }));
    return;
  }
  set({ vocabCount: items.length });
  const list = el('ul', { class: 'list' });
  for (const v of items) {
    const li = el('li', {}, [
      el('div', { class: 'body' }, [
        el('div', {}, [el('span', { class: 'lemma', text: v.lemma }), v.pos ? el('span', { class: 'pos muted', text: ` · ${t.pos[v.pos] || v.pos}` }) : null]),
        el('div', { text: [v.gloss_ko, v.gloss_en].filter(Boolean).join(' · ') }),
        v.sentence_de ? el('div', { class: 'sub', text: v.sentence_de }) : null,
        v.sentence_ko ? el('div', { class: 'sub', text: v.sentence_ko }) : null,
      ]),
      el('button', { class: 'btn small danger', text: t.delete, onclick: async () => {
        try { await deleteVocab(v.id); li.remove(); set({ vocabCount: Math.max(0, state.vocabCount - 1) }); } catch { toast(t.offline); }
      } }),
    ]);
    list.append(li);
  }
  root.replaceChildren(
    el('h2', { text: `${t.vocab} (${items.length})` }),
    el('div', { class: 'row' }, [
      el('a', { class: 'btn primary', href: vocabExportUrl(), download: 'vocab.tsv', text: t.export_anki }),
      el('span', { class: 'muted', text: 'Anki → 파일 → 가져오기 (탭 구분, HTML 허용)' }),
    ]),
    items.length ? list : el('div', { class: 'muted', text: '아직 저장한 단어가 없어요. 자막의 단어를 탭해 보세요.' }),
  );
}
