# Vertical Slice Runbook

How to stand up Cast Booster locally and verify the whole chain works, from
extension click to Chromecast playback. Phase 1 is dev-only — no installer,
no code signing, no landing page.

## One-time setup

### 1. Python environment

```bash
cd app
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 2. Load the extension in Chrome

1. Open `chrome://extensions`.
2. Toggle **Developer mode** on (top right).
3. Click **Load unpacked** and pick the `extension/` folder at the repo root.
4. Copy the extension ID shown under "Cast Booster" (32 lowercase `a`–`p` chars).

### 3. Register the native messaging host

From a PowerShell window (no admin required). Replace the example ID with your actual one — **no angle brackets**, PowerShell treats `<` as a redirect operator:

```powershell
cd app\scripts
powershell -ExecutionPolicy Bypass -File .\register_nm_host.ps1 -ExtensionId abcdefghijklmnopabcdefghijklmnop
```

This writes `com.castbooster.host.generated.json` alongside `nm_host.bat` and
adds an `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.castbooster.host`
registry entry pointing at the manifest.

**Restart Chrome** afterward — native messaging hosts are discovered only at
browser startup.

### 4. Launch the desktop app

Either double-click `app\scripts\run_dev.bat`, or from `app/`:

```bash
.venv\Scripts\python.exe -m castbooster
```

On first launch Windows will prompt once to allow the app through the firewall
on private networks. Accept. After that the tray icon (a small blue/white "CB")
shows in the system tray.

The app also auto-launches via the native messaging host the first time the
extension opens a port, so launching it by hand is optional.

## Smoke tests

Each test is independent and can be run after the corresponding milestone is in
place.

### A. App skeleton
- **Run**: desktop app is running.
- **Expect**: `curl http://127.0.0.1:38123/health` returns `{"ok":true,"version":"0.1.0","lanIp":"..."}`. Same response from the LAN IP (replace 127.0.0.1) from a phone on the same Wi-Fi.

### B. Native messaging bridge
- **Run**: click the extension icon in Chrome.
- **Expect popup shows**: `Connected: app v0.1.0 on 192.168.x.y` (green status).
- **Troubleshooting**: if popup says "Desktop app not running" with a hint about registering, re-run step 3 above and restart Chrome. Check `%LOCALAPPDATA%\CastBooster\nm_host.log` for host-side errors.

### C. Cookie capture + register_stream
- **Run**: open an EgyDead episode page in Chrome, start playback, open the extension popup.
- **Expect**: a "Video stream" dropdown appears with at least one sniffed candidate (usually "HLS master • sniffed"). Clicking **Cast** (if a Chromecast is listed below) kicks off the full flow. See E.
- **Log check**: `%LOCALAPPDATA%\CastBooster\castbooster.log` shows a line like:
  ```
  session created: token=... cookies=N headers=1 ua='Mozilla/5.0 ...' url=https://...
  ```

### D. HLS proxy
This can be tested without a Chromecast, using VLC on a phone.

1. Register a stream via the popup (click Cast — if there's no device it will fail at the cast step, but the playback URL is printed in `castbooster.log` above).
2. Grab the `playbackUrl` from the log and open it in VLC on a phone on the same Wi-Fi. Example: `http://192.168.1.42:38123/s/<token>/master.m3u8`.
3. **Expect**: video plays on the phone, streamed through the proxy. This proves the session-aware fetch + HLS rewrite works.

Unit tests for the rewriter:

```bash
cd app
.venv\Scripts\python -m pytest tests/ -v
```

### E. Chromecast discovery + cast
- **Run**: Chromecast is powered on, on the same Wi-Fi as the laptop. Open the extension popup on a video page, wait ~3s for discovery.
- **Expect**: "Cast to" dropdown lists the device. Clicking **Cast** with both stream + device selected makes the TV play the video within ~10s.
- **App log**: look for `cast discovered: <name>` and then `play_media: active on <name>`.

### F. End-to-end demo
- Fresh Chrome restart → open the target streaming site → click extension icon → pick candidate → pick device → click Cast.
- Success = video playing on the TV in optimized quality (full HD, native aspect ratio) within ~10s. This is the vertical-slice demo.

## Resetting state

- **Stop the app**: right-click the tray icon → Quit. Or `taskkill /F /PID <pid>` where pid is whatever's bound to 38123 (`netstat -ano | findstr :38123`).
- **Unregister the native host**: `powershell -ExecutionPolicy Bypass -File app\scripts\unregister_nm_host.ps1`.
- **Nuke session cache**: stop the app. Sessions are in-memory only.
- **Clear logs**: delete `%LOCALAPPDATA%\CastBooster\*.log`.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Popup: "Specified native messaging host not found" | `register_nm_host.ps1` not run, or wrong extension ID | Re-run registration with the exact ID, then restart Chrome |
| Popup: "Native host has exited" | Host crashed before responding | Check `%LOCALAPPDATA%\CastBooster\nm_host.log` |
| /health reachable on 127.0.0.1 but not LAN IP | Windows Firewall block on private networks | Accept the one-time firewall prompt, or add the rule manually |
| "No Chromecasts detected" | Laptop on 5 GHz SSID, Chromecast on 2.4 GHz SSID | Ensure both devices are on the same Wi-Fi network (not just the same SSID family) |
| Proxy returns 502 on master.m3u8 | Session-locked URL expired (EgyDead-class, ~1-6h lifetime) | Refresh the page and re-cast |
| VPN on → LAN IP is the VPN tunnel IP | Chromecast can't reach a VPN address | Disable VPN, or set `CASTBOOSTER_LAN_IP` env var to the real LAN IP |
