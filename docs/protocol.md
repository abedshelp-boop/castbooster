# Cast Booster — Native Messaging Protocol

**Transport**: Chrome Native Messaging host `com.castbooster.host`. Each message is 4-byte little-endian length prefix + UTF-8 JSON body. Chrome enforces 1 MB max per request, 64 KB max per response.

**Bridge**: The native host process is a thin stdio forwarder. It reads a frame from Chrome's stdin, POSTs the JSON body to `http://127.0.0.1:38123/nm` on the running desktop app, writes the response body back to Chrome as a framed response. If the app isn't running, the host auto-launches it (detached) and waits up to 5s for `/health`.

---

## Requests

### `ping` — health check
**Request**
```json
{"type":"ping"}
```
**Response**
```json
{"type":"pong","version":"0.1.0","lanIp":"192.168.1.42"}
```

### `register_stream` — hand a sniffed URL + session identity to the app
**Request**
```json
{
  "type":"register_stream",
  "url":"https://cdn.example.com/.../master.m3u8",
  "cookies":[
    {"name":"sid","value":"...","domain":".example.com","path":"/","secure":true,"httpOnly":true,"sameSite":"lax"}
  ],
  "headers":{"Referer":"https://example.com/watch/123"},
  "userAgent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ..."
}
```
**Response (success)**
```json
{"type":"stream_registered","token":"f8a2...","playbackUrl":"http://192.168.1.42:38123/s/f8a2.../master.m3u8","contentType":"application/vnd.apple.mpegurl"}
```
**Response (error)**
```json
{"type":"error","detail":"upstream returned 403"}
```

### `list_casts` — enumerate Chromecast devices
**Request**
```json
{"type":"list_casts"}
```
**Response**
```json
{"type":"casts","casts":[{"uuid":"...","name":"Living Room TV","model":"Chromecast"}]}
```

### `cast` — play a registered stream on a device
**Request**
```json
{"type":"cast","token":"f8a2...","castUuid":"..."}
```
**Response**
```json
{"type":"casting","status":"ok","detail":"Playback started on Living Room TV"}
```
or
```json
{"type":"casting","status":"error","detail":"device not found"}
```

### `media_cmd` — drive playback on an active cast
**Request**
```json
{
  "type":"media_cmd",
  "castUuid":"72201756-13e5-41f4-8be2-e746647981fa",
  "action":"pause",
  "seconds":123.4,
  "delta":10
}
```

`action` is one of `"play"`, `"pause"`, `"stop"`, `"seek"`, `"skip_forward"`, `"skip_back"`.
- `seek` requires `seconds` (absolute position, float).
- `skip_forward` / `skip_back` default to a 10-second jump; pass `delta` to override.

**Response**
```json
{"type":"media_cmd_result","status":"ok"}
```
or
```json
{"type":"media_cmd_result","status":"error","detail":"no active session on 72201756-..."}
```

### `media_status` — current playback snapshot
**Request**
```json
{"type":"media_status","castUuid":"72201756-..."}
```
**Response**
```json
{
  "type":"media_status_result",
  "state":"PLAYING",
  "currentTime":432.1,
  "duration":2730.0,
  "title":"Episode 12 - Dining Room Cut",
  "canSeek":true,
  "castUuid":"72201756-...",
  "deviceName":"Dining room TV"
}
```
`state` is pychromecast's raw `player_state` (`"PLAYING"` / `"PAUSED"` / `"BUFFERING"` / `"IDLE"` / `"UNKNOWN"`). `duration` is `0` for live streams or when unknown. `state=IDLE` with `currentTime=0` and `duration=0` means the Chromecast returned to its home screen — treat that as "cast ended".

### `error` — error response envelope (any request may return this)
```json
{"type":"error","detail":"human-readable message"}
```

---

## Milestone coverage

| Milestone | Implemented | Stub |
|---|---|---|
| B | `ping` | `register_stream`, `list_casts`, `cast` return `{"type":"error","detail":"not implemented yet"}` |
| C | + `register_stream` (validate + store only, no upstream fetch yet) | `list_casts`, `cast` |
| D | + `register_stream` fully (upstream fetch + HLS rewrite) | `list_casts`, `cast` |
| E | + `list_casts`, `cast` | — |
| Playback controls | + `media_cmd`, `media_status` | — |
