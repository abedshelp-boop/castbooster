const statusEl = document.getElementById('status');
const hintEl = document.getElementById('hint');
const heroArea = document.getElementById('heroArea');
const deviceArea = document.getElementById('deviceArea');
const actionRow = document.getElementById('actionRow');
const castBtn = document.getElementById('castBtn');
const castBtnLabel = castBtn.querySelector('.cb-btn-label');
const resultEl = document.getElementById('result');

// Custom "cast to" dropdown
const devicesDD = document.getElementById('devicesDD');
const devicesTrigger = document.getElementById('devicesTrigger');
const devicesLabel = document.getElementById('devicesLabel');
const devicesMenu = document.getElementById('devicesMenu');

// Picker state — what the user has selected (or auto-chosen)
let selectedStreamUrl = '';      // chosen candidate URL (picked silently — no dropdown)
let selectedDeviceUuid = '';     // chosen Chromecast UUID
let availableDevices = [];       // [{ uuid, name, model }, ...]
let haveStream = false;          // true once we have at least one candidate

// Player UI
const playerArea = document.getElementById('playerArea');
const mcTitle = document.getElementById('mcTitle');
const mcDevice = document.getElementById('mcDevice');
const mcResBadge = document.getElementById('mcResBadge');
const mcDeviceNameStage = document.getElementById('mcDeviceNameStage');
const mcStage = document.getElementById('mcStage');
const mcStageNote = document.getElementById('mcStageNote');
const mcCurrent = document.getElementById('mcCurrent');
const mcDuration = document.getElementById('mcDuration');
const mcScrubber = document.getElementById('mcScrubber');
const mcBack10 = document.getElementById('mcBack10');
const mcPlayPause = document.getElementById('mcPlayPause');
const mcFwd10 = document.getElementById('mcFwd10');
const mcVolDown = document.getElementById('mcVolDown');
const mcVolUp = document.getElementById('mcVolUp');
const mcVolReadout = document.getElementById('mcVolReadout');
const mcStop = document.getElementById('mcStop');

let currentTabId = null;
let currentTabUrl = '';
let allCandidates = [];

// Player state
let activeCast = null;      // { castUuid, deviceName, ... } from chrome.storage
let lastStatus = null;      // last media_status_result from the app
let pollTimer = null;
let isDragging = false;     // suppress scrubber updates while user drags
let lastPlayPauseAt = 0;    // debounce rapid toggles
let currentVolume = 0.5;    // tracked locally so +/- can step from a known value
let volReadoutTimer = null; // fade-out timer for the % readout
let lastOptimisticSeekAt = 0;  // suppresses scrubber snap-back after skip
let frameBlocked = false;   // source tab's <video> canvas is tainted; stop polling frames
let lastFrameDataUrl = '';  // last good frame; reused if a poll returns idle/blocked

// Copy of the ad-CDN check from background.js — popup needs it client-side to
// label ad hosts in the dropdown.
const AD_CDN_PATTERNS = [
  /(^|\.)tiktokcdn\.com$/i,
  /(^|\.)doubleclick\.net$/i,
  /(^|\.)googlesyndication\.com$/i,
  /(^|\.)adsafeprotected\.com$/i,
  /(^|\.)amazon-adsystem\.com$/i,
  /(^|\.)2mdn\.net$/i,
  /(^|\.)googleadservices\.com$/i,
  /(^|\.)moatads\.com$/i,
  /(^|\.)scorecardresearch\.com$/i,
  /(^|\.)adsrvr\.org$/i,
];
function isAdHost(url) {
  try {
    const h = new URL(url).hostname;
    return AD_CDN_PATTERNS.some((re) => re.test(h));
  } catch (_) { return false; }
}

function setStatus(kind, text) {
  statusEl.textContent = text;
  statusEl.classList.remove('ok', 'err');
  if (kind) statusEl.classList.add(kind);
}

function setHint(html) { hintEl.innerHTML = html || ''; }

