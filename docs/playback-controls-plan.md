# Playback Controls — Design & Implementation Plan

**Status**: not started. Built after vertical slice (Milestones A–F) is confirmed working end-to-end, which it is as of 2026-04-22.

**Goal**: while a cast session is active, the extension popup lets the user play/pause, skip ±10 seconds, seek anywhere by dragging a bar, see current time + total duration, and stop casting. Same experience as Videostream's remote — built on top of what we already have.

---

## 1. Context & what we already have

The vertical slice works: extension sniffs → app fetches upstream with cookies/UA/Referer → Chromecast plays from our LAN proxy. pychromecast's `MediaController` is already connected to each active device (we hold the connection in `CastManager._connections[uuid]` for the playback lifetime).

Everything we need to drive playback is already wired, just not exposed through the NM protocol:

- `mc.pause()` / `mc.play()` / `mc.stop()`
- `mc.seek(seconds)` — absolute position
- `mc.status.current_time` — float, seconds elapsed
- `mc.status.duration` — float, total seconds (nan for live streams)
- `mc.status.player_state` — `"PLAYING"` / `"PAUSED"` / `"BUFFERING"` / `"IDLE"`
- `mc.status.title` — from metadata if the receiver sent any

No Chromecast SDK changes needed. This is purely plumbing.

## 2. Protocol additions (docs/protocol.md)

Two new native messaging types:

### `media_cmd` — drive the active cast
**Request**
```json
{
  "type": "media_cmd",
  "castUuid": "72201756-13e5-41f4-8be2-e746647981fa",
  "action": "pause" | "play" | "stop" | "seek" | "skip_forward" | "skip_back",
  "seconds": 123.4                   // required for seek; optional delta for skip_*
}
```
**Response**
```json
{"type":"media_cmd_result","status":"ok"}
```
or
```json
{"type":"media_cmd_result","status":"error","detail":"no active session on device"}
```

### `media_status` — current playback snapshot
**Request**
```json
{"type":"media_status","castUuid":"..."}
```
**Response**
```json
{
  "type": "media_status_result",
  "state": "PLAYING",
  "currentTime": 432.1,
  "duration": 2730.0,
  "title": "Episode 12 - Dining Room Cut",
  "canSeek": true,
  "castUuid": "72201756-...",
  "deviceName": "Dining room TV"
}
```
`state=IDLE` + `currentTime=0` means the cast ended (Chromecast returned to home screen). Popup treats that as "not casting anymore" and offers to dismiss the controls.

## 3. Desktop app changes

### `app/castbooster/caster.py`
Add two methods on `CastManager`. Both are synchronous; called via `loop.run_in_executor`.

```python
def control(self, uuid_str, action, *, seconds=None, delta=None):
    """Dispatch play/pause/stop/seek/skip_* to the MediaController.
    Returns None on success, raises on failure."""
    conn = self._require_conn(uuid_str)
    mc = conn.media_controller
    if action == "pause":   mc.pause()
    elif action == "play":  mc.play()
    elif action == "stop":  mc.stop()
    elif action == "seek":
        if seconds is None: raise ValueError("seek requires seconds")
        mc.seek(float(seconds))
    elif action == "skip_forward":
        d = float(delta or 10)
        mc.seek(max(0.0, (mc.status.current_time or 0) + d))
    elif action == "skip_back":
        d = float(delta or 10)
        mc.seek(max(0.0, (mc.status.current_time or 0) - d))
    else:
        raise ValueError(f"unknown action: {action}")

def get_status(self, uuid_str):
    conn = self._require_conn(uuid_str)
    mc = conn.media_controller
    try: mc.update_status()      # nudge a refresh
    except Exception: pass
    s = mc.status
    return {
        "state": getattr(s, "player_state", "UNKNOWN") or "UNKNOWN",
        "currentTime": float(getattr(s, "current_time", 0) or 0),
        "duration": float(getattr(s, "duration", 0) or 0),
        "title": getattr(s, "title", "") or "",
        "canSeek": bool(getattr(s, "supports_seek", True)),
    }

def _require_conn(self, uuid_str):
    from uuid import UUID
    uuid = UUID(uuid_str)
    with self._lock:
        conn = self._connections.get(uuid)
    if conn is None:
        raise LookupError(f"no active session on {uuid_str}")
    return conn
```

### `app/castbooster/proxy.py`
Add two handlers, wire them into `_default_handlers()`:

```python
async def _handle_media_cmd(app, msg):
    uuid = msg.get("castUuid")
    action = msg.get("action")
    if not uuid or not action:
        return {"type":"media_cmd_result","status":"error","detail":"missing castUuid/action"}
    cm = app["cast_manager"]
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, lambda: cm.control(uuid, action,
                                     seconds=msg.get("seconds"),
                                     delta=msg.get("delta")),
        )
    except Exception as e:
        log.exception("media_cmd failed")
        return {"type":"media_cmd_result","status":"error","detail":str(e)}
    return {"type":"media_cmd_result","status":"ok"}

async def _handle_media_status(app, msg):
    uuid = msg.get("castUuid")
    if not uuid:
        return {"type":"media_status_result","state":"IDLE","error":"missing castUuid"}
    cm = app["cast_manager"]
    loop = asyncio.get_running_loop()
    try:
        status = await loop.run_in_executor(None, cm.get_status, uuid)
    except LookupError:
        return {"type":"media_status_result","state":"IDLE","currentTime":0,"duration":0,"title":"","canSeek":False}
    status["type"] = "media_status_result"
    status["castUuid"] = uuid
    # Device name lookup
    for d in cm.list_devices():
        if d["uuid"] == uuid:
            status["deviceName"] = d["name"]
            break
    return status
```

