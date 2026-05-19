// content.js — runs in every frame of every page (all_frames: true).
//
// Finds the "best" <video> element in this frame and reports it to the
// background worker. Also, when running in the top frame, reports any large
// player-shaped iframes so the background has a fallback candidate list.

(() => {
  if (window.__castBoosterInstalled) return;
  window.__castBoosterInstalled = true;

  const REPORT_DEBOUNCE_MS = 400;
  let reportTimer = null;

  function describe(video) {
    return {
      src: video.src || '',
      currentSrc: video.currentSrc || '',
      duration: Number.isFinite(video.duration) ? video.duration : 0,
      width: video.videoWidth || video.clientWidth || 0,
      height: video.videoHeight || video.clientHeight || 0,
      paused: !!video.paused,
      frameUrl: location.href,
    };
  }

  function pickBest() {
    const videos = Array.from(document.querySelectorAll('video'));
    if (videos.length === 0) return null;
    let best = null;
    let bestScore = -1;
    for (const v of videos) {
      const w = v.videoWidth || v.clientWidth || 0;
      const h = v.videoHeight || v.clientHeight || 0;
      const area = w * h;
      const playing = !v.paused && v.readyState >= 2;
      const hasDuration = Number.isFinite(v.duration) && v.duration > 0;
      const score = area + (playing ? 1_000_000 : 0) + (hasDuration ? 500_000 : 0);
      if (score > bestScore) { bestScore = score; best = v; }
    }
    return best;
  }

  function scanIframes() {
    if (window.top !== window.self) return [];
    const out = [];
    for (const f of document.querySelectorAll('iframe')) {
      const src = f.src || '';
      if (!/^https?:\/\//i.test(src)) continue;
      const r = f.getBoundingClientRect();
      if (r.width < 400 || r.height < 200) continue;
      let host = '';
      try { host = new URL(src).hostname; } catch (_) {}
      out.push({ src, width: Math.round(r.width), height: Math.round(r.height), host });
    }
    return out;
  }

  function scheduleReport() {
    clearTimeout(reportTimer);
    reportTimer = setTimeout(() => {
      const v = pickBest();
      const iframes = scanIframes();
      try {
        if (v) chrome.runtime.sendMessage({ type: 'VIDEO_DETECTED', payload: describe(v) });
        for (const f of iframes) chrome.runtime.sendMessage({ type: 'PLAYER_IFRAME', payload: f });
      } catch (_) {
        // Extension context invalidated (reload). Ignore.
      }
    }, REPORT_DEBOUNCE_MS);
  }

  const mo = new MutationObserver(() => scheduleReport());
  mo.observe(document.documentElement || document, { childList: true, subtree: true });

  for (const ev of ['play', 'pause', 'loadedmetadata', 'durationchange', 'emptied']) {
    document.addEventListener(ev, scheduleReport, true);
  }

  scheduleReport();
})();
