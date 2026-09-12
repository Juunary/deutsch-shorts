// Tiny DOM/storage helpers.
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'hidden') node.hidden = !!v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

export function storageGet(key, fallback = null) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}
export function storageSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode etc. */ }
}
export function storageDel(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } }

let toastTimer = 0;
export function toast(msg, ms = 2200) {
  const box = document.getElementById('toast');
  if (!box) return;
  box.textContent = msg;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, ms);
}

export function fmtDuration(s) {
  if (s === null || s === undefined) return '';
  const m = Math.floor(s / 60), r = Math.floor(s % 60);
  return `${m}:${String(r).padStart(2, '0')}`;
}

export function debounce(fn, ms) {
  let id = 0;
  return (...args) => { clearTimeout(id); id = setTimeout(() => fn(...args), ms); };
}

export function longPress(node, handler, ms = 500) {
  let timer = 0, startX = 0, startY = 0;
  const cancel = () => { clearTimeout(timer); timer = 0; };
  node.addEventListener('pointerdown', (e) => {
    startX = e.clientX; startY = e.clientY;
    cancel();
    timer = setTimeout(() => { timer = 0; handler(e); }, ms);
  });
  node.addEventListener('pointermove', (e) => { if (Math.abs(e.clientX - startX) > 10 || Math.abs(e.clientY - startY) > 10) cancel(); });
  for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) node.addEventListener(ev, cancel);
  node.addEventListener('contextmenu', (e) => e.preventDefault());
}
