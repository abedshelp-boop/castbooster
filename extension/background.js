// background.js — MV3 service worker.
//
// Three jobs:
//   1. Sniff network traffic per-tab for video URLs (HLS/DASH/MP4/WebM) and
//      rank them. Capture the Referer + User-Agent the browser used for each
//      request, because the desktop app needs these to replay upstream fetches
//      with the same session identity.
//   2. Accept messages from content scripts (VIDEO_DETECTED, PLAYER_IFRAME)
//      and the popup (GET_STATE, REGISTER_AND_SHIP, NM_SEND), dispatch.
//   3. On REGISTER_AND_SHIP: gather cookies for both the URL being cast and
//      the top-level page, bundle with captured UA + Referer, forward to the
//      desktop app via native messaging, and return the playback URL.

import { captureCookiesFor } from './lib/cookie_capture.js';

const HOST_NAME = 'com.castbooster.host';

// ---------------------------------------------------------------------------
// URL classification (ported from salvaged/NOTES.md §1-3)
// ---------------------------------------------------------------------------
const EXT_REGEX = /\.(m3u8|mpd|mp4|webm|m4s|ts)(\?|$)/i;
const URLSET_REGEX = /\.urlset\/(master|index)[\w\-.]*\.(txt|m3u8)/i;

function classifyUrl(url) {
  const m = EXT_REGEX.exec(url);
  if (m) return m[1].toLowerCase();
  if (URLSET_REGEX.test(url)) return 'm3u8';
  return 'unknown';
}

function m3u8Role(url) {
  if (/\.urlset\/master/i.test(url)) return 'master';
  if (/\.urlset\/index-/i.test(url)) return 'variant';
  if (/\/seg\d*\/|\/chunk|\/frag|_\d+\.m3u8(?:\?|$)|-\d+\.m3u8(?:\?|$)/i.test(url)) return 'fragment';
  return 'master';
}

// Hostnames that are almost always ads / trackers / analytics, never real
// video content. Any URL whose host matches one of these gets a big score
// penalty so it sinks to the bottom of the candidate list.
const AD_CDN_PATTERNS = [
  /(^|\.)tiktokcdn\.com$/i,
  /(^|\.)doubleclick\.net$/i,
  /(^|\.)googlesyndication\.com$/i,
  /(^|\.)adsafeprotected\.com$/i,
  /(^|\.)amazon-adsystem\.com$/i,
  /(^|\.)2mdn\.net$/i,
  /(^|\.)adservice\.google\./i,
  /(^|\.)googleadservices\.com$/i,
  /(^|\.)moatads\.com$/i,
  /(^|\.)scorecardresearch\.com$/i,
  /(^|\.)adsrvr\.org$/i,
];

export function isAdHost(url) {
  try {
    const h = new URL(url).hostname;
    return AD_CDN_PATTERNS.some((re) => re.test(h));
  } catch (_) {
    return false;
  }
}

function baseScore(url, type) {
  let base;
  switch (type) {
    case 'm3u8': {
      const role = m3u8Role(url);
      if (role === 'master') base = 110;
      else if (role === 'variant') base = 75;
      else base = 25;
      break;
    }
    case 'mpd': base = 95; break;
    case 'mp4': {
      const looksLikeFragment = /(init|chunk|frag|seg|bumper)[-_\d]*\.mp4|\/\d+\.mp4/i.test(url);
      base = looksLikeFragment ? 20 : 70;
      break;
    }
    case 'webm': base = 60; break;
    case 'm4s':
    case 'ts': base = 10; break;
    default: base = 0;
  }
  if (isAdHost(url)) base = Math.max(0, base - 100);
  return base;
}

function typeFromContentType(ct) {
  if (!ct) return null;
  const low = ct.toLowerCase();
  if (low.includes('mpegurl')) return 'm3u8';
  if (low.includes('dash+xml')) return 'mpd';
  if (low.startsWith('video/mp4') || low.startsWith('application/mp4')) return 'mp4';
  if (low.startsWith('video/webm')) return 'webm';
  if (low.startsWith('video/x-matroska')) return 'mkv';
  if (low.startsWith('video/mp2t')) return 'ts';
  if (low.startsWith('video/')) return 'video';
  return null;
}

// ---------------------------------------------------------------------------
// Per-tab state
// ---------------------------------------------------------------------------
/** @type {Map<number, {captures: any[], dom: any|null, iframes: any[]}>} */
const tabState = new Map();

function getTab(tabId) {
  let s = tabState.get(tabId);
  if (!s) { s = { captures: [], dom: null, iframes: [] }; tabState.set(tabId, s); }
  return s;
}
function clearTab(tabId) { tabState.delete(tabId); }

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === 'loading' && changeInfo.url) clearTab(tabId);
});
chrome.tabs.onRemoved.addListener((tabId) => clearTab(tabId));

