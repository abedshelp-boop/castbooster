# Drag-Seek Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `-hls_playlist_type vod` to the transcoder's ffmpeg argv so Chromecast treats the output playlist as VOD and enables drag-seek on the scrubber.

**Architecture:** Single-flag addition in `_build_argv` in `app/castbooster/transcoder.py`. One new argv assertion in the existing test at `app/tests/test_transcoder_lifecycle.py::test_command_uses_libx264_flags_for_sw_tier`. No new files, no new modules.

**Tech Stack:** Python 3.14 + ffmpeg (HLS muxer). No new dependencies.

**Spec:** [`docs/superpowers/specs/2026-05-18-drag-seek-vod-playlist-type-design.md`](../specs/2026-05-18-drag-seek-vod-playlist-type-design.md). Read it first — especially §3.1 (the exact diff) and §3.3 (the exact test assertion).

**Branch:** `master` (single-commit fix on master per the new master-only workflow).

**Baseline before this plan:** 169 tests passing, 5 gated tests deselected (`RUN_REAL_FFMPEG=1`). After this plan: same counts; the change extends an existing test rather than adding a new one.

---

## File map

- Modify: `app/castbooster/transcoder.py` — `_build_argv`, the HLS flag block at lines ~95-109.
- Modify: `app/tests/test_transcoder_lifecycle.py` — `test_command_uses_libx264_flags_for_sw_tier`, the argv-assertion block at lines ~185-195.

No new files. No file splits. The change is too small to justify any structural reorganization.

---

## Task 1: Add `-hls_playlist_type vod` to argv via TDD

**Why this task is the whole plan:** The fix is a single ffmpeg flag in one argv builder. TDD cycle is short — failing assertion → minimal code → green.

**Files:**
- Modify: `app/castbooster/transcoder.py`
- Modify: `app/tests/test_transcoder_lifecycle.py`

---

- [ ] **Step 1: Write the failing test assertion**

Open `app/tests/test_transcoder_lifecycle.py`. Find the existing test `test_command_uses_libx264_flags_for_sw_tier` (starts at line 163). Find the assertion block starting at line 186 (`assert_adjacent("-hls_time", "2")`).

Insert a new assertion immediately after the `-hls_time` line. The block should become:

```python
    assert_adjacent("-hls_time", "2")
    # 2026-05-18: PLAYLIST-TYPE:VOD enables drag-seek on the Chromecast
    # scrubber. Without it the live-style playlist disables absolute seek.
    assert_adjacent("-hls_playlist_type", "vod")
    # 2026-05-18: list_size=0 (unlimited) + no delete_segments — see the
    # comment in transcoder._build_argv. Prevents the Chromecast from
    # 404'ing on segments ffmpeg has already produced + deleted.
    assert_adjacent("-hls_list_size", "0")
```

The new lines are the comment + the `assert_adjacent("-hls_playlist_type", "vod")` call. The `-hls_list_size` assertion and its comment are unchanged — they were already there.

---

- [ ] **Step 2: Run the failing test to verify it actually fails**

Run: `cd app && .venv/Scripts/python -m pytest tests/test_transcoder_lifecycle.py::test_command_uses_libx264_flags_for_sw_tier -v`

Expected output (key line):
```
ValueError: '-hls_playlist_type' is not in list
```
or
```
AssertionError: -hls_playlist_type should be followed by 'vod', got ...
```

The test MUST fail with a message that names `-hls_playlist_type`. If it passes, something is wrong (maybe the flag is already in the argv) — STOP and investigate before continuing.

---

- [ ] **Step 3: Add the flag to `_build_argv`**

Open `app/castbooster/transcoder.py`. Find `_build_argv` (starts at line 69). Find the HLS flag block at lines ~95-109. The current block:

```python
    argv += [
        "-c:a", "copy",
        "-f", "hls",
        "-hls_time", str(hls_segment_seconds),
        # 2026-05-18: 6-segment sliding window with delete_segments caused
        # 404s on /output/seg_NNNNN.ts when SW transcoding ran at 22x
        # realtime — ffmpeg deleted segments faster than the Chromecast
        # could fetch them. Keep all segments listed + on disk for the
        # duration of the session. output_dir is wiped on stop(), so disk
        # use is bounded by the session length (~200KB per 2s segment).
        "-hls_list_size", "0",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", str(output_dir / "seg_%05d.ts"),
        str(output_dir / "variant.m3u8"),
    ]
```

Insert one comment block + one flag pair between `-hls_time` and the existing `-hls_list_size` comment. The block becomes:

```python
    argv += [
        "-c:a", "copy",
        "-f", "hls",
        "-hls_time", str(hls_segment_seconds),
        # 2026-05-18: PLAYLIST-TYPE:VOD enables Chromecast drag-seek on the
        # output playlist. Without it the player treats the live-style
        # playlist as un-seekable and disables the scrubber UI. EXT-X-ENDLIST
        # gets written on clean ffmpeg exit (session stop or upstream EOF).
        "-hls_playlist_type", "vod",
        # 2026-05-18: 6-segment sliding window with delete_segments caused
        # 404s on /output/seg_NNNNN.ts when SW transcoding ran at 22x
        # realtime — ffmpeg deleted segments faster than the Chromecast
        # could fetch them. Keep all segments listed + on disk for the
        # duration of the session. output_dir is wiped on stop(), so disk
        # use is bounded by the session length (~200KB per 2s segment).
        "-hls_list_size", "0",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", str(output_dir / "seg_%05d.ts"),
        str(output_dir / "variant.m3u8"),
    ]
```

