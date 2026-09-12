// Single YouTube IFrame player, created once, reused for every card. Never hidden or covered.
const bus = new EventTarget();
let player = null;
let apiReady = null;
let currentId = null;
let creating = null;

export const STATE = { UNSTARTED: -1, ENDED: 0, PLAYING: 1, PAUSED: 2, BUFFERING: 3, CUED: 5 };

export function on(type, fn) { bus.addEventListener(type, fn); return () => bus.removeEventListener(type, fn); }

export function ready() {
  if (apiReady) return apiReady;
  apiReady = new Promise((resolve) => {
    if (window.YT && window.YT.Player) return resolve();
    const prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => { if (typeof prev === 'function') prev(); resolve(); };
    // the API script is async; if it fails to load, keep waiting (the UI shows an offline toast)
  });
  return apiReady;
}

// Create the player with a CUED video (no autoplay). The first user tap calls play() synchronously.
export async function create(videoId) {
  if (player) { if (videoId && videoId !== currentId) cue(videoId); return player; }
  if (creating) return creating;
  creating = ready().then(() => new Promise((resolve) => {
    currentId = videoId;
    player = new window.YT.Player('player', {
      videoId,
      host: 'https://www.youtube-nocookie.com',
      playerVars: { playsinline: 1, rel: 0, cc_load_policy: 0, enablejsapi: 1, controls: 1, origin: location.origin },
      events: {
        onReady: () => { bus.dispatchEvent(new CustomEvent('ready')); resolve(player); },
        onStateChange: (e) => bus.dispatchEvent(new CustomEvent('statechange', { detail: e.data })),
        onError: (e) => bus.dispatchEvent(new CustomEvent('error', { detail: e.data })),
      },
    });
  }));
  return creating;
}

export function isCreated() { return !!player; }
export function current() { return currentId; }

export function load(videoId) {
  if (!player) return false;
  currentId = videoId;
  player.loadVideoById(videoId);
  return true;
}
export function cue(videoId) {
  if (!player) return false;
  currentId = videoId;
  player.cueVideoById(videoId);
  return true;
}
export function play() { try { player && player.playVideo(); } catch { /* not ready */ } }
export function pause() { try { player && player.pauseVideo(); } catch { /* not ready */ } }
export function getTime() { try { return player ? player.getCurrentTime() : 0; } catch { return 0; } }
export function getDuration() { try { return player ? player.getDuration() : 0; } catch { return 0; } }
export function state() { try { return player ? player.getPlayerState() : STATE.UNSTARTED; } catch { return STATE.UNSTARTED; } }
export function setRate(rate) { try { player && player.setPlaybackRate(rate); } catch { /* ignore */ } }
