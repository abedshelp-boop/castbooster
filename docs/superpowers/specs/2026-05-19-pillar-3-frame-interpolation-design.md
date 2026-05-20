# Pillar 3 — Frame Interpolation (Design)

> Approved brainstorm output, 2026-05-19. Recommended approach only —
> alternatives considered are in the brainstorm transcript, not
> duplicated here. Source plan file:
> `~/.claude/plans/yo-we-have-finally-serene-hopper.md`.
>
> **2026-05-20 amendment (during P3.2 implementation):** D7's choice
> of `rife-anime` was empirically falsified at the gated test —
> rife-anime v1.8 refuses custom `-n`, stderr: *"only rife-v4 model
> support custom numframe and timestep"*. P3.2 switched the bundled
> model to **`rife-v4.6`** (still the same upstream `20221029`
> release zip). v4.6 supports arbitrary `-n target_count` and the
> Flowframes community considers it superior to rife-anime even for
> animation. P3.3 / P3.4 / P5 should treat the model variable as
> `rife-v4.6` everywhere D7 is referenced. Full context in
> `~/vault-global/claude-code/gotchas/decision-review-log.md` (2026-05-20).

---

## 1. Context

Third pillar of the "OP Upgrade" project. Pillar 2 shipped: ffmpeg-based
transcoder, hot-reloadable `FilterChain`, receiver capability table
(`ReceiverCaps.max_fps`), HLS re-segmenter, drag-seek-compatible playlist
type. **Pillar 3 adds the first user-visible quality enhancement:**
real-time frame interpolation via `rife-ncnn-vulkan` — anime cast as 60fps
instead of the source's 24fps.

The user's framing was "as high as we can and possible." Verified ceiling
is **60fps** — Chromecast firmware caps output there on every modern
device (Ultra, Google TV HD, Google TV 4K), and older Chromecasts (3rd gen
and prior) cap at 30. The real headroom isn't fps — it's *quality* (which
RIFE model), *consistency* (no dropped frames mid-action), and *coverage*
(graceful behavior on 30-cap and audio-only receivers).

Two seams from P2 are explicitly waiting on P3:

- **P2.3 D2 / encoder retuning**: "P3's fps change will trigger encoder
  retuning via a Transcoder-side hook, not a filter-owned flag."
- **P2.5 C3 / receiver caps**: "P3 (RIFE@60fps) is the first real
  consumer — it gates 60fps output on `max_fps == 60`."

Both are wired through in this design.

---

## 2. Decisions (locked this brainstorm 2026-05-19)

| # | Decision | Choice | Why |
|---|---|---|---|
| D1 | Integration architecture | **Segment-batch with continuous-I/O endpoints** — ffmpeg-decode + Python-batched rife + ffmpeg-encode, all connected by stdin/stdout pipes. Rife operates on per-segment frame folders internally because upstream `nihui/rife-ncnn-vulkan` is folder-only (no stdin/stdout). | Matches upstream README workflow + Flowframes architecture. ~2s latency is fine for VOD HLS (Chromecast already buffers 5–10s). |
| D2 | Target FPS UI | **Single "Smooth motion" toggle** — auto-picks receiver max (60 on Ultra/GTV; 30 on older 'Chromecast'). Toggle hidden on audio-only receivers. | Matches "as high as we can" framing. No granularity for users to misunderstand. |
| D3 | RIFE failure behavior | **Auto-demote to passthrough + non-blocking popup warning.** Cast survives. | Pro users deserve to know smoothness stopped. Uses existing `set_filter_chain(NoopFilter)` reload path. |
| D4 | Mid-cast toggle | **Hot-reload (~3-5s seek-like pause)** on toggle change. | Uses P2.3's `set_filter_chain` extended to multi-process slot swap. Sexy UX, infrastructure already exists. |
| D5 | Source FPS handling | ffprobe at session start. **Skip interpolation if source unknown/VFR/already ≥ target_fps.** CFR detection: `r_frame_rate == avg_frame_rate`. | Standard practice. Avoids RIFE on inputs it can't reason about. |
| D6 | Encoder retuning | **sqrt scaling of bitrate**: `new_bitrate = base × sqrt(target_fps / source_fps)`. | Empirical, perceptually-tuned. 24→60 ≈ 1.58× bitrate; 24→30 ≈ 1.12×. |
| D7 | Model variant | **`rife-anime`** bundled (single model). Exact version verified via context7 at impl time. | Anime-first product. P4 (Anime4K) covers general quality. |
| D8 | Premium gate | **Stubbed `license.is_pro() → True`** for now. P6 wires real Polar.sh check. **Defense in depth**: popup hides toggle if `!is_pro()`, proxy rejects `enable_smooth=True` if `!is_pro()`. | Builds the gate now; lights it up in P6 without retrofit. |
| D9 | GPU availability gate | **`license.vulkan_available()`** one-shot probe at service startup. Smooth toggle hidden if no Vulkan. | Cheap to add; Pillar 5 expands into full GPU tier system. |

