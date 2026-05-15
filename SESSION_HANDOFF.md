# Cast Booster — Session Handoff Document

**Read this file FIRST before doing anything else in a new session.**

This is the complete memory transfer from the design session that scoped Cast Booster. It captures every agreement, technical decision, user preference, and next step so a fresh Claude instance can pick up the work without asking Abed to re-explain anything.

- **Last session ended**: 2026-04-22
- **Authors**: Claude (Sonnet) — design session (2026-04-14), then vertical-slice build (2026-04-22)
- **User**: Abed
- **Working directory**: `C:\Users\Abeds\Cursor projects\Chrome-cast-extension\`
- **Current state of product**: **Phase 1 vertical slice SHIPS.** Windows desktop app + Chrome extension + Chrome Native Messaging bridge + session-aware HLS proxy + pychromecast control all working end-to-end. Verified by casting a real HLS stream from `masukestin.com` to a real Chromecast ("Dining room TV"), full HD optimized playback. Session-locked URLs solved. Next work is controls (play/pause/seek/skip in the popup) and then Phase 2 polish (installer, code signing, landing page, payments).
- **Pillar 2 in progress**: P2.1 (ffmpeg foundation) shipped on branch `pillar-2.1/ffmpeg-foundation`. Spec at `docs/superpowers/specs/2026-05-15-pillar-2.1-ffmpeg-foundation-design.md`; plan at `docs/superpowers/plans/2026-05-15-pillar-2.1-ffmpeg-foundation.md`. Next sub-session: P2.2 (transcoder module).
- **Pillar 2 in progress**: P2.2 (transcoder module + state machine) shipped. Spec at `docs/superpowers/specs/2026-05-15-pillar-2.2-transcoder-design.md`; plan at `docs/superpowers/plans/2026-05-15-pillar-2.2-transcoder.md`. Module is a leaf (no proxy wiring yet); next sub-session P2.3 builds the filter chain.
- **Pillar 2 in progress**: P2.3 (filter chain abstraction + hot-reload) shipped on branch `pillar-2.3/filter-chain`. Spec at `docs/superpowers/specs/2026-05-15-pillar-2.3-filter-chain-design.md`; plan at `docs/superpowers/plans/2026-05-15-pillar-2.3-filter-chain.md`. Next sub-session: P2.4 proxy integration — wire `/upstream/*` and `/output/*` routes, gate `_handle_cast` on transcoder READY.

---

## 2026-05-15 — P2.3 complete

Pillar 2 sub-task 3 (filter chain abstraction + NoopFilter + SubtitleBurnIn + hot-reload) shipped on `pillar-2.3/filter-chain`.

- `app/castbooster/filter_chain.py` — FilterStage Protocol, NoopFilter, SubtitleBurnIn, FilterChain
- `app/castbooster/transcoder.py` — extracted `_ProcessSlot`, added `RELOADING` state, added `set_filter_chain(chain) -> bool` with full reload semantics (promote / demote with `last_reload_error`)
- 13 filter unit tests + 14 reload unit tests + 2 gated e2e tests; all green
- P2.2's 22 lifecycle tests preserved through the refactor

Spec: `docs/superpowers/specs/2026-05-15-pillar-2.3-filter-chain-design.md`
Plan: `docs/superpowers/plans/2026-05-15-pillar-2.3-filter-chain.md`

Next: P2.4 proxy integration — wire `/upstream/*` and `/output/*` routes, escape-hatch env flag, gate `_handle_cast` on transcoder READY.

---

## Section 0 — TL;DR (read this even if you read nothing else)

Cast Booster is a **Windows desktop app + Chrome extension + castbooster.com landing page + SaaS backend** that lets users cast video from sites Chrome does NOT optimize for (EgyDead, vibuxer, hanerix, Smashystream mirrors, and similar custom-player streaming sites) to a Chromecast in genuine high-quality "optimized for video" mode rather than the washed-out "tab mirror" fallback.

The extension-only prototype in this folder proved detection and sniffing work, but hit a fundamental physics ceiling: session-locked CDN URLs cannot be replayed from any client other than the browser session that originally requested them. The only solution is a desktop helper app that receives the stream URL + cookies + headers from the extension via **Chrome Native Messaging**, replays the CDN request with the user's session identity, and serves the video to the Chromecast from a tiny local HTTP proxy running on 127.0.0.1 + LAN IP.

**Target business**: $500K–$3M ARR lifestyle SaaS, one-person operation, primarily inheriting the 500K–700K stranded users of the abandoned Videostream extension. User confirmed "could grow to 1M ARR" so tech stack choices must prioritize longevity and scale, not just speed-to-first-demo.

**Next session first task**: Implement the playback controls feature (play / pause / skip ±10s / draggable seek bar / stop) per `docs/playback-controls-plan.md`. After that: ICP research (Section 12 — originally blocking, deferred while core tech got built and validated).

**Also pending for Phase 2**: first-run diagnostic bot, Inno Setup installer, Windows Authenticode code signing, auto-update, castbooster.com landing page, Stripe/Polar.sh payments, account system. See Section 13 for full roadmap.

---

## Section 0.5 — Phase 1 Build Log (2026-04-22)

What was actually built, what broke, and the non-obvious fixes.

### Shipped in one session

All six milestones from the build plan (`.claude/plans/1-delete-our-current-sorted-cupcake.md`) landed:

- **A** — Windows tray app, aiohttp HTTP server bound to `0.0.0.0:38123`, rotating log at `%LOCALAPPDATA%\CastBooster\castbooster.log`, LAN IP auto-detection via UDP-connect trick (picks default-route interface).
- **B** — Chrome Native Messaging bridge. Thin `nm_host.py` stdio forwarder that POSTs JSON frames to `/nm` on the running app. Auto-launches the app detached (`CREATE_NO_WINDOW | DETACHED_PROCESS`) in ~2.6s when Chrome spawns a host connection and the app isn't up yet. HKCU registration script (`app/scripts/register_nm_host.ps1`) — no admin needed.
- **C** — Cookie capture via `chrome.cookies.getAll`, UA + Referer capture via `chrome.webRequest.onBeforeSendHeaders` with `extraHeaders` (mandatory or Chrome strips these), pulled for both the media URL's host AND the top page's host.
- **D** — Session-aware HLS proxy: hand-rolled line-by-line URL rewriter (`hls_rewriter.py`) preserving master→variant→segment chain, `#EXT-X-KEY`/`#EXT-X-MAP`/`#EXT-X-MEDIA` `URI="..."` attributes. Five passing unit tests against fixture playlists. Range-aware pass-through for segments.
- **E** — pychromecast 14.0 discovery (`CastBrowser` + `SimpleCastListener`) + `play_media` + `block_until_active`. Holds cast connections in `_connections[uuid]` so GC doesn't drop them mid-playback. Media status listener logs every state transition.
- **F** — Popup picks stream + device → background worker orchestrates `register_stream` → `cast` via Native Messaging. Full end-to-end flow.

### The three bugs that mattered

1. **Explicit HEAD route registration crashed aiohttp startup.** My CORS patch added `app.router.add_route("HEAD", path, handler)` alongside the `add_get`, but aiohttp's GET handler already auto-serves HEAD, so startup threw `RuntimeError: Added route will never be executed, method HEAD is already registered`. Symptom: `nm_host` launching app detached but app crashing silently before logging a single line, so Chrome's NM port just said "app not running." Fix: remove the redundant HEAD registrations; keep only the explicit OPTIONS ones for CORS preflight. (`app/castbooster/proxy.py`.)

2. **Missing CORS headers on proxy responses.** Chromecast's Default Media Receiver (app ID `CC1AD845`) loads from `gstatic.com` and fetches our rewritten playlist/segment URLs via MSE cross-origin. Without `Access-Control-Allow-Origin: *` on the playlist response, the receiver's Chromium sandbox silently rejected the playlist — Chromecast would re-fetch `master.m3u8` four times in two seconds and then go silent, never fetching a single segment. Symptom: "TV starts loading but never plays." Fix: add CORS headers + OPTIONS preflight handler to every `/s/{token}/*` route.

3. **Ad playlist getting ranked as the top stream candidate.** On `masukestin.com`, the pre-roll ads are served as an HLS playlist (50 KB!) pointing at TikTok CDN images, and the sniffer caught it first because it was the first `.m3u8` the page requested. Popup collapsed to top-4, real video URL was hidden beyond it, user picked the ad playlist → Chromecast played 5 ads successfully → segment 6 returned 403 → `state=IDLE reason=ERROR`. Symptom: "video plays for 10 seconds then cuts off." Fix: (a) downscore hosts matching known ad CDN patterns (`tiktokcdn.com`, `doubleclick.net`, `googlesyndication.com`, etc.) by 100 points in `background.js`, (b) show ALL sniffed candidates in the popup (no more collapse limit), (c) label ad-CDN hosts with `⚠ ad CDN` in the dropdown, (d) default-select the first non-ad candidate.

### Known site-level issues (not our bugs)

- **`uqload.is`** closes TCP connections after ~15s of streaming to non-browser clients. Anti-leech. Symptom: 15s of clean playback then `ConnectionResetError: WinError 64`. Mitigation would be UA rotation / paced reads / ffmpeg in the middle — Phase 2 territory. Workaround for users: pick a different backend on the same episode page.

### Environment & library versions locked

- Python 3.14.2 on Windows 11 x64
- aiohttp 3.13.5 (async HTTP server + client)
- pychromecast 14.0.10 (Chromecast control)
- pystray 0.19.5 + Pillow 11.3.0 (system tray icon)
- zeroconf 0.148.0 (via pychromecast, shared instance — do NOT create a second one)
- pytest 8.4.2 (unit tests)

### Dev workflow (for next session)

1. `cd app` then `.venv\Scripts\python -m castbooster` to run the app (tray icon shows blue/white CB circle).
2. Extension already loaded unpacked at `extension/` with ID `fkplchgkmmdjdlckdgilacamohhjpmdj`. NM host already registered via `register_nm_host.ps1`.
3. Logs: `%LOCALAPPDATA%\CastBooster\castbooster.log` (app) and `%LOCALAPPDATA%\CastBooster\nm_host.log` (stdio bridge).
4. To restart the app cleanly: right-click tray icon → Quit, or `taskkill //F //PID $(netstat -ano | grep :38123 | awk '{print $5}')` from git bash.
5. Tests: `.venv\Scripts\python -m pytest tests/ -v` — 5 HLS rewriter tests should pass.

---

## Section 1 — User Profile (Abed)

- **Technical level**: Layman. Direct quote: *"im just a layman. Teach me all those technical terms you've mentioned thus far."* Always explain technical terms in plain English the first time you use them. Do not assume knowledge of: CORS, MSE, HLS, DASH, service workers, CSP, native messaging, mDNS, proxies, tokens, manifests, etc.
- **Operating system**: Windows. Machine username `Abeds`. Primary target platform is Windows-first.
- **Working style**:
  - Wants feasibility confirmed BEFORE implementation. Quote: *"Tell me first if this is even something we can fix or not before doing anything."*
  - Wants honesty. Quote: *"Be honest and straightforward."*
  - Wants up-to-date tooling. Don't use outdated/deprecated libraries; always current stable versions.
  - Prefers a single, integrated product over a collection of tools the user has to assemble.
  - Will ask "is this doable?" and expects a real answer, not hedging.
- **Budget**: Not a constraint. Quote: *"The budget is not an issue"*. Can afford code signing certs, premium dev tools, paid APIs, etc.
- **Scale ambition**: Serious business. Quote: *"This could grow to a 1m ARR one day so we should start with the most suitable options."* Not a weekend hobby.
- **Communication style**: Direct, a bit informal, sometimes mixes English and transliterated Arabic ("lol", "tbh"). Doesn't need you to be formal.

---

## Section 2 — The Problem

### What the user experiences (before Cast Booster exists)

1. Abed opens a streaming site like EgyDead (Arabic anime/movies mirror) on his laptop.
2. He clicks play, video works fine on the laptop.
3. He opens Chrome's menu → Cast → picks his Chromecast.
4. Chrome sends a **tab mirror** — the whole browser tab is re-encoded in low quality and beamed to the Chromecast. It looks bad, lags, audio syncs poorly, and the Chromecast is working way harder than it needs to.
5. On YouTube or Netflix, Chrome instead detects the real `<video>` element and sends a direct stream URL to the Chromecast. The Chromecast fetches the video itself, plays it in full quality, and the laptop can even close the tab — that's "optimized" casting.

Chrome does this optimization automatically on whitelisted sites and on any page where it can see a plain `<video src="https://...">`. On custom-player sites the video source is a `blob:` URL produced by MediaSource Extensions (MSE), so Chrome's Cast system can't read the real URL and falls back to tab mirror.

### Why every off-the-shelf solution fails

- **Chrome's built-in Cast**: only optimizes whitelisted sites or plain `<video>` tags. Falls back to tab mirror for everything else.
- **Videostream**: the old go-to solution. 500K–700K install base. Abandoned by its developer since ~2022. No updates, doesn't handle modern HLS session locks, Chrome Web Store listing still exists but reviews are full of "doesn't work anymore".
- **Existing HLS proxies** (@warren-bank/hls-proxy etc.): CLI-only, developer tools. No regular user will install Node.js and run a proxy from a terminal. 78 weekly downloads on npm says everything.
- **VLC / MPV**: Desktop players with Chromecast output. Require the user to already have the stream URL, which they don't because the site hides it behind MSE.
- **DLNA servers / Plex**: Solve a different problem (serving your own media library). Useless for "I want to cast this website I'm watching right now."
- **Browser bookmarklets**: Can grab URLs but can't solve session locking.

### The two physics constraints that kill extension-only solutions

1. **Session lock**: Many CDN URLs are bound to the originating browser session — they include token params and expect specific cookies/Referer/User-Agent that the browser sent in the original fetch. Replaying the URL from a different origin (like `chrome-extension://...`) returns HTTP 404. Confirmed in the prototype: the DNR CORS bypass changed errors from "HTTP 0 (blocked)" to "HTTP 404 (server rejected)", but nothing in the extension can make the CDN accept the replay.
2. **Chromecast network identity**: When you tell a Chromecast to fetch a URL directly, the Chromecast device (on your LAN, different IP, no cookies, no referer) does the fetch itself. Session-locked CDNs reject this just as hard.

The only way to bypass both constraints is to have **something running on the user's machine that owns the cookies and makes the fetch look identical to the original browser request**, then re-serves the bytes to the Chromecast from a local HTTP server. That's the desktop app.

---

## Section 3 — The Solution Architecture

### High-level picture

```
┌──────────────────────────────────────────────────────────────────┐
│                      User's Windows laptop                      │
│                                                                  │
│  ┌───────────────────┐       Chrome Native Messaging             │
│  │  Chrome Extension │ ◄───── stdin/stdout JSON messages ─────►  │
│  │  (Cast Booster)   │                                           │
│  │                   │                                           │
│  │  - webRequest     │       ┌──────────────────────────┐        │
│  │    sniffer        │       │   Cast Booster Desktop   │        │
│  │  - content scripts│       │   (Windows tray app)     │        │
│  │  - popup UI       │       │                          │        │
│  │  - cookies perm   │       │  - local HTTP proxy      │        │
│  │  - native msg host│       │    127.0.0.1:38123 +     │        │
│  └──────────┬────────┘       │    LAN IP:38123          │        │
│             │                │  - session-aware fetcher │        │
│             │                │  - mDNS discovery        │        │
│             │                │  - pychromecast control  │        │
│             │                │  - system tray icon      │        │
│             │                │  - auto-update           │        │
│             │                │  - first-run wizard      │        │
│             │                └────────┬─────────────────┘        │
│             │                         │                          │
│             │                         │ HTTP (on LAN)             │
│             │                         ▼                          │
│  ┌──────────▼────────┐    ┌────────────────────┐                  │
│  │  Streaming site   │    │   Chromecast on    │                  │
│  │  (EgyDead etc.)   │    │   same Wi-Fi       │                  │
│  └───────────────────┘    └──────────┬─────────┘                  │
└──────────────────────────────────────┼──────────────────────────┘
                                       │ HDMI
                                       ▼
                                    TV screen
```

### Components

1. **Chrome extension** (refactored from the current prototype)
   - Sniffs URLs, reads cookies via `chrome.cookies.getAll()` (new permission), captures Referer/UA from webRequest
   - Sends `{ type: 'REGISTER_STREAM', url, cookies, headers }` to the desktop app via native messaging
   - Receives back a local URL like `http://127.0.0.1:38123/s/abc123/master.m3u8`
   - Also discovers Chromecasts via the desktop app: `{ type: 'LIST_CHROMECASTS' }`
   - On cast: `{ type: 'CAST', streamToken: 'abc123', device: 'Living Room TV' }`
   - Shows first-run status in popup: ✅ Desktop app running / ✅ Chromecast found / ✅ Ready to cast
   - If desktop app not installed: popup shows "Install Cast Booster for Windows" button linking to castbooster.com

2. **Desktop helper app** (the real product)
   - Runs as a Windows tray app (starts on login, minimized)
   - Native messaging host — Chrome talks to it via stdin/stdout JSON
   - Embedded HTTP server on `127.0.0.1:38123` AND `<LAN IP>:38123` (dual-bind so both browser and Chromecast can reach it)
   - Session-impersonating proxy: when the Chromecast fetches `<LAN IP>:38123/s/abc123/master.m3u8`, the app re-fetches the real URL using the cookies + headers the extension sent, rewrites any absolute URLs in HLS playlists to point back through the proxy, and streams bytes
   - mDNS Chromecast discovery via pychromecast (or equivalent)
   - Chromecast control via pychromecast (or equivalent): `play_media(local_url, content_type)`
   - First-run wizard (see Section 5)
   - "Is everything working?" diagnostic (see Section 5)
   - Auto-update mechanism
   - Telemetry (opt-in) for debugging / product analytics

3. **castbooster.com landing page + SaaS backend**
   - Hero + screenshots
   - "Download for Windows" button → signed installer
   - "Install Chrome extension" button → Chrome Web Store link
   - Ideally a single flow: installer installs the app, opens the Web Store page for the extension, and shows "almost done!" while user completes
   - Account system (email + password or Google OAuth)
   - Stripe integration for payments
   - License key generation and validation
   - Email service (SendGrid / Postmark / Resend) for welcome, onboarding, upgrade prompts, refund confirmations
   - Auto-update server (signed manifest + binary downloads)
   - Support ticket inbox

4. **Installer**
   - Inno Setup or NSIS for Windows
   - Signed with a real code-signing certificate (user confirmed budget allows)
   - Registers the native messaging host manifest at `HKEY_CURRENT_USER\SOFTWARE\Google\Chrome\NativeMessagingHosts\com.castbooster.app`
   - Installs Start Menu shortcut, registers auto-start on login
   - Post-install: opens browser to Chrome Web Store extension page
   - Handles uninstall cleanly (removes registry entries, stops tray process, wipes user data if user confirms)

---

## Section 4 — User Journey (plain English)

This is what Abed wants the first-time user to experience:

1. User lands on **castbooster.com**, reads the tagline ("Cast anything your browser plays to your Chromecast in real quality — not low-res tab mirror"), watches a 30-second demo video.
2. User clicks **"Download for Windows"**, gets `CastBoosterSetup.exe` (signed, no SmartScreen warning).
3. User runs installer, clicks through (~3 clicks), installer finishes.
4. Installer opens the Chrome Web Store page for the Cast Booster extension. User clicks **"Add to Chrome"**.
5. Extension installs. Browser notification: "Cast Booster is ready. Click the icon to start."
6. User opens their streaming site (say, EgyDead), starts a video, clicks the Cast Booster icon.
7. Popup shows:
   - ✅ Desktop app running
   - ✅ Chromecast found: "Living Room TV"
   - ✅ Video detected
   - A **"Cast to Living Room TV"** button.
8. User clicks the button.
9. Video starts playing on the TV in full quality within 2-3 seconds. Laptop tab shows a small "Now casting" indicator with play/pause/volume controls.
10. User closes laptop lid. Video keeps playing because the Chromecast is fetching from the desktop app's local HTTP server, which is still running in the tray.

### Failure paths and how the guide bot handles them

- **Desktop app not running**: popup button says "Start Cast Booster" → launches the tray app. If app isn't installed at all, button says "Install Cast Booster" → link to castbooster.com.
- **Firewall blocked**: desktop app detects its LAN port can't accept external connections, shows a Windows toast: "Windows Firewall blocked Cast Booster. Click to allow." with a button that re-triggers the firewall permission dialog. (User explicitly asked for this.)
- **Wi-Fi band mismatch**: laptop on 5 GHz, Chromecast on 2.4 GHz, different subnets. Desktop app's mDNS discovery fails. Bot shows: "Your laptop and Chromecast appear to be on different Wi-Fi networks. Many routers have two networks (one called XXX and one called XXX-5G). Connect your laptop to the same one as the Chromecast and try again." (User explicitly asked for this — "help the user understand this is their issue, not ours.")
- **DRM-protected content** (Netflix, Disney+, HBO Max): extension detects the site, does NOT show an error, instead shows: "Netflix already has its own casting app that works great, so Cast Booster doesn't need to support it. Just use the Cast button inside Netflix." (User explicitly wanted this framing — "don't say error, protect the pro vibe.")
- **Chromecast not found**: "No Chromecast detected on your network. Make sure your Chromecast is plugged in, powered on, and on the same Wi-Fi as this laptop."
- **Session-lock fallback failure**: if even the desktop app can't replay the URL, message: "This site's video is extra locked down. Try refreshing the page and casting again — sometimes the session expires quickly."

---

## Section 5 — First-Run Diagnostic Bot

User explicitly asked for this: a built-in "is everything working?" flow that walks new users through setup and detects common misconfigurations.

Checks to implement:

| # | Check | Failure message |
|---|---|---|
| 1 | Desktop app reachable | "Start Cast Booster from the system tray." |
| 2 | Chrome extension installed | "Install the Chrome extension from the Web Store." |
| 3 | Extension can talk to app via native messaging | "Restart Chrome and try again." |
| 4 | LAN port bindable (not blocked) | "Windows Firewall is blocking Cast Booster. Click here to fix it." |
| 5 | mDNS works (Chromecast discovery returns results) | "Is your Chromecast plugged in?" |
| 6 | Laptop and Chromecast on same subnet | "They look like they're on different Wi-Fi networks. See this help article." |
| 7 | Can successfully proxy a test URL to the Chromecast | "Try a different Chromecast, or report this bug." |

Run this flow:
- On first launch after install
- Manually via a "Run diagnostics" button in the popup or tray menu
- Automatically if the user hits an error during a real cast

---

## Section 6 — Tech Stack

### Decided

- **Target platform v1**: Windows 10/11 (x64). Mac and Linux come later if there's traction.
- **Extension language**: Vanilla JavaScript, no build step. Same as the current prototype. MV3.
- **Casting library (under the hood)**: **pychromecast** (home-assistant-libs/pychromecast). Active, maintained, de-facto standard. 10-line-script can make a Chromecast play any HTTP URL.
- **Chromecast discovery**: mDNS via pychromecast (built in), or `zeroconf` library directly.
- **Browser ↔ app communication**: **Chrome Native Messaging**. Same mechanism 1Password, LastPass, Bitwarden, Dashlane, Grammarly use. Google's official MV3 native messaging sample was merged in Feb 2024 and works cleanly.
- **Extension permissions needed (new)**: `nativeMessaging`, `cookies`, keep existing `webRequest`, `tabs`, `activeTab`, `storage`, `declarativeNetRequestWithHostAccess`, `host_permissions: ["<all_urls>"]`.
- **Installer**: Inno Setup (simpler than NSIS, widely used, free).
- **Code signing**: Windows Authenticode certificate, ~$150–$300/year. Sectigo or SSL.com. User said budget is fine.
- **Auto-update**: needs a small server-side component (signed manifest describing latest version). Consider `winsparkle` or homegrown.
- **Landing page**: Framework TBD in next session. Next.js + Vercel is a safe default. Must be fast, must load the demo video instantly, must have a big Windows download button above the fold.
- **SaaS backend**: TBD in next session. Candidates: Node + Express + Postgres on Railway / Render, or Supabase + Next.js API routes, or Django + Heroku. User wants 1M ARR scale planning so pick something that won't force a migration at 50K users.
- **Payments**: Stripe. No question.
- **Email**: Resend (modern, dev-friendly) or Postmark (reliable). Not SendGrid.

### TBD (pick in next session, after ICP research)

- **Desktop app language/runtime**: Four real candidates:
  1. **Python + PyInstaller**: fastest iteration, pychromecast is Python, huge ecosystem. Downsides: ~30 MB bundle, slower cold-start, harder to sign (but still possible).
  2. **Node.js + pkg/nexe**: can reuse extension JS patterns, single language across stack. Downsides: no first-party pychromecast equivalent (would need `castv2` or similar, less mature).
  3. **Go**: small single-file binary, easy cross-compile, great for HTTP proxy, has `go-chromecast`. Steeper learning curve.
  4. **Rust + Tauri**: smallest bundle, best performance, modern UI with web tech for the tray/settings window. Steepest curve.

  **Recommendation to make in next session**: Python first for v1 speed-to-market, plan to rewrite performance-critical parts in Go or Rust IF scale forces it. User said budget/timeline isn't tight — could also go straight to Go for longevity.

- **UI framework for the tray window and first-run wizard**:
  - If Python: PySide6 (Qt) for native look, or pywebview for HTML inside a native window
  - If Go: Wails or Fyne
  - If Rust: Tauri
  - If Node: Electron (but that adds Chromium weight — avoid if possible)

---

## Section 7 — Current State of the Codebase (post Phase 1)

The original Chrome-only prototype has been deleted. Reusable logic (URL classification, candidate ranking, DOM scanner) was extracted to `salvaged/NOTES.md` before deletion. The real architecture is in place and working.

### File inventory

```
Chrome-cast-extension/                      (project root — folder name kept for history; logically this is "cast-booster")
├── SESSION_HANDOFF.md                      ← THIS FILE
├── README.md                               How to run locally (dev-mode instructions)
├── .gitignore
│
├── app/                                    Python desktop app (tray + proxy + NM + Chromecast)
│   ├── requirements.txt                    aiohttp, pychromecast, pystray, Pillow, pytest
│   ├── castbooster/
│   │   ├── __init__.py                     __version__ = "0.1.0"
│   │   ├── __main__.py                     `python -m castbooster` entry
│   │   ├── main.py                         wires tray + asyncio loop thread + shutdown
│   │   ├── log.py                          rotating file handler at %LOCALAPPDATA%\CastBooster\
│   │   ├── netinfo.py                      LAN IP via UDP-connect trick + CASTBOOSTER_LAN_IP override
│   │   ├── tray.py                         pystray icon + menu (Quit, Open log)
│   │   ├── proxy.py                        aiohttp app: /health, /nm, /s/{token}/master.m3u8, /s/{token}/fetch, CORS
│   │   ├── session_store.py                token -> {upstream_url, cookies, headers, user_agent}
│   │   ├── hls_rewriter.py                 pure-text playlist URI rewriter (line-by-line, URI="..." attrs)
│   │   ├── caster.py                       pychromecast CastManager: discover + play_media + MediaStatusListener
│   │   └── nm_host.py                      Chrome Native Messaging stdio bridge (spawned per connectNative)
│   ├── scripts/
│   │   ├── nm_host.bat                     Launcher Chrome invokes (activates venv + runs nm_host)
│   │   ├── register_nm_host.ps1            HKCU registry key + generated manifest JSON
│   │   ├── unregister_nm_host.ps1
│   │   └── run_dev.bat                     Convenience: runs `python -m castbooster` from venv
│   └── tests/
│       ├── __init__.py
│       └── test_hls_rewriter.py            5 tests — absolute URIs, relative URIs, EXT-X-KEY attrs, roundtrip, blanks
│
├── extension/                              Chrome MV3 extension (unpacked for dev)
│   ├── manifest.json                       nativeMessaging, cookies, webRequest, tabs, activeTab, scripting, storage, <all_urls>
│   ├── background.js                       Service worker: sniffer + ranking + cookie/UA/Referer capture + NM orchestration
│   ├── content.js                          DOM video scanner + iframe detector (all_frames, debounced 400ms)
│   ├── popup.html                          Status + stream picker + device picker + Cast button
│   ├── popup.js                            Ping app, list_casts, build+render candidates, CAST_NOW orchestrator
│   ├── lib/
│   │   └── cookie_capture.js               chrome.cookies.getAll wrapper, dedup across URLs
│   └── icons/                              16/48/128 PNG (placeholders)
│
├── docs/
│   ├── protocol.md                         Native messaging wire protocol (ping, register_stream, list_casts, cast)
│   ├── vertical-slice-runbook.md           End-to-end setup + test procedure + failure-mode table
│   └── playback-controls-plan.md           ← NEXT SESSION: design for play/pause/seek/skip feature
│
├── cast-booster-market-analysis.pdf        18-page market research (kept as-is)
└── build_market_analysis_pdf.py            ReportLab script that generated the PDF (reference only)
```

### What works, verified by casting to a real Chromecast

- Extension sniffs HLS / DASH / MP4 / WebM URLs via webRequest in multiple frames + cross-origin iframes.
- Cookies captured for both the top-level page host AND the media URL's host; merged and shipped to the app.
- `User-Agent` and `Referer` captured via `onBeforeSendHeaders` with `extraHeaders` (mandatory in MV3).
- Candidate ranking: HLS master 110, DASH 95, MP4 full 85, variant 75, MP4 fragment 20, `.ts`/`.m4s` 10, ad CDN hosts get −100 penalty.
- Desktop app's aiohttp proxy binds `0.0.0.0:38123` with CORS headers (required for Chromecast's sandboxed receiver).
- Session-locked upstream fetch: cookies filtered by host-domain match, UA replayed, Referer preserved, `Accept-Encoding: identity` for playlists, `Range` forwarded for segments.
- HLS playlist rewriter preserves master→variant→segment chain and `#EXT-X-KEY`/`#EXT-X-MAP`/`#EXT-X-MEDIA` URI attributes.
- pychromecast discovery finds devices in ~3s via zeroconf; `play_media` + `block_until_active(15)` starts playback within ~5s.
- MediaController status listener logs `BUFFERING → PLAYING → IDLE/ERROR` transitions for post-cast diagnostics.

### What doesn't work yet

- **Playback controls**: no play/pause/seek/skip from the popup. Designed in `docs/playback-controls-plan.md`, to build next session.
- **Aggressive anti-leech sites** (e.g. `uqload.is`): upstream closes TCP after ~15s. Needs UA rotation / paced reads / ffmpeg mediator; Phase 2+ scope.
- **Session refresh** when a CDN URL expires mid-playback: currently the Chromecast just stops. No auto-reregister flow yet.
- **First-run diagnostic bot** (Section 5): not built.
- **Installer + code signing**: not built.
- **Landing page, payments, accounts**: not built.

### Non-obvious invariants (don't regress these)

- aiohttp's `add_get` already registers HEAD automatically — do NOT add explicit HEAD routes. Only register OPTIONS alongside GET (for CORS preflight).
- CORS headers must be on EVERY response the Chromecast might touch (`/s/{token}/master.m3u8`, `/s/{token}/fetch`, and OPTIONS preflight). Without them the Default Media Receiver silently rejects cross-origin MSE fetches.
- `zeroconf.Zeroconf()` must be a single instance shared with pychromecast's `CastBrowser`. Two instances fight for port 5353 on Windows.
- pychromecast `block_until_active` can take up to ~15s; call it via `loop.run_in_executor` so aiohttp stays responsive.
- `chrome.webRequest.onBeforeSendHeaders` requires the `'extraHeaders'` option in its `extraInfoSpec` or Chrome strips `Referer` / `User-Agent` / cookies from the callback.
- Cookies from `chrome.cookies.getAll` must be pulled for BOTH the top-page host AND the media URL's host; streaming sites often put session cookies on one and video CDN on another.
- Extension ID (`fkplchgkmmdjdlckdgilacamohhjpmdj`) is stable as long as the unpacked folder path doesn't move. If it does, re-run `app/scripts/register_nm_host.ps1 -ExtensionId <new-id>`.
- Native messaging host process must use `msvcrt.setmode(..., os.O_BINARY)` on Windows or stdio framing corrupts silently.
- The wake-lock MUST hold both `SetThreadExecutionState(ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)` AND `PowerSetRequest(PowerRequestExecutionRequired)`. On Modern Standby (S0 Low Power Idle) systems — every recent Windows 11 laptop, including Abed's — `ES_AWAYMODE_REQUIRED` is effectively a no-op because S3 sleep is disabled, so when the display turns off the OS enters Connected Standby and throttles user-mode processes to near-zero CPU. `PowerRequestExecutionRequired` is the only flag that keeps our proxy thread runnable across Connected Standby entry. Symptom of regression: intermittent cast freezes when the screen sleeps; log shows the WiFi keepalive `sent` counter barely incrementing over multi-minute windows, followed by `WinError 1232 "Network location cannot be reached"` (WiFi radio dissociates as a downstream effect). Verify after startup that the log shows `PowerCreateRequest ok (handle=0x...)` and, on every cast, `PowerSetRequest(type=3) ok`. Check supported sleep states with `powercfg /a`; if you see "Standby (S0 Low Power Idle)" listed under available states, Modern Standby is in play. (See `app/castbooster/wakelock.py` and `app/tests/test_wakelock.py`.)

---

## Section 8 — Business Model & Goals

### Agreed goals

- **Target ARR**: $500K–$3M (lifestyle business ceiling, one-person operation).
- **Stretch ceiling**: $1M ARR (user's own number). Tech stack must not block this.
- **Market**: Inherit stranded Videostream users (500K–700K). Videostream has been abandoned since ~2022; their reviews are full of "broken" and "developer never responds."
- **Primary channel**: Organic launch on r/Chromecast and r/Videostream subreddit. Follow up with r/cordcutters and anime/cordcutter Twitter.

### Pricing model (TBD — decide next session after ICP)

Three candidate models, in order of simplicity:

1. **One-time lifetime**: $29 one-time purchase. Low friction, inherits Videostream's old pricing, no churn to manage. Downside: no recurring revenue, must keep acquiring new users forever.
2. **Freemium**: free with limited casting time per day (30 min?) or watermark, $5/month or $29/year for unlimited. Standard SaaS model. More revenue per user over time.
3. **Plex-style Pro tier**: free for core casting, $4/month or $39/year for "Pro" features (multi-device, cloud sync, history, TV guide, captions). Long-term highest ceiling.

User leaning: start simple, evolve if traction shows. Don't over-engineer the pricing before you know who's buying.

### Legal safe zone (agreed)

- **Always say**: "Cast anything your browser plays."
- **Never say**: "cast Netflix/Disney+/HBO" or any pirate-adjacent keyword.
- **Hard rule**: Cast Booster cannot and does not circumvent DRM. Widevine-protected content (Netflix, Prime Video, Disney+, HBO Max, Apple TV+) will explicitly fail with a friendly message telling the user to use the service's own casting app. This keeps us out of DMCA-anticircumvention territory.
- The rest of the market — amateur streamers, regional mirrors, self-hosted content, non-DRM HLS — is legally fine to serve. Same legal position as VLC.

### Projected timeline (rough, from market PDF)

- **Month 0–3**: ICP research, build v1 desktop app + refactored extension, soft launch to small audience, 0–500 users.
- **Month 3–6**: launch on r/Chromecast, first paid tier, ~$1K MRR.
- **Month 6–12**: content marketing (YouTube tutorials, comparison articles), $1K → $5K MRR.
- **Month 12–24**: $5K → $50K MRR if traction holds. Maybe first contractor hire for support/marketing.
- **Month 24+**: $50K–$250K MRR = $500K–$3M ARR lifestyle business.

---

## Section 9 — Critical Constraints & Non-Negotiables

These came from direct user requests. Do not propose alternatives without explicit sign-off.

1. **Always use up-to-date tools**. No deprecated libraries. Check current stable versions before adding a dependency. Applies to pychromecast, hls.js, Node, Python, Chrome APIs, everything.
2. **Code-sign the installer**. User said budget is fine. Do it from day 1. Unsigned Windows binaries trigger SmartScreen warnings that kill conversion.
3. **Firewall failure handling**: if Windows Firewall blocks the LAN port, detect it and guide the user to fix it. Don't silently fail.
4. **Wi-Fi band handling**: if laptop and Chromecast are on different subnets, detect it and explain to the user that it's their router config (not our bug). Helpful, not blaming.
5. **DRM message framing**: never say "error" for DRM content. Instead: "This service has its own official app that works great, so we don't need to support it." Protects the professional vibe.
6. **First-run diagnostic bot**: walk new users through setup. Detect every common failure mode. This is how we beat Videostream (which had zero onboarding).
7. **Single integrated product**: user wants ONE thing to download, not "download app + install extension + configure both + troubleshoot firewall." The installer should do as much as possible automatically, and the first-run wizard should handle the rest.
8. **Layman-friendly everywhere**: error messages, onboarding, marketing, docs. Abed is the target user himself — if he'd be confused, a random user definitely will be.

---

## Section 10 — Agreements & Direct Quotes

Preserving exact user statements so future sessions can resolve ambiguity.

- On feasibility: *"Tell me first if this is even something we can fix or not before doing anything. Also, make sure if we are using any MCPs, tools, or APIs or whatever to run our extension that they are up to date."*
- On layman explanations: *"Also, there's a lot of terms that you used that I dont understand. You see im just a layman. Teach me all those technical terms you've mentioned thus far."*
- On the product form factor: *"I am thinking of turning this into like a website that runs this service for me instead of having it in a terminal and run it every single time. Is that doable? Be honest and straightforward."*
- On the full vision: *"The way I am thinking of implementing this is making an app. So the user journey would look something like: They go to the website at castbooster.com, They download an app instead of running something weird on a local server. Also with an extension that will be chatting with the app. So using both, when I open for example egydead, the extension catches the video, and the app handles the rest and makes it able to cast."*
- On scale and tech stack: *"Well this is a full stack app kinda where we store accounts, accept payments, handle email campaigns, refunds, etc. This could grow to a 1m ARR one day so we should start with the most suitable options."*
- On budget: *"The budget is not an issue"*
- On ICP: *"The ICP(Ideal customer profile) would need some reasearch to decide that. You listed some people who could be interested, but to figure out which one is the easiest to target we need to do more research. Questions like: Who is easier to reach? Who is more eager and has more passion? The one who has stronger emotions and connection to something is more likely to spend on it. Where do they hang out? On which social media? How easy is it to market to them and how? etc. This will help us qualify the most suitable ICP. Do the research, and come to a conclusion yourself based on the metrics I provided and other logics you think of."*
- On DRM framing: *"don't say 'error', say the service already has its own app so we don't support them (protects pro vibe)"* (paraphrased from summary; the user's preference is clear)
- On firewall/Wi-Fi UX: user explicitly asked for a "guide bot" that handles firewall denials, Wi-Fi band mismatches, and other first-run issues

---

## Section 11 — Market Research Summary (from the PDF)

The full 18-page analysis is at `cast-booster-market-analysis.pdf` in this folder. Key findings for quick reference:

- **Real demand confirmed**. Dozens of Reddit threads (r/Chromecast, r/cordcutters, r/Piracy, r/anime_piracy) asking "how do I cast X to my TV in good quality." High upvotes, no clean solution in comments.
- **Incumbent abandoned**. Videostream (500K–700K MAU at peak) hasn't shipped an update since ~2022. Support ticket inbox abandoned. Reviews trending negative. This is a "stranded user base" opportunity.
- **Competitive landscape**: Videostream (dead), Cast-It-Tab (abandoned), VLC (desktop-only, no browser integration), AllCast (mobile-only), LocalCast (mobile-only), various GitHub proxies (CLI-only). Nothing clean, modern, and browser-integrated exists.
- **Legal risk**: Manageable if we avoid DRM'd content explicitly. Same position as VLC. Cite DMCA §1201(f) and the historical non-enforcement pattern for general-purpose cast tools.
- **Revenue projections**: $500K–$3M ARR lifestyle ceiling realistic. $1M ARR achievable by year 2-3 with organic marketing if ICP research nails the targeting.

---

## Section 12 — IMMEDIATE NEXT-SESSION TASK: ICP Research

**Do this first. Do not start coding until this is done.**

User said: *"Do the research, and come to a conclusion yourself based on the metrics I provided and other logics you think of."*

### Candidates to evaluate

1. **Anime piracy fans** (EgyDead, 9anime, gogoanime, kissanime — whatever mirrors exist in 2026). Emotional attachment very high. Communities: r/anime, r/animepiracy, Discord servers, Twitter anime fan accounts. Budget: low per user but huge audience.
2. **Cord-cutters watching live sports** on unofficial streams (r/nbastreams, r/soccerstreams were banned but Rojadirecta / equivalent sites). Urgent emotional need (game is ON right now), willing to pay to avoid buffering. Budget: moderate to high.
3. **Foreign-language content viewers** (Arabic dramas on Shahid mirrors, Indian content on non-official sites, Korean drama sites, Turkish series). Diaspora communities, high passion, underserved by English-first SaaS tools. Budget: moderate.
4. **Self-hosted media enthusiasts** who already use Plex/Jellyfin and want easier casting from their browser when they're not at home. Tech-literate, high churn resistance. Budget: high per user but smaller TAM.
5. **General Chromecast users watching non-whitelisted content** — broadest bucket, includes YouTube-adjacent but not YouTube, Dailymotion, Vimeo, educational content, podcasts with video, etc. Least focused, hardest to target.
6. **Former Videostream users specifically**. Smallest TAM but highest intent — they literally already installed a product like this and lost it. Reachable via r/Videostream, review replies, direct outreach to old blog posts that recommended Videostream.

### Research method to use

- **Reddit scraping**: `audience-research` skill is available — use it to pull sentiment and volume from the top 5 candidate subreddits. Look for: post frequency, upvote distribution, commercial intent keywords ("willing to pay", "sick of using...", "anyone know a good..."), complaint clustering.
- **Exa / Brave Search**: trend data, community sizes, competing products in each vertical.
- **YouTube**: search "how to cast [site] to chromecast" and count results + view counts. High view count = high demand.
- **Twitter/X**: search same queries, look for recent tweets of frustration.
- **Google Trends**: relative search volume for "chromecast [anime/sports/drama]".

### Deliverable

A short ranked shortlist (1 page) with:
1. The winning ICP, one sentence of positioning, and why it beats the others.
2. The three subreddit/platform communities where they hang out.
3. The first 3 pieces of content or outreach we should produce to reach them.
4. The rough TAM estimate and realistic 12-month paid conversion assumption.

Then present it to Abed and let him confirm or redirect before we start building.

---

## Section 13 — Implementation Roadmap

### Phase 1: Vertical-slice proof-of-concept — ✅ DONE (2026-04-22)

Goal was: one session-locked streaming URL plays on a real Chromecast in optimized quality. End to end. Ugly UI is fine.

1. ✅ Picked desktop app language: **Python + PyInstaller**.
2. ✅ Scaffolded the desktop app: tray icon (pystray), aiohttp HTTP server on `0.0.0.0:38123`, LAN IP detection.
3. ✅ Scaffolded the refactored extension with `nativeMessaging` + `cookies` permissions. Thin `nm_host.py` stdio bridge forwards to running app; auto-launches app detached if not running.
4. ✅ Cookie + UA + Referer capture wired (pulls cookies for both top-page host and media URL host).
5. ✅ Session-aware HLS proxy: aiohttp ClientSession with captured cookies/headers + hand-rolled line-by-line playlist URI rewriter. 5 unit tests.
6. ✅ pychromecast 14 integrated: CastBrowser + SimpleCastListener for discovery, `play_media` + `block_until_active`, MediaStatusListener.
7. ✅ End-to-end verified: HLS stream from `masukestin.com` plays on "Dining room TV" Chromecast in full HD.
8. ✅ Non-obvious bugs caught + fixed in the session: aiohttp HEAD-route collision, missing CORS for Chromecast receiver sandbox, ad CDN poisoning the candidate picker.

### Phase 1.5: Playback controls — NEXT SESSION

Goal: while casting, the popup is a remote — play/pause/skip ±10s/seek bar/stop.

See `docs/playback-controls-plan.md` for the full design. Roughly:

1. Extend native messaging protocol with `media_cmd` and `media_status` types.
2. `CastManager` gets `control(uuid, action, ...)` and `get_status(uuid)` methods.
3. Popup detects active cast via `chrome.storage.local`; renders player UI instead of picker.
4. Poll media status every 1s while popup is open.
5. Button handlers send media commands through existing NM bridge.

### Phase 2: First-run polish & installer (2 weeks)

1. Build the first-run diagnostic bot with all 7 checks.
2. Build the installer (Inno Setup), register native messaging host, auto-start config.
3. Acquire code signing cert, integrate signing into the build pipeline.
4. Test clean install + clean uninstall on 3 different Windows 10/11 machines (spin up VMs).
5. Add auto-update scaffolding (even if the endpoint is just a static JSON on castbooster.com at first).

### Phase 3: Landing page & soft launch (2 weeks)

1. Build castbooster.com (Next.js on Vercel is the fast default).
2. Record the demo video (30 seconds, no narration, captions).
3. Stripe integration (one-time purchase first, can add subscription later).
4. Email integration (Resend) — welcome + receipt + help triggers.
5. Account system — defer if going one-time-lifetime only and license keys are email-based.
6. Soft launch to 10 beta users (pick from the ICP). Collect their screen recordings. Fix everything.

### Phase 4: Public launch (ongoing)

1. Post on r/Chromecast, r/Videostream, and the ICP's primary community.
2. Respond to every comment for the first 72 hours.
3. YouTube demo video.
4. Post-launch bug triage.
5. Start Phase 5 planning (pro tier, mobile app, Mac/Linux).

---

## Section 14 — What NOT To Do (lessons from the prototype phase)

- **Don't** try to solve session locking inside the extension. It's physically impossible. The desktop app is the only answer.
- **Don't** load external scripts from `extension_pages` CSP — MV3 forbids it. Use a sandbox page if you need external JS in an extension.
- **Don't** use `console.error` for recoverable failures — it pollutes `chrome://extensions` and looks broken even when it isn't.
- **Don't** show HLS fragments, `.m4s`, `.ts`, or any chunk-level candidates to users. They can't be cast alone and confuse people.
- **Don't** expose raw URLs in the UI without a friendly label ("HLS master", "MP4 full file") — users don't know what they're looking at.
- **Don't** ship without code signing. SmartScreen warnings kill the "pro vibe" and the funnel.
- **Don't** over-engineer pricing or account systems before launch. One-time $29 with email license keys is fine for v1. Subscriptions and accounts can be bolted on later.
- **Don't** forget the Videostream lesson: abandonment is how you lose a 500K-user business. Commit to shipping updates.
- **Don't** market to piracy keywords. Market to "cast anything your browser plays."

---

## Section 15 — Open Questions for Abed (ask at the start of next session)

Keep these short and batched so Abed doesn't feel grilled.

1. **ICP confirmation**: "I researched X ICPs and recommend Y because Z — does that match your gut?"
2. **Desktop app language**: "Python for fastest iteration or Go for longer-term performance? Pick one."
3. **Pricing**: "Start with one-time $29 lifetime, or freemium + $5/month? I lean one-time for v1."
4. **Brand**: "Is 'Cast Booster' the final name, or should we consider others before buying the domain?"
5. **Landing page copy**: "Want me to write the first draft of castbooster.com copy, or do you want to brief me on tone first?"

---

## Section 16 — Quick-Start Script for Next Session

When Claude opens the next session, it should:

1. Read this file (`SESSION_HANDOFF.md`) in full. Pay special attention to **Section 0.5 (Phase 1 Build Log)** and **Section 7 (Current State of the Codebase)** — the old prototype sections are gone, the shipped code is real.
2. Read `docs/playback-controls-plan.md` — this is the build plan for the next task.
3. Skim these files to get grounded in the working architecture:
   - `app/castbooster/proxy.py` (aiohttp server, HLS proxy, NM handler dispatch)
   - `app/castbooster/caster.py` (pychromecast wrapper — where `control()` and `get_status()` will be added)
   - `extension/popup.js` and `extension/popup.html` (where the player UI will live)
   - `extension/background.js` (NM orchestration — `sendNative` and `handleCastNow`)
   - `docs/protocol.md` (where new NM types will be documented)
4. Greet Abed with: *"Welcome back. Vertical slice is working — you verified playback on the Dining Room TV last session. Ready to build the playback controls (play/pause, skip ±10s, seek bar)? Plan is in `docs/playback-controls-plan.md`."*
5. Implement per the plan. Start with desktop-app side (caster + proxy handlers + protocol doc update), smoke test via curl, then wire the extension side.
6. After playback controls land, the next major track is ICP research (Section 12) → then Phase 2 (installer, code signing, auto-update) → then Phase 3 (landing page, payments).

**Dev env reminder**:
- Cast Booster extension ID: `fkplchgkmmdjdlckdgilacamohhjpmdj`. NM host already registered. Venv already created at `app/.venv`.
- To run the app: `cd app` then `.venv\Scripts\python -m castbooster`.
- To run tests: `cd app` then `.venv\Scripts\python -m pytest tests/ -v`.
- Logs: `%LOCALAPPDATA%\CastBooster\castbooster.log` and `nm_host.log`.

**Skill to use**: `executing-plans` (or `subagent-driven-development` for the independent NM + caster + popup tasks).

---

## Section 17 — File References

### Working docs
| File | Purpose |
|---|---|
| `SESSION_HANDOFF.md` | This file. Source of truth for design, build log, roadmap. |
| `README.md` | How to run locally (venv, extension, NM registration). |
| `docs/protocol.md` | Native messaging wire protocol. Grows as we add `media_cmd` / `media_status`. |
| `docs/vertical-slice-runbook.md` | End-to-end setup + manual test procedure + failure-mode table. |
| `docs/playback-controls-plan.md` | Design + implementation plan for the next feature. |
| `cast-booster-market-analysis.pdf` | 18-page market research PDF. Business context. |
| `build_market_analysis_pdf.py` | ReportLab script that generated the PDF. Reference only. |

### Python desktop app
| File | Purpose |
|---|---|
| `app/requirements.txt` | Locked-in deps: aiohttp, pychromecast, pystray, Pillow, pytest. |
| `app/castbooster/main.py` | Wires tray (main thread) + asyncio loop thread + clean shutdown. |
| `app/castbooster/proxy.py` | aiohttp server: `/health`, `/nm`, `/s/{token}/*`. CORS on all `/s/*`. |
| `app/castbooster/session_store.py` | In-memory `token -> StreamSession` map. |
| `app/castbooster/hls_rewriter.py` | Pure-text playlist URI rewriter (line-by-line, preserves fidelity). |
| `app/castbooster/caster.py` | pychromecast wrapper: discovery + `play_media` + MediaStatusListener. **Playback controls will land here.** |
| `app/castbooster/nm_host.py` | Chrome Native Messaging stdio bridge (spawned per `connectNative`). |
| `app/castbooster/netinfo.py` | LAN IP detection (UDP-connect trick + override). |
| `app/castbooster/tray.py` | pystray icon + menu. |
| `app/castbooster/log.py` | Rotating file log at `%LOCALAPPDATA%\CastBooster\`. |
| `app/scripts/register_nm_host.ps1` | HKCU registration. Required once per unpacked extension ID. |
| `app/scripts/nm_host.bat` | Launcher that Chrome invokes (activates venv + runs `nm_host.py`). |
| `app/scripts/run_dev.bat` | Convenience: runs `python -m castbooster` from venv. |
| `app/tests/test_hls_rewriter.py` | 5 unit tests for the URI rewriter (absolute, relative, EXT-X-KEY, roundtrip, blanks). |

### Chrome extension
| File | Purpose |
|---|---|
| `extension/manifest.json` | MV3. Perms: `nativeMessaging`, `cookies`, `webRequest`, `tabs`, `activeTab`, `storage`, `scripting`, `<all_urls>`. |
| `extension/background.js` | Service worker: sniffer + ranking + cookie/UA/Referer capture + CAST_NOW orchestrator + ad CDN downscoring. |
| `extension/content.js` | DOM `<video>` scanner + iframe detector (debounced 400ms, runs in all frames). |
| `extension/popup.html` + `popup.js` | Status, stream picker (with `⚠ ad CDN` labels), device picker, Cast button. **Player UI will land here.** |
| `extension/lib/cookie_capture.js` | `chrome.cookies.getAll` wrapper; merges cookies across multiple URLs. |
| `extension/icons/` | 16/48/128 PNG placeholders (real branding in Phase 2). |

---

## End of handoff document

If any part of this file is unclear or seems to contradict what Abed says in the next session, **trust Abed and ask for clarification**. This document captures state as of **2026-04-22** — Phase 1 shipped, playback controls are the next task (see `docs/playback-controls-plan.md`).

Phase 1 was a one-session build: design, clean slate, six milestones, three real bugs caught and fixed, end-to-end verified on a live Chromecast. Good foundation. Keep shipping.