Net diff: +5 lines (4 comment lines + 1 flag-pair list entry which is 2 items on one line).

---

- [ ] **Step 4: Run the target test to verify it now passes**

Run: `cd app && .venv/Scripts/python -m pytest tests/test_transcoder_lifecycle.py::test_command_uses_libx264_flags_for_sw_tier -v`

Expected: PASS.

If it still fails, the most likely cause is a typo in the inserted argv entry (e.g., `"vod "` with a trailing space, or `"-hls-playlist-type"` with hyphens instead of underscores). Diff the change carefully against the spec snippet in §3.1.

---

- [ ] **Step 5: Run the full test suite to verify no regression**

Run: `cd app && .venv/Scripts/python -m pytest tests/`

Expected output (last line):
```
================ 169 passed, 5 skipped, ... warnings in ~16s ================
```

The counts must match the baseline exactly (169 passed, 5 skipped). If anything in the broader suite breaks, STOP — the change should be argv-additive only. Investigate before continuing.

---

- [ ] **Step 6: Commit on master**

```bash
git add app/castbooster/transcoder.py app/tests/test_transcoder_lifecycle.py
git commit -m "$(cat <<'EOF'
fix(transcoder): -hls_playlist_type vod enables Chromecast drag-seek

Chromecast was treating the transcoded output as a live playlist (no
EXT-X-PLAYLIST-TYPE tag, no EXT-X-ENDLIST until ffmpeg exit) and
disabling the absolute scrubber. ±10s skip kept working because that's
a buffer-relative seek, but dragging the playhead bounced back.

Adding -hls_playlist_type vod makes ffmpeg write EXT-X-PLAYLIST-TYPE:VOD
into variant.m3u8 from the first segment. Shaka Player (Chromecast's
underlying engine) interprets that as scrubber-enabled even while the
playlist is still growing. EXT-X-ENDLIST gets written on clean ffmpeg
exit, completing the VOD playlist.

Issue A from the P2.4 session handoff. Spec at
docs/superpowers/specs/2026-05-18-drag-seek-vod-playlist-type-design.md.

Verification: real-cast + scrubber drag — separate manual step before
declaring the fix shipped.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Real-Chromecast verification (manual, user-in-loop)

**Why a separate task:** This step needs the user's real Chromecast on the LAN. No automated test covers it. Cannot be done by a subagent.

**Owner:** Abed.

**Prerequisite:** Task 1 commit on master.

---

- [ ] **Step 1: Launch castbooster locally**

In PowerShell or git-bash from the repo root:

```bash
cd app && .venv/Scripts/python -m castbooster
```

Wait for the tray icon to appear and the proxy to log "listening on 127.0.0.1:38123".

---

- [ ] **Step 2: Cast a known-good source**

Open Chrome with the extension loaded. Navigate to a streaming site that goes through the transcoder (e.g. the masukestin/TikTok-CDN backend that worked end-to-end during P2.4 verification on 2026-05-18, or any anime stream that triggers the transcoder pipeline).

Click the extension popup → cast. Wait for `state=PLAYING` (visible on the TV and in `castbooster.log`).

---

- [ ] **Step 3: Drag the Chromecast scrubber**

On the Chromecast UI (visible on the TV, or via the Chromecast control card in Chrome's media controls):

- **Drag the scrubber away from the current position** (toward an earlier or later time within the encoded range).
- Confirm:
  - Playhead jumps to the target position. **Does NOT bounce back to live edge.**
  - Playback resumes from the new position within ~2-3 seconds.
- Also confirm:
  - The `±10s` skip buttons still work (they should — unchanged from before).

---

- [ ] **Step 4: Confirm `PLAYLIST-TYPE:VOD` is written**

While the cast is still running (or after stop), find the output dir from the log. Look for a line like:

```
ffmpeg argv: ... '-hls_segment_filename' 'C:/Users/Abeds/AppData/Local/Temp/.../v0/seg_%05d.ts' 'C:/Users/Abeds/AppData/Local/Temp/.../v0/variant.m3u8'
```

The `variant.m3u8` path tells you where ffmpeg is writing. Read the first ~20 lines of that file — they should include:

```
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:...
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-INDEPENDENT-SEGMENTS
#EXTINF:...
seg_00000.ts
...
```

The `#EXT-X-PLAYLIST-TYPE:VOD` line is the proof the flag took effect.

After the cast stops cleanly, the last line of `variant.m3u8` should be `#EXT-X-ENDLIST`.

---

- [ ] **Step 5: Report back**

If drag-seek works: report "drag-seek verified" — the fix ships, we move to P2.5 brainstorm.

If drag-seek doesn't work despite `PLAYLIST-TYPE:VOD` being written: the fallback is a one-line change from `vod` to `event` in `_build_argv` (re-run the test suite with the matching assertion update). Report the symptom and we'll iterate.

---

## Out of scope (do not do in this plan)

- Segment-duration tuning (`-hls_time 2` stays).
- Keyframe alignment changes (`-force_key_frames` already in argv).
- `_BAD_SEG_CT_PREFIXES image/` dead-code cleanup (Issue C from P2.4 handoff — a separate small commit if desired, not blocking this).
- Audio handling (Pillar 2.5).
- Receiver capability negotiation (Pillar 2.5).
