// First-run: pick topics, mode and subtitle language.
import { getTopics, putSettings } from './api.js';
import { state, set } from './state.js';
import { el, toast } from './util.js';
import { t } from './i18n.ko.js';

export async function renderOnboarding(root, onDone) {
  const chosen = new Set(state.settings.topics || []);
  let mode = state.settings.mode || 'listening';
  let lang = state.settings.subtitle_lang || 'ko';
  const chips = el('div', { class: 'chips' });
  try {
    const { items } = await getTopics();
    for (const tp of items) {
      const chip = el('button', { class: 'chip' + (chosen.has(tp.id) ? ' on' : ''), text: tp.label_ko, onclick: () => {
        chosen.has(tp.id) ? chosen.delete(tp.id) : chosen.add(tp.id);
        chip.classList.toggle('on', chosen.has(tp.id));
      } });
      chips.append(chip);
    }
  } catch { chips.append(el('span', { class: 'muted', text: t.offline })); }

  const seg = (options, current, setter) => {
    const box = el('div', { class: 'seg' });
    for (const [value, text] of options) {
      box.append(el('button', { class: current === value ? 'on' : '', text, onclick: (e) => { for (const b of box.children) b.classList.remove('on'); e.currentTarget.classList.add('on'); setter(value); } }));
    }
    return box;
  };
  root.replaceChildren(
    el('h2', { text: t.onboarding_title }),
    el('div', { class: 'muted', text: t.onboarding_help }),
    chips,
    el('div', { class: 'field' }, [el('label', { text: t.mode_listening_full + ' / ' + t.mode_reading_full }), seg([['listening', t.mode_listening], ['reading', t.mode_reading]], mode, (v) => { mode = v; })]),
    el('div', { class: 'field' }, [el('label', { text: t.subtitle_lang }), seg([['ko', t.korean], ['en', t.english]], lang, (v) => { lang = v; })]),
    el('button', { class: 'btn primary', style: 'margin-top:16px;width:100%', text: t.start, onclick: async () => {
      if (!chosen.size) { toast(t.onboarding_help); return; }
      try {
        const s = await putSettings({ topics: [...chosen], mode, subtitle_lang: lang, onboarded: true });
        set({ settings: s });
        onDone();
      } catch { toast(t.offline); }
    } }),
  );
}
