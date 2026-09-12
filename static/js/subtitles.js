// Pure helpers for subtitle timing + the sync loop that follows the player clock.

// Last index whose start_ms <= tMs, or -1 before the first cue.
export function findIndex(startsMs, tMs) {
  let lo = 0, hi = startsMs.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (startsMs[mid] <= tMs) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

// Split a German line into word / non-word tokens. Letters incl. umlauts, inner hyphens/apostrophes stay inside words.
const WORD_RE = /[A-Za-zÄÖÜäöüß0-9]+(?:['’-][A-Za-zÄÖÜäöüß0-9]+)*/g;
export function tokenize(text) {
  const out = [];
  let last = 0;
  const s = String(text || '');
  for (const m of s.matchAll(WORD_RE)) {
    if (m.index > last) out.push({ w: s.slice(last, m.index), word: false });
    out.push({ w: m[0], word: true });
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push({ w: s.slice(last), word: false });
  return out;
}

// Estimate the player time between 250 ms samples so subtitles switch smoothly.
// playerApi: { getTime(): seconds, state(): number }, PLAYING = 1.
export function createSync(playerApi, onIndex, opts = {}) {
  const sampleEvery = opts.sampleEvery ?? 250;
  const nowFn = opts.now || (() => performance.now());
  const raf = opts.raf || ((fn) => requestAnimationFrame(fn));
  const caf = opts.caf || ((id) => cancelAnimationFrame(id));
  let starts = [], anchor = null, lastSample = -Infinity, rate = 1, active = -2, running = false, rafId = 0;

  function estimate(now) {
    if (!anchor) return 0;
    if (playerApi.state() !== 1) return anchor.t;
    return anchor.t + ((now - anchor.now) / 1000) * rate;
  }
  function sample(now) {
    const t = Number(playerApi.getTime());
    if (!Number.isFinite(t)) return;
    anchor = { t, now };
    lastSample = now;
  }
  function step() {
    if (!running) return;
    const now = nowFn();
    if (now - lastSample >= sampleEvery) sample(now);
    const idx = findIndex(starts, Math.round(estimate(now) * 1000));
    if (idx !== active) { active = idx; onIndex(idx); }
    rafId = raf(step);
  }
  return {
    start(segments) {
      starts = segments.map((s) => s.start_ms);
      anchor = null; lastSample = -Infinity; active = -2; running = true;
      step();
    },
    stop() { running = false; if (rafId) caf(rafId); rafId = 0; },
    setRate(r) { rate = r; anchor = null; },
    reanchor() { anchor = null; lastSample = -Infinity; },
    tick: step,             // exposed for tests
    get active() { return active; },
  };
}