function setResult(kind, text) {
  resultEl.textContent = text;
  resultEl.classList.remove('ok', 'err');
  if (kind) resultEl.classList.add(kind);
}

function prettyKind(type, url) {
  if (type === 'iframe') return 'Player page';
  if (type === 'm3u8') {
    if (/\.urlset\/master/i.test(url)) return 'HLS master';
    if (/\.urlset\/index-/i.test(url)) return 'HLS variant';
    if (/\/seg|\/chunk|\/frag|_\d+\.m3u8|-\d+\.m3u8/i.test(url)) return 'HLS fragment';
    return 'HLS master';
  }
  if (type === 'mpd') return 'DASH manifest';
  if (type === 'mp4') return 'MP4';
  if (type === 'webm') return 'WebM';
  if (type === 'mkv') return 'MKV';
  if (type === 'ts') return 'HLS chunk';
  if (type === 'm4s') return 'DASH chunk';
  if (type === 'video') return 'Video';
  return type || 'video';
}

function humanBytes(n) {
  if (!n) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function truncate(s, n) { return s.length <= n ? s : s.slice(0, n - 1) + '…'; }

function guessType(url) {
  const m = /\.(m3u8|mpd|mp4|webm|m4s|ts)(\?|$)/i.exec(url);
  if (m) return m[1].toLowerCase();
  if (/\.urlset\/(master|index)[\w\-.]*\.(txt|m3u8)/i.test(url)) return 'm3u8';
  return 'video';
}

function buildCandidates(state) {
  const out = [];
  const seen = new Set();

  for (const f of state.iframes || []) {
    if (seen.has(f.src)) continue;
    seen.add(f.src);
    out.push({
      url: f.src,
      label: `Player page (${f.host || 'iframe'}) • fallback`,
      type: 'iframe',
      score: 40,
    });
  }

  if (state.dom) {
    const domUrl = state.dom.currentSrc || state.dom.src;
    if (domUrl && !domUrl.startsWith('blob:') && !domUrl.startsWith('data:') && !seen.has(domUrl)) {
      const type = guessType(domUrl);
      seen.add(domUrl);
      out.push({
        url: domUrl,
        label: `${prettyKind(type, domUrl)} • from <video>`,
        type,
        score: 120,
      });
    }
  }

  for (const cap of state.captures || []) {
    if (seen.has(cap.url)) continue;
    if (cap.type === 'ts' || cap.type === 'm4s') continue;
    seen.add(cap.url);
    const sizeStr = cap.contentLength ? ' • ' + humanBytes(cap.contentLength) : '';
    const adWarn = isAdHost(cap.url) ? ' ⚠ ad CDN' : '';
    out.push({
      url: cap.url,
      label: `${prettyKind(cap.type, cap.url)}${sizeStr} • sniffed${adWarn}`,
      type: cap.type,
      score: cap.score,
      isAd: isAdHost(cap.url),
    });
  }

  out.sort((a, b) => b.score - a.score);
  return out;
}

function renderCandidates(list) {
  if (list.length === 0) {
    haveStream = false;
    selectedStreamUrl = '';
    actionRow.hidden = true;
    if (heroArea) heroArea.hidden = false;
    return;
  }
  // Pick the best candidate silently: first non-ad, fallback to first overall.
  const firstReal = list.find((c) => !c.isAd) || list[0];
  selectedStreamUrl = firstReal.url;
  haveStream = true;
  if (heroArea) heroArea.hidden = true;
}

function renderDevices(devices) {
  availableDevices = Array.isArray(devices) ? devices : [];
  devicesMenu.innerHTML = '';

  if (availableDevices.length === 0) {
    deviceArea.hidden = true;
    selectedDeviceUuid = '';
    setDevicesLabel('Select a Chromecast…', true);
    return;
  }
  deviceArea.hidden = false;

  // Preserve a prior selection if it's still in the list; otherwise pick first.
  if (!availableDevices.some((d) => d.uuid === selectedDeviceUuid)) {
    selectedDeviceUuid = availableDevices[0].uuid;
  }

  for (const d of availableDevices) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'cb-dd-item';
    item.setAttribute('role', 'option');
    item.dataset.uuid = d.uuid;
    if (d.uuid === selectedDeviceUuid) item.classList.add('is-selected');

    const tv = document.createElement('span');
    tv.className = 'cb-dd-tv';
    tv.setAttribute('aria-hidden', 'true');

    const text = document.createElement('span');
    text.className = 'cb-dd-text';
    const nameEl = document.createElement('div');
    nameEl.className = 'cb-dd-name';
    nameEl.textContent = d.name || d.uuid;
    text.appendChild(nameEl);
    if (d.model) {
      const metaEl = document.createElement('div');
      metaEl.className = 'cb-dd-meta';
      metaEl.textContent = d.model;
      text.appendChild(metaEl);
    }

    const check = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    check.setAttribute('class', 'cb-dd-check');
    check.setAttribute('width', '12');
    check.setAttribute('height', '12');
    check.setAttribute('viewBox', '0 0 24 24');
    check.setAttribute('fill', 'none');
    check.setAttribute('stroke', 'currentColor');
    check.setAttribute('stroke-width', '3');
    check.setAttribute('stroke-linecap', 'round');
    check.setAttribute('stroke-linejoin', 'round');
    check.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M5 12.5l4.5 4.5L19 7.5');
    check.appendChild(path);

    item.appendChild(tv);
    item.appendChild(text);
    item.appendChild(check);

    item.addEventListener('click', () => {
      selectDevice(d.uuid);
      closeDevicesMenu();
      devicesTrigger.focus();
    });

    devicesMenu.appendChild(item);
  }

  syncDevicesLabel();
}

