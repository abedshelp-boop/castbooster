# Drag-Seek Fix — `-hls_playlist_type vod` (Design Spec)

> Small follow-up to Pillar 2.4. Adds `-hls_playlist_type vod` to the
> transcoder's ffmpeg argv so Chromecast treats the output playlist as
> VOD (enabling absolute scrubber seek) instead of live (which disables
> the scrubber).
>
> **Parent docs:**
> - P2.4 design: [docs/superpowers/specs/2026-05-15-pillar-2.4-proxy-integration-design.md](2026-05-15-pillar-2.4-proxy-integration-design.md)
> - Issue origin: [next-session-prompt-p2-5-or-p3-handoff.md](../../../../../.claude/plans/next-session-prompt-p2-5-or-p3-handoff.md) — "Open issue A".
>
> **Status:** spec draft, awaiting user review before plan.

---

## 1. Context

After Pillar 2.4 shipped, every cast against a transcoded source plays
correctly via `/output/master.m3u8`. The Chromecast UI gives users the
`±10s` skip buttons, but **dragging the scrubber bar does not work** —
the playhead bounces back to the live edge.

Root cause: ffmpeg's HLS muxer, when invoked without
`-hls_playlist_type`, produces a playlist with **no** `EXT-X-PLAYLIST-TYPE`
tag and **no** `EXT-X-ENDLIST` until the process exits. Chromecast's
Shaka Player interprets that combination as a live stream and disables
absolute seek. (Only the ±10s relative-seek API stays enabled because it
operates on the player's buffer, not on a playlist timeline.)

The transcoder's input is always a VOD m3u8 (an anime episode, a movie),
so live semantics are wrong for our case. The user can't drag-seek even
though every segment is on disk and reachable.

This spec patches `_build_argv` to declare the output as VOD.

---

## 2. Decision

| # | Decision | Choice | Rationale |
|---|---|---|---|
| S1 | Which playlist-type value | `vod` | Common ffmpeg-on-Chromecast practice. Empirically enables drag-seek on Chromecast Default Receiver. Spec-pure alternative `event` (for growing playlists) is held in reserve. |
| S2 | Scope | Single flag in `_build_argv`; no segment-duration / keyframe-alignment changes | Stay narrow. Drag-seek is the only symptom we're solving. |
| S3 | Where in the argv block | Between `-hls_time` and `-hls_list_size` | Keeps the HLS flag cluster grouped + readable. ffmpeg parses the cluster order-agnostically. |
| S4 | Test coverage | Extend existing argv-assertion block in `test_transcoder_lifecycle.py` | No new test file. One `assert_adjacent("-hls_playlist_type", "vod")` next to the existing assertions. |

Options considered and rejected:

- **`-hls_playlist_type event`**. Semantically correct for our growing
  playlist (playlist may grow during transcode, then ENDLIST at exit).
  Spec-compliant. Held in reserve as a one-line fallback if `vod`
  triggers Chromecast misbehavior (e.g., player stops polling for new
  segments because VOD means "never changes").
- **Manually inject `#EXT-X-PLAYLIST-TYPE:VOD` into the master playlist**.
  Wrong layer — `master.m3u8` is the variant-list playlist, not the
  media playlist. Chromecast reads PLAYLIST-TYPE from the media playlist
  (`variant.m3u8`), which only ffmpeg writes.
- **Wider scope: revisit `-hls_time 2` (segment duration)**. Smaller
  segments give finer seek granularity but produce more files +
  metadata overhead. 2s is a reasonable balance for SW + HW transcoding
  at our current bitrates. Not addressed here.

---

## 3. Architecture

### 3.1 The change

[app/castbooster/transcoder.py](../../app/castbooster/transcoder.py) —
inside `_build_argv` (line 69), the HLS flag block currently reads:

```python
argv += [
    "-c:a", "copy",
    "-f", "hls",
    "-hls_time", str(hls_segment_seconds),
    # ... comment about hls_list_size 0 ...
    "-hls_list_size", "0",
    "-hls_flags", "independent_segments",
    "-hls_segment_filename", str(output_dir / "seg_%05d.ts"),
    str(output_dir / "variant.m3u8"),
]
```

Becomes:

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
    # 2026-05-18: list_size=0 (unlimited) + no delete_segments — see the
    # 2026-05-18 P2.4 fix; the Chromecast was 404'ing on segments ffmpeg
    # had already produced + deleted.
    "-hls_list_size", "0",
    "-hls_flags", "independent_segments",
    "-hls_segment_filename", str(output_dir / "seg_%05d.ts"),
    str(output_dir / "variant.m3u8"),
]
```

Net diff: +3 lines (1 comment block + 1 flag pair = 2 list entries).

### 3.2 State machine sanity (2026-04-22 IDLE-race lesson)

Per the lesson: enumerate every observable state, including startup +
transient, before declaring the change safe.

| Slot state | What's on disk | What the change affects |
|---|---|---|
| SPAWNING | nothing yet | argv constructed; flag is in the list |
| WARMING | `master.m3u8` exists (hand-written), `variant.m3u8` doesn't yet | unchanged — proxy hands the loopback URL to ffmpeg, ffmpeg starts writing `variant.m3u8` momentarily |
| READY | `variant.m3u8` exists with `EXT-X-PLAYLIST-TYPE:VOD` header + first segments listed, no ENDLIST | **the change**: Chromecast sees VOD header, enables scrubber |
| STREAMING | `variant.m3u8` continues to grow with new `seg_NNNNN.ts` entries, still VOD-typed, still no ENDLIST | Chromecast continues to fetch the playlist + new segments; player keeps the scrubber enabled |
| STALLED | playlist unchanged (no new segments) | unchanged from STREAMING for Chromecast purposes |
| RELOADING | new slot in `v<N+1>/` writes its own `variant.m3u8` with VOD header from scratch | unchanged behavior — each slot's playlist independently typed |
| TERMINATING | ffmpeg receives stop signal, writes `EXT-X-ENDLIST` if it has time before kill | clean exit ⇒ ENDLIST present ⇒ playlist is well-formed VOD; killed mid-write ⇒ no ENDLIST but `stop()` wipes `output_dir` anyway so the stale playlist never reaches the Chromecast |
| TERMINATED | `output_dir` wiped | nothing on disk to confuse anyone |

Conclusion: no startup or transient state is broken by the change. The
"growing VOD playlist" semantic is technically a spec violation (HLS
§4.4.3.5: "If the EXT-X-PLAYLIST-TYPE value is VOD, the Playlist file
MUST NOT change") but Chromecast/Shaka Player is empirically lenient
about this — they treat PLAYLIST-TYPE:VOD as "scrubber on" and continue
polling for playlist updates until ENDLIST appears.

### 3.3 Tests

[app/tests/test_transcoder_lifecycle.py](../../app/tests/test_transcoder_lifecycle.py),
the existing argv-assertion block (currently lines 185-194). Add one
assertion adjacent to the existing `-hls_time` check:

```python
assert_adjacent("-hls_time", "2")
# 2026-05-18: PLAYLIST-TYPE:VOD enables drag-seek on Chromecast.
assert_adjacent("-hls_playlist_type", "vod")
# 2026-05-18: list_size=0 (unlimited) + no delete_segments — see the
# 2026-05-18 fix; the Chromecast was 404'ing on segments ffmpeg has
# already produced + deleted.
assert_adjacent("-hls_list_size", "0")
```

No other test files touch the HLS flag set. Full suite must run green:
expected 169 passed + 5 skipped (same as post-P2.4 baseline).

### 3.4 Verification (real Chromecast — manual)

After merge:

1. Run castbooster locally: `cd app && .venv\Scripts\python -m castbooster`
2. Cast a known-good source (the masukestin/TikTok-CDN backend from
   P2.4 verification, or any anime stream that reaches the transcoder).
3. Wait for `state=PLAYING`.
4. **Drag the scrubber on the Chromecast UI** away from the current
   position. Confirm:
   - Playhead jumps to the target position (not back to live edge).
   - Playback resumes from the new position within ~2-3 seconds.
5. After cast stops, inspect `variant.m3u8` in the output dir
   (`C:\Users\Abeds\AppData\Local\Temp\castbooster-output\<token>\v0\variant.m3u8`
   or wherever the transcoder writes — verify path from log). Confirm:
   - First lines include `#EXT-X-PLAYLIST-TYPE:VOD`.
   - Last line is `#EXT-X-ENDLIST` (only present after clean exit).

