# Salvaged logic from the prototype

Extracted from `background.js`, `content.js`, `popup.js` before the clean-slate deletion. Port these into the new `extension/` during Milestone C and F. Delete this file once done.

---

## 1. URL classification — file-extension regex

**Source**: `background.js` L101-113, `popup.js` L107-112.

```js
// Matches the six video-related extensions we care about, with or without query string.
const EXT_REGEX = /\.(m3u8|mpd|mp4|webm|m4s|ts)(\?|$)/i;

// Smashystream / urlset-family mirrors serve HLS playlists from:
//   .../hls3/.../xxx.urlset/master.txt
//   .../xxx.urlset/index-f2-v1-a1.txt
// The .txt is a cache-buster; content is still #EXTM3U. Must catch this.
const URLSET_REGEX = /\.urlset\/(master|index)[\w\-.]*\.(txt|m3u8)/i;

function classifyUrl(url) {
  const m = EXT_REGEX.exec(url);
  if (m) return m[1].toLowerCase();
  if (URLSET_REGEX.test(url)) return 'm3u8';
  return 'unknown';
}
```

## 2. HLS role detection (master / variant / fragment)

**Source**: `background.js` L119-124.

```js
function m3u8Role(url) {
  if (/\.urlset\/master/i.test(url)) return 'master';
  if (/\.urlset\/index-/i.test(url)) return 'variant';
  if (/\/seg\d*\/|\/chunk|\/frag|_\d+\.m3u8(?:\?|$)|-\d+\.m3u8(?:\?|$)/i.test(url)) return 'fragment';
  return 'master';
}
```

## 3. Candidate ranking scores

**Source**: `background.js` L128-151, `popup.js` L42-105.

| Type | Base score | Notes |
|---|---|---|
| HLS master (from URL) | 110 | Highest — castable directly |
| DASH `.mpd` | 95 | Castable, but rare on target sites |
| MP4 (looks like full file) | 70 (or 85 if >5 MB per Content-Length) | Castable |
| HLS variant | 75 | Castable |
| WebM | 60 | Castable |
| DOM `<video>.currentSrc` (non-blob) | 120 | Most trustworthy — from actual playback |
| Player iframe (top frame, ≥400×200) | 40 | Fallback only; ranked BELOW real URLs |
| MP4 fragment (init/chunk/frag/seg pattern, or <200 KB) | 15-20 | Discard |
| `.ts` / `.m4s` chunks | 10 | Always discard from UI |
| Unknown (rescued via Content-Type starting with `video/`) | floor 50 | Content-Type-only catch |

Content-Type bumps (applied during `onHeadersReceived`):
- `application/vnd.apple.mpegurl` / `application/x-mpegurl` → floor 110
- `application/dash+xml` → floor 95
- `video/mp4` + Content-Length >5 MB → floor 85
- `video/mp4` + Content-Length <200 KB → cap 15
- Any `video/*` → floor 50

## 4. URL sniffing — when to capture from headers

**Source**: `background.js` L226-295.

Both triggers catch new URLs:
1. `chrome.webRequest.onBeforeRequest` — URL matches `EXT_REGEX` or `URLSET_REGEX`.
2. `chrome.webRequest.onHeadersReceived` — even if URL doesn't match, capture if Content-Type is a known video MIME. Skip if Content-Length < 200 KB AND not a manifest (playlists are small; video fragments <200 KB are probably noise).

```js
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
```