function setDevicesLabel(text, isPlaceholder) {
  devicesLabel.textContent = text;
  devicesLabel.classList.toggle('is-placeholder', !!isPlaceholder);
}

function syncDevicesLabel() {
  const d = availableDevices.find((x) => x.uuid === selectedDeviceUuid);
  if (!d) {
    setDevicesLabel('Select a Chromecast…', true);
    return;
  }
  const model = d.model ? `  ·  ${d.model}` : '';
  setDevicesLabel(truncate((d.name || d.uuid) + model, 60), false);
}

function selectDevice(uuid) {
  selectedDeviceUuid = uuid;
  for (const item of devicesMenu.querySelectorAll('.cb-dd-item')) {
    item.classList.toggle('is-selected', item.dataset.uuid === uuid);
  }
  syncDevicesLabel();
  refreshActionRow();
}

function openDevicesMenu() {
  if (availableDevices.length === 0) return;
  devicesDD.dataset.open = 'true';
  devicesTrigger.setAttribute('aria-expanded', 'true');
  // Focus the selected item (or first) for keyboard nav.
  const target = devicesMenu.querySelector('.cb-dd-item.is-selected')
    || devicesMenu.querySelector('.cb-dd-item');
  if (target) target.focus({ preventScroll: true });
}

function closeDevicesMenu() {
  devicesDD.dataset.open = 'false';
  devicesTrigger.setAttribute('aria-expanded', 'false');
}

function toggleDevicesMenu() {
  if (devicesDD.dataset.open === 'true') closeDevicesMenu();
  else openDevicesMenu();
}

devicesTrigger.addEventListener('click', toggleDevicesMenu);
devicesTrigger.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    openDevicesMenu();
  }
});

devicesMenu.addEventListener('keydown', (e) => {
  const items = Array.from(devicesMenu.querySelectorAll('.cb-dd-item'));
  if (items.length === 0) return;
  const activeIdx = items.indexOf(document.activeElement);
  if (e.key === 'Escape') {
    e.preventDefault();
    closeDevicesMenu();
    devicesTrigger.focus();
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    items[Math.min(items.length - 1, activeIdx + 1)]?.focus();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    items[Math.max(0, activeIdx - 1)]?.focus();
  } else if (e.key === 'Home') {
    e.preventDefault();
    items[0]?.focus();
  } else if (e.key === 'End') {
    e.preventDefault();
    items[items.length - 1]?.focus();
  } else if (e.key === 'Enter' || e.key === ' ') {
    if (document.activeElement && document.activeElement.classList.contains('cb-dd-item')) {
      e.preventDefault();
      document.activeElement.click();
    }
  }
});