If drag-seek doesn't work despite the VOD tag being written, the
fallback is to change `vod` → `event` in a one-line follow-up and
re-test.

---

## 4. Risk + rollback

**Risk: very low.**

Single flag addition to one argv builder. No new code paths, no state
transitions, no I/O changes. The flag is well-documented in the ffmpeg
HLS muxer docs and widely used in production Chromecast streaming
setups.

**Failure modes considered:**

1. **Chromecast misinterprets growing VOD playlist and stops polling.**
   Symptom: playback stops after the initial buffered window. Detection:
   visible within ~30s of cast start. Mitigation: fall back to
   `-hls_playlist_type event` (HLS-spec-compliant for growing playlists).
2. **ffmpeg signal-handler doesn't write ENDLIST on stop.** Symptom:
   stale `variant.m3u8` without terminator. Mitigation: not load-bearing
   in our pipeline because `stop()` wipes the output dir; the Chromecast
   never sees a post-mortem playlist.
3. **Test argv-assertion off by one position.** Symptom: test fails
   immediately, no production impact. Mitigation: visible at commit time.

**Rollback:** revert the single commit. No data migration, no schema
change, no protocol negotiation that could leave a Chromecast in an odd
state.

---

## 5. Out of scope

- Segment duration changes (`-hls_time 2` stays).
- Keyframe alignment changes (`-force_key_frames` already in argv).
- Master playlist changes (`master.m3u8` is hand-written, not produced
  by ffmpeg, and Chromecast reads PLAYLIST-TYPE from the variant
  playlist anyway).
- Issue C from the P2.4 handoff (`_BAD_SEG_CT_PREFIXES` `image/` dead
  code) — separate small cleanup commit if the user wants it.
- Audio handling + receiver capability negotiation — those are Pillar
  2.5, which begins after this fix verifies on a real cast.

---

## 6. Acceptance

Spec is "done" for implementation when:

1. The patched `_build_argv` produces an argv list that, when joined,
   contains `-hls_playlist_type vod` adjacent flag-value pair.
2. The existing test suite passes: 169 passed + 5 skipped.
3. The new `assert_adjacent("-hls_playlist_type", "vod")` assertion is
   present and passes.

Spec is "done" for shipping when:

4. Real Chromecast drag-seek manual verification passes (§3.4).
5. `variant.m3u8` on disk shows `#EXT-X-PLAYLIST-TYPE:VOD` in its header.
6. Commit merged to master.