// ---------------------------------------------------------------------------
// webRequest sniffing
// ---------------------------------------------------------------------------
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const type = classifyUrl(details.url);
    if (type === 'unknown') return;
    const s = getTab(details.tabId);
    const existing = s.captures.find((c) => c.url === details.url);
    const now = Date.now();
    if (existing) { existing.lastSeen = now; return; }
    s.captures.push({
      url: details.url,
      type,
      firstSeen: now,
      lastSeen: now,
      contentLength: 0,
      contentType: '',
      score: baseScore(details.url, type),
      referer: '',
      userAgent: '',
    });
    if (s.captures.length > 200) {
      s.captures.sort((a, b) => b.score - a.score);
      s.captures.length = 200;
    }
  },
  { urls: ['<all_urls>'] },
  []
);

// Capture UA and Referer. 'extraHeaders' is mandatory on MV3 — without it
// Chrome strips these headers from the callback.
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const s = getTab(details.tabId);
    const cap = s.captures.find((c) => c.url === details.url);
    if (!cap) return;
    for (const h of details.requestHeaders || []) {
      const n = h.name.toLowerCase();
      if (n === 'user-agent') cap.userAgent = h.value;
      else if (n === 'referer') cap.referer = h.value;
    }
  },
  {
    urls: ['<all_urls>'],
    types: ['media', 'xmlhttprequest', 'other', 'sub_frame', 'object'],
  },
  ['requestHeaders', 'extraHeaders']
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return;
    let contentLength = 0;
    let contentType = '';
    for (const h of details.responseHeaders || []) {
      const name = h.name.toLowerCase();
      if (name === 'content-length') contentLength = parseInt(h.value || '0', 10) || 0;
      else if (name === 'content-type') contentType = (h.value || '').toLowerCase();
    }
    const s = getTab(details.tabId);
    let cap = s.captures.find((c) => c.url === details.url);
    if (!cap) {
      const ctType = typeFromContentType(contentType);
      if (!ctType) return;
      const isManifest = ctType === 'm3u8' || ctType === 'mpd';
      if (!isManifest && contentLength > 0 && contentLength < 200 * 1024) return;
      cap = {
        url: details.url,
        type: ctType,
        firstSeen: Date.now(),
        lastSeen: Date.now(),
        contentLength,
        contentType,
        score: baseScore(details.url, ctType),
        referer: '',
        userAgent: '',
      };
      s.captures.push(cap);
      if (s.captures.length > 200) {
        s.captures.sort((a, b) => b.score - a.score);
        s.captures.length = 200;
      }
    } else {
      cap.contentLength = contentLength || cap.contentLength;
      cap.contentType = contentType || cap.contentType;
    }
    if (cap.type === 'mp4') {
      if (cap.contentLength > 5 * 1024 * 1024) cap.score = Math.max(cap.score, 85);
      else if (cap.contentLength > 0 && cap.contentLength < 200 * 1024) cap.score = Math.min(cap.score, 15);
    }
    if (cap.contentType.includes('mpegurl')) cap.score = Math.max(cap.score, 110);
    if (cap.contentType.includes('dash+xml')) cap.score = Math.max(cap.score, 95);
    if (cap.contentType.startsWith('video/') && cap.score < 50) cap.score = 50;
  },
  {
    urls: ['<all_urls>'],
    types: ['media', 'xmlhttprequest', 'other', 'sub_frame', 'object'],
  },
  ['responseHeaders']
);

// ---------------------------------------------------------------------------
// Native messaging — one-shot send wrapped in a promise
// ---------------------------------------------------------------------------
function sendNative(payload) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendNativeMessage(HOST_NAME, payload, (resp) => {
        const err = chrome.runtime.lastError;
        if (err) reject(new Error(err.message || 'native messaging failed'));
        else resolve(resp);
      });
    } catch (e) {
      reject(e);
    }
  });
}