document.addEventListener('click', (e) => {
  if (devicesDD.dataset.open !== 'true') return;
  if (!devicesDD.contains(e.target)) closeDevicesMenu();
});

function refreshActionRow() {
  const haveDevice = !!selectedDeviceUuid;
  const ready = haveStream && haveDevice;
  actionRow.hidden = !ready;
  castBtn.disabled = !ready;
}

castBtn.addEventListener('click', async () => {
  const url = selectedStreamUrl;
  const castUuid = selectedDeviceUuid;
  if (!url || !castUuid) return;
  castBtn.disabled = true;
  const origLabel = castBtnLabel ? castBtnLabel.textContent : castBtn.textContent;
  if (castBtnLabel) castBtnLabel.textContent = 'Casting…'; else castBtn.textContent = 'Casting…';
  setResult('', '');
  try {
    const out = await chrome.runtime.sendMessage({
      type: 'CAST_NOW',
      tabId: currentTabId,
      tabUrl: currentTabUrl,
      url,
      castUuid,
      fallbackUserAgent: navigator.userAgent,
    });
    if (!out || !out.ok) {
      setResult('err', 'Failed: ' + (out && out.error ? out.error : 'unknown error'));
    } else if (out.cast && out.cast.type === 'casting' && out.cast.status === 'ok') {
      setResult('ok', out.cast.detail || 'Playback started');
      // Background has already stashed activeCast — switch the popup into
      // player mode right away so the user doesn't have to close+reopen.
      await init();
      return;
    } else if (out.cast && out.cast.type === 'casting' && out.cast.status === 'error') {
      setResult('err', 'Cast error: ' + out.cast.detail);
    } else {
      setResult('err', 'Unexpected reply: ' + JSON.stringify(out.cast || out.register || out));
    }
  } catch (e) {
    setResult('err', 'Failed: ' + String(e && e.message ? e.message : e));
  } finally {
    castBtn.disabled = false;
    if (castBtnLabel) castBtnLabel.textContent = origLabel; else castBtn.textContent = origLabel;
  }
});

async function sendNM(payload) {
  const resp = await chrome.runtime.sendMessage({ type: 'NM_SEND', payload });
  if (!resp) throw new Error('background worker unreachable');
  if (!resp.ok) throw new Error(resp.error || 'nm failed');
  return resp.resp;
}

// ---------------------------------------------------------------------------
// Player mode
// ---------------------------------------------------------------------------

function formatTime(sec) {
  if (!sec || !isFinite(sec) || sec < 0) return '0:00';
  const s = Math.floor(sec);
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const pad = (n) => n.toString().padStart(2, '0');
  if (hh > 0) return `${hh}:${pad(mm)}:${pad(ss)}`;
  return `${mm}:${pad(ss)}`;
}

function showPlayerMode() {
  playerArea.hidden = false;
  if (heroArea) heroArea.hidden = true;
  deviceArea.hidden = true;
  actionRow.hidden = true;
  // Result block + picker hints are noise once the player is showing.
  resultEl.style.display = 'none';
  setHint('');
  // Light up the ambient broadcast bias-light under the popup.
  document.body.classList.add('broadcasting');
  // Fresh session — clear any stage frame state from a previous cast.
  resetStageFrame();
}

function showPickerMode() {
  playerArea.hidden = true;
  document.body.classList.remove('broadcasting');
  stopPolling();
  resetStageFrame();
}

function resetStageFrame() {
  frameBlocked = false;
  lastFrameDataUrl = '';
  if (mcStage) {
    mcStage.style.backgroundImage = '';
    mcStage.style.backgroundSize = '';
    mcStage.style.backgroundPosition = '';
    delete mcStage.dataset.hasFrame;
  }
  if (mcStageNote) mcStageNote.hidden = true;
}

