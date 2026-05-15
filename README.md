# Cast Booster

Cast video from any site your browser plays to a Chromecast in real quality —
not washed-out tab mirror.

This repo is **Phase 1** of the product: a working vertical slice that proves
the core tech (Chrome extension + Windows desktop helper + Chrome Native
Messaging + session-aware HLS proxy + pychromecast) actually casts from a
session-locked streaming site to a real Chromecast.

Phase 2+ adds the installer, code signing, auto-update, landing page, payments,
and onboarding. See `SESSION_HANDOFF.md` for the full product vision.

## Repo layout

| Path | What |
|---|---|
| `app/` | Python desktop app (tray icon, aiohttp proxy, pychromecast, native messaging host) |
| `extension/` | Chrome MV3 extension (sniffer, DOM scanner, popup) |
| `docs/` | Protocol spec and runbook |
| `salvaged/NOTES.md` | Logic salvaged from the Chrome-only prototype (delete once new extension is proven in the wild) |
| `cast-booster-market-analysis.pdf` | 18-page market research |
| `SESSION_HANDOFF.md` | Full product design doc from the scoping session |

## Quick start

See `docs/vertical-slice-runbook.md` for the full walkthrough. The short
version:

```bash
# 1. Install Python deps
cd app
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2. Launch the app
.venv\Scripts\python -m castbooster

# 3. Load extension in chrome://extensions (Developer mode → Load unpacked → extension/)
#    Copy the extension ID.

# 4. Register the native messaging host (paste your actual extension ID —
#    no angle brackets, PowerShell reads '<' as a redirect operator)
cd scripts
powershell -ExecutionPolicy Bypass -File .\register_nm_host.ps1 -ExtensionId abcdefghijklmnopabcdefghijklmnop

# 5. Restart Chrome. Open a video page. Click the extension icon. Cast.
```

## What works in Phase 1

- [x] Sniff HLS, DASH, MP4, WebM URLs via webRequest + DOM scanning
- [x] Capture browser cookies, User-Agent, Referer per sniffed request
- [x] Ship stream + session identity to the desktop app via Chrome Native Messaging
- [x] Local HTTP proxy on `0.0.0.0:38123` refetches upstream with captured session
- [x] HLS playlist URI rewriter (preserves master/variant/#EXT-X-KEY/#EXT-X-MAP chain)
- [x] Range-aware segment streaming for fMP4 + plain MP4
- [x] Chromecast discovery via pychromecast
- [x] `play_media` integration — TV plays from the proxy URL
- [x] Unit tests for the HLS rewriter

## What's not here yet

- Installer + code signing
- First-run diagnostic bot
- Auto-update
- Landing page, payments, accounts, email
- Mac / Linux builds
- Session refresh for expired CDN URLs
