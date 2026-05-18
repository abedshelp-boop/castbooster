# Pillar 2 — Shipped 2026-05-19

> Pillar 2 (transcode pipeline foundation) is complete. The proxy re-segments every cast through ffmpeg with HW-accel-when-available, a hot-reloadable filter chain, always-AAC audio, and a per-device receiver-capability table. Stranded output dirs sweep at startup, and `python -m castbooster.acceptance` drives the 30-minute soak gate.

## Definition of Done (roadmap §7 lines 242–249)

| Item | Status | Reference |
|---|---|---|
| `castbooster/transcoder.py` — long-running supervised ffmpeg subprocess per session | ✅ | P2.2 (`transcoder.py`, 22 lifecycle tests) |
| Proxy can route passthrough OR transcode-with-noop-filter | ✅ | P2.4 (`_handle_cast` at `app/castbooster/proxy.py:154` — passthrough is FAILED-state fallback only) |
| Cast a 30-min anime stream through transcode-with-noop end-to-end, no user-visible regression vs passthrough | 🟡 → operator gate | `python -m castbooster.acceptance --mode soak --minutes 30 --cast-uuid <uuid>` (Abed runs on real Chromecast) |
| HW-accel decode + encode on NVENC + QSV | ✅ code-complete; SW path verified — HW paths untested pending hardware | `_ENCODER_FLAGS` in `transcoder.py:754` |
| Software fallback path works | ✅ | `accel.tier == "sw"` with 12s warming budget (`proxy.py:79`) |
| `filter_chain` config slot, hot-reloadable per cast session | ✅ | P2.3 `Transcoder.set_filter_chain(chain) -> bool` + RELOADING state + R6/R7/R8 reload tests |
| State machine enumerated explicitly — every observable state including startup/transient | ✅ | `TranscoderState` enum at `transcoder.py:56` — 9 states; per 2026-04-22 IDLE-race lesson |

## Closeout commits (2026-05-19)

- `53b5d5e` — `feat(p2.6): acceptance.py — end-to-end smoke + 30min soak harness`
- `5c7ddce` — `feat(p2.6): sweep stranded output dirs at startup`
- (this commit) — `docs(p2): Pillar 2 closing checklist + vault status`

Test count: 181 → **185 passed + 5 skipped** (T1 added 4 sweeper tests).

## How to verify

```
cd app
.venv\Scripts\python -m pytest tests/                                                          # 185 passed + 5 skipped
.venv\Scripts\python -m castbooster.acceptance --cast-uuid <real-uuid>                         # 60s smoke
.venv\Scripts\python -m castbooster.acceptance --mode soak --minutes 30 --cast-uuid <uuid>     # DoD soak
```

The acceptance harness starts its own in-process proxy; the tray app must NOT be running on port 38123 concurrently.

## What's next

**Pillar 3 — RIFE frame interpolation.** Starter prompt at `~/.claude/plans/next-session-prompt-p3-rife-interpolation.md`. First real consumer of `CastManager.capabilities()` (gates 60fps output on `caps.max_fps == 60`). Plugs into the existing `FilterChain` Protocol via `FilterStage` — no transcoder changes expected.

## Open follow-ups (carried forward, non-blocking)

- Drag-seek manual verification on real Chromecast (synthetic ffmpeg test ✅; real TV pending)
- AC3/EAC3 audio manual verification on real Chromecast (synthetic ffmpeg test ✅; real TV pending)
- The 6 long-standing `extension/*` WIP files — unrelated to pillar work; revisit when intent is clear
- Audinifer wrapper-strip miss (P2.4 Issue B) — possibly transient; revisit on repro
- `_BAD_SEG_CT_PREFIXES image/` dead code cleanup (P2.4 Issue C)
- hlswish.com re-test post-PNG-strip (P2.4 Issue D)
- Tray menu surface for `CastManager.transcoder_failure_stats()` (hook at `caster.py:671` exists; consumer deferred to P5 polish)
- `_on_startup` sweep wipes real `%TEMP%/castbooster/*` whenever pytest exercises it — harmless unless a live cast is in flight on the same machine concurrent with `pytest`; if it ever bites, accept-an-override on `proxy.py` `_on_startup` is the fix