// Map a media_status payload onto the single-stage player UI. Only the
// primary play/pause button glyph changes between states — the stage, title,
// scrubber and rest of the chrome stay visually static.
function updatePlayerUi(status) {
  lastStatus = status;
  const dead = isDeadSession(status);
  const state = (status.state || '').toUpperCase();
  const title = status.title || (activeCast && activeCast.upstreamUrl) || 'Casting';
  const device = status.deviceName || (activeCast && activeCast.deviceName) || '';
  const host = (() => {
    try { return new URL(activeCast && activeCast.upstreamUrl || '').hostname; }
    catch (_) { return ''; }
  })();
  const resLabel = status.resolutionLabel || '1080p';

  mcTitle.textContent = title;
  mcDevice.textContent = dead
    ? (device ? `Playback error on ${device}` : 'Playback error')
    : (host ? `${host}` : (state ? state.toLowerCase() : 'casting'));

  if (mcResBadge) mcResBadge.textContent = `${resLabel} · FORCED`;
  if (mcDeviceNameStage) mcDeviceNameStage.textContent = device || '—';

  // Volume — keep our local mirror in sync if the app reports it.
  if (typeof status.volume === 'number' && !Number.isNaN(status.volume)) {
    currentVolume = Math.max(0, Math.min(1, status.volume));
  }

  const duration = Number(status.duration) || 0;
  const current = Number(status.currentTime) || 0;
  const sinceOptimistic = Date.now() - lastOptimisticSeekAt;
  const skipScrubberWrite = sinceOptimistic < 800;

  if (!isDragging && !skipScrubberWrite) {
    mcCurrent.textContent = formatTime(current);
    if (duration > 0) {
      mcDuration.textContent = formatTime(duration);
      mcScrubber.max = String(duration);
      mcScrubber.value = String(Math.min(current, duration));
      mcScrubber.disabled = dead || !status.canSeek;
      const pct = duration > 0 ? Math.min(100, Math.max(0, (current / duration) * 100)) : 0;
      mcScrubber.style.setProperty('--cb-fill', pct.toFixed(2) + '%');
    } else {
      mcDuration.textContent = '--:--';
      mcScrubber.value = '0';
      mcScrubber.disabled = true;
      mcScrubber.style.setProperty('--cb-fill', '0%');
    }
  } else if (!isDragging && skipScrubberWrite && duration > 0) {
    // Keep the duration/disable flags fresh even while we lock the position.
    mcDuration.textContent = formatTime(duration);
    mcScrubber.max = String(duration);
    mcScrubber.disabled = dead || !status.canSeek;
  }

  mcBack10.disabled = dead;
  mcFwd10.disabled = dead;
  mcPlayPause.disabled = dead;
  mcVolDown.disabled = dead;
  mcVolUp.disabled = dead;
  mcStop.title = dead ? 'Dismiss' : 'Stop casting';
  mcStop.setAttribute('aria-label', dead ? 'Dismiss' : 'Stop casting');

  // Glyph on the primary play/pause button. CSS swaps the visible glyph
  // based on [data-glyph]. Everything else on screen stays put.
  let glyph = 'play';
  if (dead) {
    glyph = 'err';
    mcPlayPause.title = 'Playback ended';
  } else if (state === 'PLAYING') {
    glyph = 'pause';
    mcPlayPause.title = 'Pause';
  } else if (state === 'BUFFERING') {
    glyph = 'wait';
    mcPlayPause.title = 'Buffering';
  } else {
    glyph = 'play';
    mcPlayPause.title = 'Play';
  }
  mcPlayPause.dataset.glyph = glyph;
  // data-state is decorative — left for future hooks but doesn't drive visibility.
  playerArea.dataset.state = dead ? 'dead' : (state === 'BUFFERING' ? 'buffering' : (state === 'PLAYING' ? 'playing' : 'paused'));
}

// ---------------------------------------------------------------------------
// Live frame painting (source-tab <video> → stage background)
// ---------------------------------------------------------------------------