// ---------------------------------------------------------------------------
// Messages from popup + content scripts
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return;

  switch (msg.type) {
    case 'VIDEO_DETECTED': {
      const tabId = sender.tab && sender.tab.id;
      if (tabId == null) return;
      const s = getTab(tabId);
      const incoming = msg.payload;
      const cur = s.dom;
      if (
        !cur ||
        (incoming.paused === false && cur.paused === true) ||
        (incoming.width * incoming.height > (cur.width || 0) * (cur.height || 0))
      ) {
        s.dom = { ...incoming, reportedAt: Date.now() };
      }
      return;
    }

    case 'PLAYER_IFRAME': {
      const tabId = sender.tab && sender.tab.id;
      if (tabId == null) return;
      const s = getTab(tabId);
      const incoming = msg.payload;
      if (!incoming || !incoming.src) return;
      const existing = s.iframes.find((f) => f.src === incoming.src);
      if (existing) {
        existing.reportedAt = Date.now();
        existing.width = incoming.width;
        existing.height = incoming.height;
      } else {
        s.iframes.push({ ...incoming, reportedAt: Date.now() });
      }
      if (s.iframes.length > 20) {
        s.iframes.sort((a, b) => b.reportedAt - a.reportedAt);
        s.iframes.length = 20;
      }
      return;
    }

    case 'GET_STATE': {
      const s = tabState.get(msg.tabId) || { captures: [], dom: null, iframes: [] };
      const ranked = [...s.captures].sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        return b.lastSeen - a.lastSeen;
      });
      sendResponse({ dom: s.dom, captures: ranked, iframes: s.iframes || [] });
      return true;
    }

    case 'CLEAR_TAB': {
      clearTab(msg.tabId);
      sendResponse({ ok: true });
      return true;
    }

    case 'NM_SEND': {
      sendNative(msg.payload).then(
        (resp) => sendResponse({ ok: true, resp }),
        (err) => sendResponse({ ok: false, error: String((err && err.message) || err) })
      );
      return true;
    }

    case 'REGISTER_AND_SHIP': {
      // Deprecated path kept for diagnostics — register without casting.
      handleRegister(msg).then(
        (out) => sendResponse(out),
        (err) => sendResponse({ ok: false, error: String((err && err.message) || err) })
      );
      return true;
    }

    case 'CAST_NOW': {
      // Full flow: cookie capture → register_stream → cast. Runs entirely in
      // the service worker so the popup can close without aborting mid-flow.
      handleCastNow(msg).then(
        (out) => sendResponse(out),
        (err) => sendResponse({ ok: false, error: String((err && err.message) || err) })
      );
      return true;
    }
  }
});

async function buildRegisterPayload(msg) {
  const tabId = msg.tabId;
  const url = msg.url;
  const tabUrl = msg.tabUrl || '';
  if (!url) throw new Error('no url');
  const s = tabState.get(tabId) || { captures: [] };
  const cap = s.captures.find((c) => c.url === url);
  const ua = (cap && cap.userAgent) || (msg.fallbackUserAgent || '');
  const referer = (cap && cap.referer) || tabUrl;
  const headers = {};
  if (referer) headers.Referer = referer;
  const cookies = await captureCookiesFor([url, tabUrl].filter(Boolean));
  return { type: 'register_stream', url, cookies, headers, userAgent: ua };
}

async function handleRegister(msg) {
  try {
    const payload = await buildRegisterPayload(msg);
    const resp = await sendNative(payload);
    return { ok: true, request: payload, resp };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

async function handleCastNow(msg) {
  if (!msg.castUuid) return { ok: false, error: 'missing castUuid' };
  let payload;
  try {
    payload = await buildRegisterPayload(msg);
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
  const reg = await sendNative(payload);
  if (!reg || reg.type !== 'stream_registered') {
    return {
      ok: false,
      error: (reg && reg.detail) ? ('register failed: ' + reg.detail) : 'register failed',
      register: reg,
    };
  }
  // P3.4: forward the popup's "Smooth motion" preference to the proxy so it
  // can pick NoopFilter vs RIFEFilter via spec §4.2. Defaults to false when
  // omitted, matching the proxy-side default.
  const cast = await sendNative({
    type: 'cast',
    token: reg.token,
    castUuid: msg.castUuid,
    enable_smooth: !!msg.enable_smooth,
  });
  if (!cast || cast.type !== 'casting' || cast.status !== 'ok') {
    return {
      ok: false,
      error: (cast && cast.detail) ? ('cast failed: ' + cast.detail) : 'cast failed',
      register: reg,
      cast,
    };
  }

  // Stash the active cast so the popup can render player controls instead of
  // the picker on next open. Device name comes from list_casts — best effort,
  // don't fail the cast if that lookup trips.
  try {
    let deviceName = '';
    try {
      const list = await sendNative({ type: 'list_casts' });
      const found = (list && list.casts || []).find((c) => c.uuid === msg.castUuid);
      if (found) deviceName = found.name || '';
    } catch (_) { /* non-fatal */ }
    await chrome.storage.local.set({
      activeCast: {
        castUuid: msg.castUuid,
        deviceName,
        token: reg.token,
        upstreamUrl: msg.url,
        startedAt: Date.now(),
      },
    });
  } catch (e) {
    console.warn('failed to stash activeCast', e);
  }

  return { ok: true, register: reg, cast };
}