webRequest listener filter (don't sniff every CSS/font):
```js
{ urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'sub_frame', 'object'] }
```

Soft cap: 200 captures per tab. Sort by score desc and truncate when exceeded.

## 5. Fragment filter for UI

**Source**: `popup.js` L88-92.

Always skip `.ts` and `.m4s` when rendering the candidate list to the user. They're individual ~10s chunks, never castable standalone.

## 6. DOM `<video>` scanner

**Source**: `content.js` entirety.

Runs on every frame (`all_frames: true`, `match_origin_as_fallback: true`). Single-install guard: `window.__castBoosterInstalled`.

Scoring per `<video>`:
```js
area + (playing ? 1_000_000 : 0) + (hasDuration ? 500_000 : 0)
```
where `playing = !v.paused && v.readyState >= 2` and `hasDuration = isFinite(v.duration) && v.duration > 0`.

Describe the winner:
```js
{
  src: video.src || '',
  currentSrc: video.currentSrc || '',    // this is what actually plays
  duration: Number.isFinite(video.duration) ? video.duration : 0,
  width: video.videoWidth || video.clientWidth || 0,
  height: video.videoHeight || video.clientHeight || 0,
  paused: !!video.paused,
  frameUrl: location.href,
}
```

Reporting triggers (debounced 400 ms):
- `MutationObserver` on `document.documentElement` with `{ childList: true, subtree: true }` — players often create `<video>` lazily.
- Document events (capture phase): `play`, `pause`, `loadedmetadata`, `durationchange`, `emptied`.
- One-shot at injection time.

## 7. Iframe detector (top frame only)

**Source**: `content.js` L63-81.

```js
function scanIframes() {
  if (window.top !== window.self) return []; // top frame only
  const out = [];
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.src || '';
    if (!/^https?:\/\//i.test(src)) continue;       // skip about:blank, data:, srcdoc
    const r = f.getBoundingClientRect();
    if (r.width < 400 || r.height < 200) continue;  // 400x200 floor kills tracking pixels + share widgets
    out.push({
      src,
      width: Math.round(r.width),
      height: Math.round(r.height),
      host: new URL(src).hostname,
    });
  }
  return out;
}
```

Useful for fallback scenarios — if sniffing captures nothing, the player iframe URL is at least somewhere we know the video lives.

## 8. Per-tab state management

**Source**: `background.js` L64-166.

```js
const tabState = new Map();  // tabId -> { captures, dom, iframes }

function getTab(tabId) {
  let s = tabState.get(tabId);
  if (!s) { s = { captures: [], dom: null, iframes: [] }; tabState.set(tabId, s); }
  return s;
}
```

Wipe on `chrome.tabs.onUpdated` with `{ status: 'loading', url: <set> }` — new URL = new page = old captures are stale.
Wipe on `chrome.tabs.onRemoved`.

## 9. Candidate merge + dedup (for popup)

**Source**: `popup.js` L42-105.

Order of merge (highest trust first):
1. DOM `<video>.currentSrc` (score 120) — if not `blob:` or `data:`.
2. Iframes (score 40) — fallback, open in new tab.
3. webRequest captures (already-scored list from background) — skip fragments (`.ts`, `.m4s`).

Dedup by exact URL string (use a `Set<string>` of seen URLs). Sort by score descending.

## 10. Human-readable type labels

**Source**: `popup.js` L116-133.

For display in the new popup:
- `m3u8` + `.urlset/master` → `HLS master`
- `m3u8` + `.urlset/index-` → `HLS variant`
- `m3u8` + chunk pattern → `HLS fragment`
- `m3u8` plain → `HLS master`
- `mpd` → `DASH manifest`
- `mp4` → `MP4`
- `webm` → `WebM`
- `ts` → `HLS chunk`
- `m4s` → `DASH chunk`
- `iframe` → `Player page`

## 11. Manifest starting point

**Source**: prototype `manifest.json`.

Permissions to KEEP in the new manifest:
- `activeTab`, `scripting`, `tabs`, `storage`, `webRequest`
- `host_permissions: ["<all_urls>"]`
- `content_scripts` with `all_frames: true`, `match_origin_as_fallback: true`

Permissions to ADD:
- `nativeMessaging`
- `cookies`

Things to REMOVE:
- `declarativeNetRequestWithHostAccess` (CORS bypass rule is obsolete — the desktop app owns fetching, not the extension)
- `web_accessible_resources` entries for `player.html`, `player.js`, `vendor/hls.min.js`, `sandbox/*`
- `sandbox` block
- `content_security_policy.sandbox`

Things to ADD:
- A top-level `"key": "<base64>"` field so the extension ID is stable across unpacked reloads in dev. Generate with `openssl genrsa 2048 | openssl rsa -pubout -outform DER | base64 -w 0`. Commit the key.

## 12. Use `onBeforeSendHeaders` for UA + Referer capture (NEW — not in prototype)

The prototype never needed this. The new extension must capture the exact `User-Agent` and `Referer` the browser sent on each sniffed request so the desktop app can replay with identical headers.

```js
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const s = getTab(details.tabId);
    const cap = s.captures.find((c) => c.url === details.url);
    if (!cap) return;
    for (const h of details.requestHeaders || []) {
      const n = h.name.toLowerCase();
      if (n === 'user-agent') cap.userAgent = h.value;
      if (n === 'referer') cap.referer = h.value;
    }
  },
  { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'sub_frame', 'object'] },
  ['requestHeaders', 'extraHeaders']   // 'extraHeaders' is REQUIRED to see cookies, UA, referer — MV3 default strips them
);
```

---

**Everything above is dead code in the old files — the new extension ports the concepts only. Delete this file after Milestone F.**