async function paintStageFromSourceTab() {
  if (frameBlocked) return;
  const tabId = activeCast && activeCast.sourceTabId;
  if (!tabId) return;
  // Target the specific frame that owns the <video> when we know it. Without
  // this, chrome.tabs.sendMessage broadcasts to every frame and the first
  // sendResponse wins — on iframe-player sites (e.g. egydead → streamtape)
  // the top frame's "no video" idle response races ahead of the iframe's
  // real frame, so we never paint anything.
  const frameId = activeCast && activeCast.sourceFrameId;
  const hasFrameId = typeof frameId === 'number' && frameId >= 0;
  let resp;
  try {
    resp = hasFrameId
      ? await chrome.tabs.sendMessage(tabId, { type: 'GET_FRAME' }, { frameId })
      : await chrome.tabs.sendMessage(tabId, { type: 'GET_FRAME' });
  } catch (_) {
    // Tab closed, content script not injected, or extension reload — silently skip.
    return;
  }
  if (!resp) return;
  if (resp.blocked) {
    frameBlocked = true;
    if (mcStageNote) {
      mcStageNote.textContent = 'preview unavailable';
      mcStageNote.hidden = false;
    }
    return;
  }
  if (resp.idle) return;  // no playing video; keep last good frame
  if (resp.frame) {
    lastFrameDataUrl = resp.frame;
    if (mcStage) {
      // Layer the frame UNDER the existing gradients so the "On Air" / res
      // badges and overlays stay readable.
      mcStage.style.backgroundImage =
        `linear-gradient(180deg, rgba(0,0,0,0) 50%, rgba(0,0,0,0.45) 100%), ` +
        `url("${resp.frame}")`;
      mcStage.style.backgroundSize = 'cover, cover';
      mcStage.style.backgroundPosition = 'center, center';
      mcStage.dataset.hasFrame = 'true';
    }
    if (mcStageNote) mcStageNote.hidden = true;
  }
}

// Grace window after cast start — during the first few seconds the Chromecast
// reports IDLE+reason=None before transitioning to BUFFERING/PLAYING. Clearing
// the session then would bounce the user back to the picker right after they
// successfully cast.
const IDLE_GRACE_MS = 20 * 1000;

function isIdleEnded(status) {
  if (!status) return true;
  if (status.state !== 'IDLE') return false;
  const reason = (status.idleReason || '').toUpperCase();
  // Definitive end-of-session signals from pychromecast. Anything else
  // (including reason=None / "" during startup) is not "ended".
  if (reason === 'CANCELLED' || reason === 'FINISHED') return true;
  // Fallback for older app responses that didn't include idleReason: only
  // trust "IDLE means ended" after the startup grace window has passed.
  if (!reason && activeCast && activeCast.startedAt) {
    return Date.now() - activeCast.startedAt > IDLE_GRACE_MS;
  }
  return false;
}

