// 설정 view: study settings, topics, channels, Takeout import, pipeline, install, health, token.
import { getHealth, getTopics, getChannels, putChannel, putSettings, importTakeout, runPipeline, pipelineStatus } from './api.js';
import { state, set, setSettings } from './state.js';
import { el, toast, storageDel } from './util.js';
import { t } from './i18n.ko.js';

let installPrompt = null;
window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); installPrompt = e; });

function switchRow(label, key, onChange) {
  const btn = el('button', { class: 'switch' + (state.settings[key] ? ' on' : ''), onclick: async () => {
    const value = !state.settings[key];
    btn.classList.toggle('on', value);
    await save({ [key]: value }, onChange);
  } });
  return el('div', { class: 'field' }, [el('label', { text: label }), btn]);
}

function segRow(label, key, options, onChange) {
  const seg = el('div', { class: 'seg' });
  for (const [value, text] of options) {
    seg.append(el('button', { class: state.settings[key] === value ? 'on' : '', text, onclick: async (e) => {
      for (const b of seg.children) b.classList.remove('on');
      e.currentTarget.classList.add('on');
      await save({ [key]: value }, onChange);
    } }));
  }
  return el('div', { class: 'field' }, [el('label', { text: label }), seg]);
}

async function save(patch, onChange) {
  const prev = { ...state.settings };
  setSettings(patch);
  try { await putSettings(patch); } catch { toast(t.offline); }
  if (onChange) onChange(prev);
}

export async function renderSettings(root, { onChange } = {}) {
  root.replaceChildren(el('h2', { text: t.settings }));
  root.append(
    el('h3', { text: '학습' }),
    segRow(t.mode_listening_full + ' / ' + t.mode_reading_full, 'mode', [['listening', t.mode_listening], ['reading', t.mode_reading]], onChange),
    segRow(t.subtitle_lang, 'subtitle_lang', [['ko', t.korean], ['en', t.english]], onChange),
    segRow(t.speed, 'playback_rate', [[1, '1×'], [0.75, '0.75×']], onChange),
    switchRow(t.stretch, 'stretch', onChange),
    switchRow(t.reveal_de_on_tap, 'reveal_de_on_tap', onChange),
    switchRow(t.prefer_dub, 'prefer_dub', onChange),
  );

  // topics
  const topicsBox = el('div', { class: 'chips' });
  root.append(el('h3', { text: t.topics }), topicsBox);
  try {
    const { items } = await getTopics();
    const chosen = new Set(state.settings.topics || []);
    for (const tp of items) {
      const chip = el('button', { class: 'chip' + (chosen.has(tp.id) ? ' on' : ''), text: tp.label_ko, onclick: async () => {
        chosen.has(tp.id) ? chosen.delete(tp.id) : chosen.add(tp.id);
        chip.classList.toggle('on', chosen.has(tp.id));
        await save({ topics: [...chosen] }, onChange);
      } });
      topicsBox.append(chip);
    }
  } catch { topicsBox.append(el('span', { class: 'muted', text: t.offline })); }

  // channels
  const chanList = el('ul', { class: 'list' });
  root.append(el('h3', { text: t.channels }), chanList);
  try {
    const { items } = await getChannels();
    for (const c of items) {
      const sw = el('button', { class: 'switch' + (c.enabled ? ' on' : ''), onclick: async () => {
        try { const r = await putChannel(c.id, !sw.classList.contains('on')); sw.classList.toggle('on', r.enabled); } catch { toast(t.offline); }
      } });
      chanList.append(el('li', {}, [
        el('div', { class: 'body' }, [el('div', { class: 'lemma', text: c.title || c.handle || c.id }), el('div', { class: 'sub', text: `${c.level_hint || '?'} · ${c.video_count}개 · ${(c.topics || []).join(', ')}` })]),
        sw,
      ]));
    }
  } catch { chanList.append(el('li', {}, [el('span', { class: 'muted', text: t.offline })])); }

  // takeout
  const file = el('input', { type: 'file', accept: '.json,application/json' });
  root.append(el('h3', { text: t.takeout }), el('div', { class: 'muted', text: t.takeout_help }), el('div', { class: 'row' }, [file,
    el('button', { class: 'btn small', text: '가져오기', onclick: async () => {
      if (!file.files[0]) return;
      try { const r = await importTakeout(file.files[0]); toast(t.takeout_done(r.titles_used)); const s = await (await import('./api.js')).getSettings(); set({ settings: s }); } catch (e) { toast(e.detail || t.error); }
    } })]));

  // pipeline
  const runs = el('div', { class: 'runs', text: '' });
  const refreshRuns = async () => {
    try {
      const st = await pipelineStatus();
      runs.textContent = (st.running ? '실행 중…\n' : '') + st.runs.slice(0, 8).map((r) => `${(r.started_at || '').slice(0, 16)} ${r.stage} ok=${r.ok} failed=${r.failed} units=${r.quota_units}`).join('\n');
    } catch { runs.textContent = t.offline; }
  };
  root.append(el('h3', { text: '파이프라인' }), el('div', { class: 'row' }, [
    el('button', { class: 'btn small primary', text: t.run_pipeline, onclick: async () => {
      try { await runPipeline('all'); toast(t.pipeline_started); setTimeout(refreshRuns, 1500); } catch (e) { toast(e.status === 409 ? t.pipeline_running : (e.detail || t.error)); }
    } }),
    el('button', { class: 'btn small', text: t.refresh, onclick: refreshRuns }),
  ]), runs);
  refreshRuns();

  // health + install + token
  const health = el('div', { class: 'muted', text: '…' });
  root.append(el('h3', { text: t.health }), health);
  try {
    const h = await getHealth();
    health.textContent = `${t.version} ${h.version} · ${t.llm_backend} ${h.llm_backend} (${h.llm_ready ? t.llm_ready : t.llm_not_ready})\n${t.counts(h.counts)}`;
    health.style.whiteSpace = 'pre-wrap';
  } catch { health.textContent = t.offline; }

  const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent);
  const row = el('div', { class: 'row', style: 'margin-top:16px' });
  if (!isIOS) {
    row.append(el('button', { class: 'btn small', text: t.install, hidden: !installPrompt, onclick: async () => { if (!installPrompt) return; installPrompt.prompt(); installPrompt = null; } }));
  }
  row.append(el('button', { class: 'btn small danger', text: t.reset_token, onclick: () => { storageDel('ds_token'); set({ token: null }); location.reload(); } }));
  root.append(row);
  if (isIOS) root.append(el('div', { class: 'muted', style: 'margin-top:8px', text: 'iPhone에서는 Safari 탭으로 사용하세요 (홈 화면 추가 시 유튜브 재생이 막히는 문제가 보고됨).' }));
}