---

## 3. Architecture

### 3.1 FilterStage Protocol extension

```python
# castbooster/filter_chain.py (MODIFIED)
class FilterStage(Protocol):
    def render(self) -> str: ...                          # existing -vf fragment
    def pipeline_spec(self) -> PipelineSpec | None: ...   # NEW; None = pure -vf filter

# castbooster/pipeline_spec.py (NEW)
@dataclass(frozen=True)
class PipelineSpec:
    encoder_input_format: str    # "rawvideo:yuv420p:WxH"
    target_fps: int              # output framerate; encoder gets -r and bitrate-scaled
    side_task_factory: Callable[[SideTaskContext], None]   # synchronous; transcoder runs in a Thread

@dataclass(frozen=True)
class SideTaskContext:
    input_url: str
    target_fps: int
    encoder_stdin: IO[bytes]     # write rawvideo here
    workdir: Path                # for temp frame batches
    cancel_event: threading.Event
```

Matches the existing thread-based concurrency model in `_ProcessSlot`
(no asyncio inside the transcoder). The side task is a synchronous function
that runs on its own `threading.Thread` and checks `cancel_event` in its
inner loop.

`NoopFilter` and `SubtitleBurnIn` keep returning `None` from
`pipeline_spec()` — fully backward-compatible.

### 3.2 RIFEFilter (NEW)

```python
# castbooster/filters/interpolation.py (NEW)
class RIFEFilter:
    def __init__(self, source_fps: float, target_fps: int,
                 width: int, height: int, model: str = "rife-anime"):
        ...

    def render(self) -> str:
        return "null"  # filtering happens upstream of the encode ffmpeg

    def pipeline_spec(self) -> PipelineSpec:
        return PipelineSpec(
            encoder_input_format=f"rawvideo:yuv420p:{self._w}x{self._h}",
            target_fps=self._target_fps,
            side_task_factory=self._side_task,
        )

    def _side_task(self, ctx: SideTaskContext) -> None:
        """Per-segment loop (runs on its own Thread, checks ctx.cancel_event):
            1. ffmpeg-decode pulls upstream HLS, writes rawvideo to stdout pipe
            2. We accumulate 2s frame batches → workdir/in/N/*.png
            3. subprocess.run rife-ncnn-vulkan -i in/N/ -o out/N/ -n <output_count>
                 -m <model> -g <gpu_id>
            4. Read out/N/*.png → write rawvideo bytes to ctx.encoder_stdin
            5. rmtree in/N, out/N (after lag)
        Cancellable via ctx.cancel_event; raises on subprocess failure so
        _ProcessSlot can mark the slot FAILED.
        """
```

### 3.3 Process topology per slot (when RIFE active)

```
upstream HLS m3u8
   │
   ▼
[Proc A] ffmpeg-decode: -i $URL -f rawvideo -pix_fmt yuv420p -
   │ stdout
   ▼
[Python side-task] RIFEFilter._side_task:
     reads rawvideo → batches PNGs → invokes rife → reads back → writes rawvideo
   │ stdin
   ▼
[Proc B] ffmpeg-encode: -f rawvideo -pix_fmt yuv420p -s WxH -r $TARGET_FPS -i -
                       -c:v $ENCODER -b:v $SCALED_BITRATE ... -hls ...
   │
   ▼
output/seg_%05d.ts  (served by proxy as today)
```

When RIFE is NOT active (`pipeline_spec() is None`), today's single-ffmpeg
argv runs unchanged.

### 3.4 `_ProcessSlot` extension (MODIFIED `castbooster/transcoder.py`)

`_ProcessSlot` today manages one `Popen` (encode ffmpeg). It becomes
"1 primary + N side":