// Session is dead but not cleanly ended — playback errored (CDN 403, decode
// failure, network). We keep the player UI visible so the user sees what
// happened, but disable transport (those calls would raise in pychromecast)
// and turn Stop into a local-only Dismiss.
function isDeadSession(status) {
  if (!status || status.state !== 'IDLE') return false;
  return (status.idleReason || '').toUpperCase() === 'ERROR';
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

async function pollOnce() {
  if (!activeCast) return;
  let status;
  try {
    status = await sendNM({ type: 'media_status', castUuid: activeCast.castUuid });
  } catch (e) {
    // App connection dropped — don't thrash. Retry on next tick.
    pollTimer = setTimeout(pollOnce, 2000);
    return;
  }
  if (isIdleEnded(status)) {
    await clearActiveCast();
    // Re-render the picker from scratch.
    showPickerMode();
    await initPicker();
    return;
  }
  updatePlayerUi(status);
  // Piggyback a frame request on the same tick — keeps the stage in sync
  // with whatever's actually playing on the source tab.
  paintStageFromSourceTab();
  // Dead session (IDLE+ERROR): stop polling — nothing will change until the
  // user dismisses. Controls are already disabled in updatePlayerUi.
  if (isDeadSession(status)) return;
  pollTimer = setTimeout(pollOnce, 1000);
}

async function clearActiveCast() {
  activeCast = null;
  lastStatus = null;
  try { await chrome.storage.local.remove('activeCast'); } catch (_) { /* ignore */ }
}

async function sendCmd(action, extra = {}) {
  if (!activeCast) return;
  try {
    const r = await sendNM({
      type: 'media_cmd',
      castUuid: activeCast.castUuid,
      action,
      ...extra,
    });
    if (r && r.status === 'error') {
      console.warn('media_cmd error:', r.detail);
    }
  } catch (e) {
    console.warn('media_cmd failed:', e);
  }
}

mcPlayPause.addEventListener('click', () => {
  const now = Date.now();
  if (now - lastPlayPauseAt < 300) return;  // debounce spam clicks
  lastPlayPauseAt = now;
  const state = lastStatus && lastStatus.state;
  sendCmd(state === 'PLAYING' ? 'pause' : 'play');
});

// Optimistic seek: jump the scrubber synchronously and reschedule the next
// poll for ~220ms instead of waiting for the regular 1s tick. This makes the
// skip buttons feel instant; the pychromecast round-trip catches up shortly.
function optimisticSeek(deltaSeconds) {
  if (!lastStatus) return;
  const dur = Number(lastStatus.duration) || 0;
  const cur = Number(mcScrubber.value) || Number(lastStatus.currentTime) || 0;
  let next = cur + deltaSeconds;
  if (dur > 0) next = Math.min(dur, next);
  next = Math.max(0, next);
  mcScrubber.value = String(next);
  mcCurrent.textContent = formatTime(next);
  if (dur > 0) {
    const pct = Math.min(100, Math.max(0, (next / dur) * 100));
    mcScrubber.style.setProperty('--cb-fill', pct.toFixed(2) + '%');
  }
  lastOptimisticSeekAt = Date.now();
  stopPolling();
  pollTimer = setTimeout(pollOnce, 220);
}
mcBack10.addEventListener('click', () => { optimisticSeek(-10); sendCmd('skip_back', { delta: 10 }); });
mcFwd10.addEventListener('click', () => { optimisticSeek(10); sendCmd('skip_forward', { delta: 10 }); });

// Volume up/down — 5% step. We optimistically update currentVolume and the
// readout, then send the absolute volume to the app.
const VOLUME_STEP = 0.05;
function nudgeVolume(delta) {
  const next = Math.max(0, Math.min(1, +(currentVolume + delta).toFixed(2)));
  if (next === currentVolume) {
    // At a limit — still flash so the user knows the click registered.
    flashVolumeReadout(next);
    return;
  }
  currentVolume = next;
  flashVolumeReadout(next);
  sendCmd('set_volume', { volume: next });
}
function flashVolumeReadout(vol) {
  if (!mcVolReadout) return;
  mcVolReadout.textContent = Math.round(vol * 100) + '%';
  mcVolReadout.classList.add('is-on');
  if (volReadoutTimer) clearTimeout(volReadoutTimer);
  volReadoutTimer = setTimeout(() => {
    mcVolReadout.classList.remove('is-on');
  }, 1200);
}
mcVolDown.addEventListener('click', () => nudgeVolume(-VOLUME_STEP));
mcVolUp.addEventListener('click', () => nudgeVolume(VOLUME_STEP));

async function endCastAndReturnToPicker() {
  // If the session is already dead on the TV side, sending `stop` would just
  // hit pychromecast's "Failed to execute stop" — skip it and dismiss locally.
  if (!isDeadSession(lastStatus)) {
    await sendCmd('stop');
  }
  await clearActiveCast();
  showPickerMode();
  await initPicker();
}
mcStop.addEventListener('click', endCastAndReturnToPicker);

// Scrubber: use input to show the dragged position, change to commit the seek.
mcScrubber.addEventListener('input', () => {
  isDragging = true;
  mcCurrent.textContent = formatTime(Number(mcScrubber.value) || 0);
});
mcScrubber.addEventListener('change', async () => {
  const seconds = Number(mcScrubber.value) || 0;
  isDragging = false;
  await sendCmd('seek', { seconds });
});

// ---------------------------------------------------------------------------
// Picker mode (original flow, now factored into initPicker)
// ---------------------------------------------------------------------------

async function initPicker() {
  // Reset picker-side UI in case we're returning from player mode.
  if (heroArea) heroArea.hidden = false;
  deviceArea.hidden = true;
  actionRow.hidden = true;
  devicesMenu.innerHTML = '';
  closeDevicesMenu();
  selectedStreamUrl = '';
  selectedDeviceUuid = '';
  haveStream = false;
  availableDevices = [];
  setDevicesLabel('Select a Chromecast…', true);

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || tab.id == null) {
    setHint('Open a video page and reopen this popup.');
    return;
  }
  currentTabId = tab.id;
  currentTabUrl = tab.url || '';
  if (/^(chrome|edge|about|chrome-extension):/i.test(currentTabUrl)) {
    setHint('Not a regular web page.');
    return;
  }

  const [stateRes, castsRes] = await Promise.allSettled([
    chrome.runtime.sendMessage({ type: 'GET_STATE', tabId: currentTabId }),
    sendNM({ type: 'list_casts' }),
  ]);

  if (stateRes.status === 'fulfilled' && stateRes.value) {
    allCandidates = buildCandidates(stateRes.value);
    if (allCandidates.length === 0) {
      setHint('No video detected yet. Start playback and reopen this popup.');
    } else {
      renderCandidates(allCandidates);
    }
  } else {
    setHint('Background not responding.');
  }

  if (castsRes.status === 'fulfilled' && castsRes.value && castsRes.value.type === 'casts') {
    const casts = castsRes.value.casts || [];
    renderDevices(casts);
    if (casts.length === 0) {
      const prev = hintEl.innerHTML;
      setHint(
        (prev ? prev + '<br>' : '') +
        'No Chromecasts detected. Make sure yours is powered on and on the same Wi-Fi as this laptop.'
      );
    }
  } else {
    setHint('Could not list Chromecasts: ' + JSON.stringify(castsRes));
  }

  refreshActionRow();
}