## 4. Extension changes

### `extension/background.js`
Nothing structural — media_cmd and media_status just flow through the existing `sendNative()` on `NM_SEND`. But add one convenience: on a successful `cast`, stash the active session in `chrome.storage.local`:

```js
// inside handleCastNow, after a successful cast reply:
await chrome.storage.local.set({
  activeCast: {
    castUuid: msg.castUuid,
    deviceName: (await sendNative({type:"list_casts"})).casts.find(c => c.uuid === msg.castUuid)?.name,
    token: reg.token,
    upstreamUrl: msg.url,
    startedAt: Date.now(),
  },
});
```

On popup open, read this to know whether to render the player UI vs the picker UI.

### `extension/popup.html`
Add a "Now casting" panel that lives above the candidate picker:

```html
<div class="group" id="playerArea" hidden>
  <div class="now-casting">
    <div class="title" id="mcTitle">—</div>
    <div class="device" id="mcDevice">—</div>
  </div>
  <div class="scrubber-row">
    <span id="mcCurrent">0:00</span>
    <input type="range" id="mcScrubber" min="0" max="100" value="0" step="0.1">
    <span id="mcDuration">--:--</span>
  </div>
  <div class="transport-row">
    <button id="mcBack10" class="icon">« 10s</button>
    <button id="mcPlayPause" class="icon primary">▶</button>
    <button id="mcFwd10" class="icon">10s »</button>
    <button id="mcStop" class="icon danger">Stop</button>
  </div>
</div>
```

### `extension/popup.js`
On init, check storage:
- If `activeCast` exists AND `media_status` returns a live state → render player UI, hide candidate picker.
- If `activeCast` exists but media_status returns IDLE/0/0 → clear storage, render picker.
- Otherwise → render picker as today.

Polling loop while popup is open:
```js
let pollTimer = null;
function startPolling(castUuid) {
  stopPolling();
  async function tick() {
    const r = await sendNM({type:"media_status", castUuid});
    if (!r || r.state === "IDLE" && !r.currentTime) {
      stopPolling();
      await chrome.storage.local.remove("activeCast");
      // re-render picker
      return;
    }
    updatePlayerUi(r);
    pollTimer = setTimeout(tick, 1000);
  }
  tick();
}
function stopPolling() { if (pollTimer) clearTimeout(pollTimer); pollTimer = null; }
```

Button handlers all go through `sendNM({type:"media_cmd", castUuid, action:"..."})`.

Seek bar: debounce drags so we don't flood the Chromecast — fire `media_cmd` on `change` event (when user releases), not `input` (which fires continuously).

Play/pause toggle: read current `state` from the last poll; if `PLAYING` → send `pause`, else `play`.

Format time as `M:SS` (under an hour) or `H:MM:SS` (over). Duration showing as `--:--` if `duration` is 0 or NaN (live stream or unknown).

## 5. Files touched

| File | Change |
|---|---|
| `docs/protocol.md` | Add `media_cmd` and `media_status` types |
| `app/castbooster/caster.py` | +`control()`, +`get_status()`, +`_require_conn()` |
| `app/castbooster/proxy.py` | +`_handle_media_cmd`, +`_handle_media_status`; add both to `_default_handlers()` |
| `extension/background.js` | Stash `activeCast` in storage on successful cast |
| `extension/popup.html` | Add `#playerArea` with scrubber + transport buttons |
| `extension/popup.js` | Two-mode rendering (picker vs player); polling loop; button handlers |
| `app/tests/test_caster_control.py` (optional) | Unit tests for `CastManager.control()` with a mocked MediaController |

## 6. Risks & edge cases

- **Chromecast goes offline mid-playback**: `mc.update_status()` raises or returns stale values. Wrap in try/except; popup handles IDLE reply by clearing storage and going back to picker. Show a toast: "Cast ended on Dining room TV".
- **User opens popup on a different Chrome profile/window**: storage is per-profile. If the other profile doesn't have `activeCast`, they'll see the picker — fine. We're not trying to sync across profiles for Phase 2.
- **Live streams (`duration=0`)**: hide the scrubber, still show play/pause/stop. Skip buttons may or may not work depending on whether the source supports seek in a live window.
- **Rapid pause/play spam**: debounce play/pause to 300ms on the client side so impatient users don't queue up 10 commands.
- **Drag scrubber fires too often**: use `change` event (fires on mouseup) rather than `input` (every pixel). Alternatively, debounce to 250ms between seeks.
- **Duration/currentTime from pychromecast come as `None` initially**: handle gracefully — show `--:--` until real values arrive.

## 7. Definition of Done

- Cast a video (any working site), open popup: player controls appear instead of the picker.
- **Play/Pause** toggles TV playback within 500ms of click.
- **Skip ±10s**: TV jumps 10 seconds forward or back.
- **Seek bar**: drag to anywhere in the timeline, release — TV jumps there.
- Current time updates every second while popup is open.
- **Stop** button: TV returns to idle home screen, popup clears activeCast and shows the picker again.
- Close popup, reopen 30s later: controls still show, current time is still updating, still in sync with TV.
- No new `ERROR` states in `castbooster.log` during a 5-minute playback session.

## 8. Out of scope for this task (deferred)

- Volume slider (could add as bonus — `mc.set_volume(0.0–1.0)` works — but not MVP)
- Subtitle toggle / track switching
- Queueing multiple videos
- Persistent history of what's been cast
- Popup-less control (e.g. from a system tray menu)
- Mobile / remote-over-LAN controls