```python
class _ProcessSlot:
    process: Popen                            # encode ffmpeg (primary)
    # NEW
    side_processes: list[Popen]               # decode ffmpeg, etc.
    side_task_thread: Optional[Thread]        # runs RIFEFilter._side_task
    side_task_exception: Optional[BaseException]   # captured for FAILED reason
    side_stderr_threads: list[Thread]

    def start(self, argv: list[str],
              pipeline_spec: PipelineSpec | None) -> None:
        if pipeline_spec is None:
            self.process = Popen(argv, ...)   # today's behavior
        else:
            self._spawn_pipeline(argv, pipeline_spec)

    def _spawn_pipeline(self, encoder_argv, spec):
        # 1. Spawn ffmpeg-decode (the side task launches it)
        # 2. Spawn encode ffmpeg with rawvideo stdin
        # 3. Launch side_task with SideTaskContext bound to encode.stdin
        # 4. Attach stderr readers to all processes
        # 5. Watchdog rule: any side proc dying before READY → slot FAILED
```

WARMING criterion unchanged: encode has produced 2 segments. But entering
WARMING now requires all side procs alive AND side task not crashed.

### 3.5 Hot-reload mid-cast (D4)

P2.3's `set_filter_chain` flow carries over with one extension: NEW
`_ProcessSlot` may spawn 3 procs instead of 1. The atomicity guarantees
hold: NEW reaches READY before OLD is killed. Demote on failure preserves
OLD.

State machine values from P2.3 (`SPAWNING`, `WARMING`, `READY`,
`STREAMING`, `STALLED`, `RELOADING`, `FAILED`, `TERMINATING`,
`TERMINATED`) carry over **unchanged** at the `Transcoder` aggregate
level. Complexity is contained inside `_ProcessSlot`.

---

## 4. FPS handling

### 4.1 Source video probe (MODIFIED `castbooster/ffmpeg_probe.py`)

```python
@dataclass(frozen=True)
class InputVideoInfo:
    fps: Optional[float]      # None if VFR or probe failed
    width: int
    height: int
    pix_fmt: str              # e.g. "yuv420p"

def probe_input_video(url: str) -> Optional[InputVideoInfo]:
    """ffprobe -show_streams -select_streams v:0 -of json
    - fps: r_frame_rate iff r_frame_rate == avg_frame_rate (CFR), else None
    - width/height/pix_fmt: from the video stream
    Returns None only if probe itself fails (network, malformed source).
    A successful probe with fps=None means CFR check failed → skip
    interpolation but still allow passthrough cast.
    """
```

Width/height/pix_fmt drive both the encoder argv (`-s WxH`, `-pix_fmt`)
and `RIFEFilter.__init__`. `fps=None` triggers the skip-interpolation
branch in §4.2.

### 4.2 Target FPS decision (MODIFIED `proxy._handle_cast`)

```python
caps = cast_manager.capabilities(uuid)
target_fps = caps.max_fps

video = ffmpeg_probe.probe_input_video(input_url)

if (caps.audio_only or not enable_smooth or not license.is_pro()
        or video is None):
    filter_chain = FilterChain([NoopFilter()])
elif video.fps is None:
    log.warning("VFR/unknown source FPS; skipping interpolation")
    filter_chain = FilterChain([NoopFilter()])
    # popup_info: "Smoothness skipped — source frame rate not detectable"
elif video.fps >= target_fps:
    log.info(f"source {video.fps} >= target {target_fps}; skipping")
    filter_chain = FilterChain([NoopFilter()])
else:
    rife = RIFEFilter(source_fps=video.fps, target_fps=target_fps,
                      width=video.width, height=video.height)
    filter_chain = FilterChain([rife])
```

### 4.3 Encoder retuning (D6, MODIFIED `_build_argv`)

```python
if pipeline_spec is not None:
    argv = [FFMPEG,
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-s", f"{w}x{h}", "-r", str(pipeline_spec.target_fps), "-i", "-",
            *audio_input,
            *encoder_flags,
            "-r", str(pipeline_spec.target_fps),
            "-b:v", str(_scaled_bitrate(BASE_BPS, source_fps,
                                        pipeline_spec.target_fps)),
            *hls_flags]

def _scaled_bitrate(base_bps: int, src_fps: float, tgt_fps: int) -> int:
    return int(base_bps * math.sqrt(tgt_fps / max(src_fps, 1.0)))
```

---

## 5. UI + premium gate

**Popup**:
- New "Smooth motion" toggle in `popup.html` — picker view (above Cast)
  AND player view (transport area).
- `popup.js` shows the toggle only if `is_pro() && vulkan_available()`
  (fetched once per popup open via nm_host → proxy).
- Toggle state persisted to `chrome.storage.local`.
- Mid-cast change → nm_host `set_filter_chain` → proxy → transcoder.
  Popup shows "Reloading smoothness…" loader.