async function init() {
  // Ping the desktop app first — nothing below works without it.
  let ping;
  try {
    ping = await sendNM({ type: 'ping' });
    if (!ping || ping.type !== 'pong') {
      setStatus('err', 'Unexpected pong: ' + JSON.stringify(ping));
      return;
    }
    setStatus('ok', 'Connected');
  } catch (e) {
    const msg = String((e && e.message) || e);
    setStatus('err', 'Desktop app not running');
    if (/Specified native messaging host not found/i.test(msg)) {
      setHint('Run <code>register_nm_host.ps1</code> with your extension ID, then restart Chrome.');
    } else if (/Native host has exited/i.test(msg)) {
      setHint('Check <code>%LOCALAPPDATA%\\CastBooster\\nm_host.log</code>.');
    } else {
      setHint('Details: ' + msg);
    }
    return;
  }

  // Check for an active cast. If one exists AND the app still has a live
  // session for it, render player controls instead of the picker.
  let stored = {};
  try { stored = await chrome.storage.local.get('activeCast'); } catch (_) { /* ignore */ }
  if (stored && stored.activeCast && stored.activeCast.castUuid) {
    activeCast = stored.activeCast;
    let status = null;
    try {
      status = await sendNM({ type: 'media_status', castUuid: activeCast.castUuid });
    } catch (_) { /* fall through to picker */ }
    if (status && !isIdleEnded(status)) {
      showPlayerMode();
      updatePlayerUi(status);
      pollTimer = setTimeout(pollOnce, 1000);
      return;
    }
    // Stale stash — clear it and drop into picker.
    await clearActiveCast();
  }

  await initPicker();
}

window.addEventListener('unload', stopPolling);

init();