**`castbooster/license.py` (NEW, minimal)**:
```python
def is_pro() -> bool:
    return True  # stub; P6 wires Polar.sh JWT validation

@functools.cache
def vulkan_available() -> bool:
    """One-shot probe: runs `rife-ncnn-vulkan -h` (or equivalent).
    False if binary missing, Vulkan ICD load fails, or no GPU.
    P5 expands into full GPU-tier system."""
```

**Defense in depth**: `proxy._handle_cast()` rejects requests with
`enable_smooth=True` if `!is_pro()`.

---

## 6. Failure handling

| Failure | Detection | Response |
|---|---|---|
| Vulkan not available at startup | `license.vulkan_available()` False | Smooth toggle hidden in popup |
| RIFE binary missing | `FileNotFoundError` on spawn | Demote to NoopFilter; popup warns "Smoothness unavailable" |
| Source FPS unknown / VFR | `input_framerate()` None | Cast with NoopFilter; popup info |
| Audio-only receiver | `caps.audio_only` | Reject at proxy; toggle disabled if audio_only device selected |
| RIFE crashes mid-cast | side proc dies / side task raises | Slot → FAILED → auto-demote via `set_filter_chain(NoopFilter)`; popup warns |
| RIFE can't keep up | existing STALLED detector | P5's watchdog hooks here; P3 just emits metrics |

New `_FATAL_PATTERNS`:
- `r"vulkan.*not.*found"` → `"vulkan_unavailable"`
- `r"failed to find.*Vulkan device"` → `"no_vulkan_gpu"`
- `r"model.*not.*found"` → `"rife_model_missing"`

(Exact patterns verified live at impl time.)

---

## 7. Watchdog data feed (Pillar 5 prep)

P3 emits these on `Transcoder` — Pillar 5 consumes them:
- `rife_fps_actual: Optional[float]` — fps RIFE is producing
- `rife_lag_seconds: Optional[float]` — encode wall-clock vs upstream
- `vulkan_device_name: Optional[str]` — picked GPU

---

## 8. Critical files

| Module | Status |
|---|---|
| `castbooster/filters/__init__.py` | NEW |
| `castbooster/filters/interpolation.py` | NEW |
| `castbooster/pipeline_spec.py` | NEW |
| `castbooster/filter_chain.py` | MODIFIED — `pipeline_spec()` on Protocol |
| `castbooster/transcoder.py` | MODIFIED — `_ProcessSlot` multi-proc, `_build_argv` rawvideo path, new fatal patterns, watchdog metrics |
| `castbooster/ffmpeg_probe.py` | MODIFIED — `+ probe_input_video()` |
| `castbooster/license.py` | NEW — `is_pro()` stub + `vulkan_available()` probe |
| `castbooster/proxy.py` | MODIFIED — `_handle_cast` reads `enable_smooth`, gates on caps + license, demote-on-failure path |
| `extension/popup.html` | MODIFIED — "Smooth motion" toggle |
| `extension/popup.js` | MODIFIED — toggle wiring + mid-cast hot-reload request |
| `extension/popup-styles.css` | MODIFIED |
| `extension/background.js` | MODIFIED — pass `enable_smooth` through |
| `extension/nm_host.py` | MODIFIED — pass `enable_smooth` + handle `set_filter_chain` requests |
| `castbooster/bin/rife-ncnn-vulkan.exe` | NEW (bundled) |
| `castbooster/models/rife-anime/` | NEW (bundled weights) |
| `app/tests/test_filter_chain.py` | MODIFIED |
| `app/tests/test_interpolation.py` | NEW (Vulkan-gated integration test) |
| `app/tests/test_transcoder_pipeline.py` | NEW |
| `app/tests/test_ffmpeg_probe_video.py` | NEW (covers `probe_input_video` — VFR detection, dims, errors) |
| `app/tests/test_license.py` | NEW |

---

## 9. Sub-pillar split (4 sessions per roadmap §7)

- **P3.1 — Pipeline abstraction (~1 session)**
  `pipeline_spec.py` + `FilterStage.pipeline_spec()` Protocol method +
  `ffmpeg_probe.probe_input_video()` + tests. No RIFE yet. Existing tests
  stay green (NoopFilter/SubtitleBurnIn unchanged).

- **P3.2 — RIFEFilter + binary bundling (~1-2 sessions)**
  `castbooster/filters/interpolation.py` + side task + bundled
  `rife-ncnn-vulkan.exe` + `rife-anime` model. `license.vulkan_available`
  probe. Vulkan-gated integration test casts a 10s clip end-to-end.

- **P3.3 — Multi-process `_ProcessSlot` (~1-2 sessions)**
  Extend `_ProcessSlot` for 1-primary + N-side topology. New
  `_FATAL_PATTERNS`. Hot-reload of multi-process slots (P2.3 R-equivalents
  for the multi-proc case, focused on NoopFilter ↔ RIFEFilter transitions).
  Auto-demote on side-proc failure.

- **P3.4 — UI + proxy wiring + premium gate (~1 session)**
  Popup toggle + popup-styles + nm_host bridge + proxy `enable_smooth`
  gating + `license.is_pro()` stub + popup warnings on demote.

---

## 10. Verification (Pillar 3 acceptance)

1. **Unit suite**: ~25-35 new tests across P3.1-P3.4. All green.
2. **Existing suite stays green**: ~177 tests from P2.5 baseline preserved.
3. **Vulkan-gated integration test** (`RUN_REAL_FFMPEG=1 RUN_RIFE=1`):
   cast a 30s anime test clip through RIFEFilter end-to-end; verify output
   HLS has `target_fps` frames per segment.
4. **Manual real-Chromecast acceptance** (the §7 roadmap DoD):
   - 30-min anime cast with Smooth ON → visibly smoother on real TV
   - Force-kill rife mid-cast → cast continues passthrough; popup warns
   - Toggle Smooth OFF mid-cast → ~5s pause → continues without smoothness
   - Cast on 30-cap "Chromecast" → target=30; mild smoothness; no error
   - Cast on Nest Hub (audio_only) → toggle hidden / disabled in popup
5. **Performance benchmarks on reference hardware**:
   - GTX 1660 (low): fps, lag, GPU util — feed Pillar 5 thresholds
   - RTX 3060 (mid): same
   - Intel iGPU: expect demote; verify graceful behavior
6. **State-machine enumeration (per IDLE-race lesson 2026-04-22)**:
   every new state in `_ProcessSlot` (multi-proc SPAWNING, multi-proc
   FAILED variants) gets at least one unit test exercising entry.

---

## 11. Out of scope (explicit)

- Anime4K shader upscale — Pillar 4
- GPU tier auto-detect (`gpu_probe.py`) — Pillar 5
- Performance watchdog hooks — Pillar 5 (P3 just emits metrics)
- Lite-mode UI for users with no compatible GPU — Pillar 5
- Real Polar.sh license validation — Pillar 6
- Live stream support — out of scope for v1 (roadmap §11 risk #3)
- macOS / Linux — Pillar 7+ if ever
- Live-action interpolation tuning — fallback only; rife-anime used as-is
- Multi-pass / scene-change-aware interpolation — v2

---

## 12. Open verifications (at impl time, not now)

These are deferred to the implementation sessions per CLAUDE.md hard
rule #2 (context7 before integration code):

- Exact `rife-anime` model name for current upstream release
- Exact `-n num-frame` vs `-s time-step` semantics for arbitrary 24→60
- ffmpeg rawvideo `pix_fmt` and stride/alignment for pipe input
- Bitrate base value for sqrt scaling (read current `_ENCODER_FLAGS`)
- rife-ncnn-vulkan exit codes on success / Vulkan failure / model missing
- Windows path escaping for rife `-i` and `-o` flags
- Whether `-pattern_type` is needed for the encode ffmpeg's input
- Decode ffmpeg's stdout pipe buffer size for backpressure tuning (Python
  reads at rife's pace; default pipe buffer is small on Windows)

---

## 13. Workflow

After spec approval (already done via ExitPlanMode 2026-05-19):

1. Spec lands in `docs/superpowers/specs/`. Commit on a branch
   (`pillar-3/spec`) — pending Abed's call on the uncommitted
   `extension/*` working state.
2. Open a fresh Claude Code session per roadmap §17 starter-prompt
   pattern. That session:
   - Reads this spec + the parent roadmap.
   - Runs context7 verifications of §12.
   - Invokes `writing-plans` to produce the P3.1 implementation plan.
   - Then `executing-plans` for P3.1.
3. Vault copy of `vision-2026-05.md` was supposed to happen at start of
   Pillar 1; if missed, P3.1 session can do it as a one-line side-quest.
4. Subsequent sub-pillars (P3.2, P3.3, P3.4) each get their own fresh
   session with their own brainstorm → spec → plan → exec cycle, anchored
   to this parent design.
