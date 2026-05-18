"""Chromecast discovery + play_media control via pychromecast.

pychromecast is a threaded sync library. All calls here block; the aiohttp
event loop invokes them through `loop.run_in_executor` so the loop stays
responsive.

Discovery is continuous — once `start()` is called, zeroconf keeps the device
list fresh in the background. Connections to individual casts are created
lazily on the first `play()` call and held in `_cast_connections` so the
session doesn't drop mid-stream.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import random
import socket
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import UUID

import pychromecast
import zeroconf
from pychromecast.discovery import CastBrowser, SimpleCastListener

from castbooster import wakelock

log = logging.getLogger(__name__)

# idle_reasons that mean "this session is over and the wake lock should
# release": FINISHED = played through, CANCELLED = user hit stop (popup or
# TV remote), ERROR = load failed, INTERRUPTED = another sender took over.
_TERMINAL_IDLE_REASONS = {"FINISHED", "CANCELLED", "ERROR", "INTERRUPTED"}


class _MediaStatusLogger:
    """Attached to MediaController to log state transitions after play_media.
    Tells us whether the TV is buffering / playing / idle-with-error.

    Also fires `on_idle` once when the session enters a terminal IDLE state
    (FINISHED / CANCELLED / ERROR / INTERRUPTED) so the wake lock can drop
    without the popup having to tell us.
    """

    def __init__(
        self,
        friendly_name: str,
        on_idle: Optional[Callable[[], None]] = None,
        log_token: Optional[str] = None,        # NEW in P2.4
    ) -> None:
        self.friendly_name = friendly_name
        self._on_idle = on_idle
        self._log_token = log_token             # NEW
        # The first status update after play_media often carries
        # IDLE/INTERRUPTED — that's the *previous* session being kicked
        # off the device by our LOAD, NOT our new session terminating.
        # If we fired on_idle on that, we'd tear down the wake lock and
        # WiFi keepalive for the session we just started. Wait until at
        # least one non-IDLE state is observed before treating any
        # IDLE/terminal as a real session end.
        self._seen_active = False
        self._first_playing_logged = False      # NEW
        self._cast_start_monotonic = time.monotonic()  # NEW

    def new_media_status(self, status) -> None:
        try:
            player_state = getattr(status, "player_state", "?")
            idle_reason = getattr(status, "idle_reason", "?")
            log.info(
                "[%s] media status: state=%s reason=%s ct=%s content=%s",
                self.friendly_name,
                player_state,
                idle_reason,
                getattr(status, "content_type", "?"),
                (getattr(status, "content_id", "") or "")[:120],
            )
            if player_state in ("BUFFERING", "PLAYING", "PAUSED", "LOADING"):
                self._seen_active = True
            if (
                player_state == "PLAYING"
                and self._log_token is not None
                and not self._first_playing_logged
            ):
                elapsed = time.monotonic() - self._cast_start_monotonic
                log.info(
                    "[cast token=%s] media_status first PLAYING after %.2fs",
                    self._log_token[:8], elapsed,
                )
                self._first_playing_logged = True
            if (
                self._seen_active
                and self._on_idle is not None
                and player_state == "IDLE"
                and idle_reason in _TERMINAL_IDLE_REASONS
            ):
                cb = self._on_idle
                # Fire once per terminal transition. The on_idle callback
                # itself is idempotent on the CastManager side, but we
                # null out the slot so subsequent status updates don't
                # keep calling release().
                self._on_idle = None
                try:
                    cb()
                except Exception:
                    log.exception("on_idle callback failed")
        except Exception:
            log.exception("status logger failed")

    def load_media_failed(self, item, error_code) -> None:
        log.error(
            "[%s] load_media_failed: item=%s error_code=%s",
            self.friendly_name,
            item,
            error_code,
        )


class CastManager:
    def __init__(self) -> None:
        self._zconf: Optional[zeroconf.Zeroconf] = None
        self._browser: Optional[CastBrowser] = None
        # Discovered devices, keyed by UUID. Value is pychromecast.models.CastInfo.
        self._casts: Dict[UUID, object] = {}
        # Live Chromecast connections, keyed by UUID, held for playback lifetime.
        self._connections: Dict[UUID, "pychromecast.Chromecast"] = {}
        # UUIDs for which we currently hold a wake-lock ref. Parallel to
        # _connections but separate because the ref-count is per-session,
        # not per-connection (we don't re-acquire when the user swaps
        # streams on a device we're already casting to).
        self._wakelock_held: Set[UUID] = set()
        # NEW in P2.4
        self._session_end_callbacks: Dict[UUID, Callable[[], None]] = {}
        self._transcoder_failure_counter: Dict[str, Any] = {"total": 0, "by_reason": {}}
        # END NEW
        # Per-UUID asyncio task that emits a small TCP packet to the
        # Chromecast every ~1.2s while the session is active. Forces the
        # WiFi radio's tx chain to stay warm regardless of AP power-save
        # hints — the OS-level wake lock can't reach that low. Mutated
        # ONLY on the proxy event loop (see attach_loop).
        self._keepalives: Dict[UUID, "asyncio.Task[None]"] = {}
        self._proxy_loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._zconf = zeroconf.Zeroconf()
        listener = SimpleCastListener(
            add_callback=self._on_add,
            remove_callback=self._on_remove,
            update_callback=self._on_update,
        )
        self._browser = CastBrowser(listener, self._zconf)
        self._browser.start_discovery()
        log.info("cast discovery started")

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Hand the proxy thread's event loop to the manager so keepalive
        tasks can be scheduled from caster's executor-thread callers.

        Idempotent. Called once from `proxy._on_startup`. Safe to call before
        any sessions exist.
        """
        self._proxy_loop = loop

    def stop(self) -> None:
        log.info("cast discovery stopping")
        # Kill keepalives synchronously BEFORE releasing the wakelock and
        # tearing down connections — otherwise they'd race against a
        # disappearing proxy loop and throw OSErrors into the log.
        self._cancel_all_keepalives_blocking()
        wakelock.force_release_all()
        if self._browser is not None:
            try:
                self._browser.stop_discovery()
            except Exception:
                log.exception("stop_discovery failed")
            self._browser = None
        with self._lock:
            for cast in self._connections.values():
                try:
                    cast.disconnect(blocking=False)
                except Exception:
                    pass
            self._connections.clear()
            self._wakelock_held.clear()
        if self._zconf is not None:
            try:
                self._zconf.close()
            except Exception:
                pass
            self._zconf = None

    def _on_add(self, uuid: UUID, _service: str) -> None:
        try:
            info = self._browser.devices[uuid] if self._browser else None  # type: ignore[index]
        except KeyError:
            info = None
        if info is None:
            return
        with self._lock:
            self._casts[uuid] = info
        log.info(
            "cast discovered: %s (model=%s uuid=%s)",
            getattr(info, "friendly_name", "?"),
            getattr(info, "model_name", "?"),
            uuid,
        )

    def _on_remove(self, uuid: UUID, _service: str, _cast_info: object) -> None:
        with self._lock:
            self._casts.pop(uuid, None)
            conn = self._connections.pop(uuid, None)
        log.info("cast removed: uuid=%s", uuid)
        self._release_wakelock_for(uuid)
        if conn is not None:
            try:
                conn.disconnect(blocking=False)
            except Exception:
                pass

    def _on_update(self, uuid: UUID, _service: str) -> None:
        if not self._browser:
            return
        try:
            info = self._browser.devices[uuid]  # type: ignore[index]
        except KeyError:
            return
        with self._lock:
            self._casts[uuid] = info

    def list_devices(self) -> List[Dict[str, str]]:
        with self._lock:
            out: List[Dict[str, str]] = []
            for uuid, info in self._casts.items():
                out.append(
                    {
                        "uuid": str(uuid),
                        "name": getattr(info, "friendly_name", "") or "",
                        "model": getattr(info, "model_name", "") or "",
                    }
                )
            return out

    def play(
        self,
        uuid_str: str,
        url: str,
        content_type: str,
        *,
        on_session_end: Optional[Callable[[], None]] = None,
        log_token: Optional[str] = None,
    ) -> str:
        """Block until the cast session is active. Returns the friendly name.

        Raises LookupError if the device isn't in the current discovery set,
        ValueError on malformed UUID, and any pychromecast exception on
        connection / controller failures.
        """
        try:
            uuid = UUID(uuid_str)
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid uuid: {uuid_str}") from e

        # P2.4: stash the on_session_end callback BEFORE play_media. Handles
        # the play-during-play race by firing any previously-registered
        # callback (whose transcoder must be torn down).
        if on_session_end is not None:
            self._register_session_end_callback(uuid, on_session_end)

        with self._lock:
            info = self._casts.get(uuid)
            conn = self._connections.get(uuid)
        if info is None:
            raise LookupError(f"no cast device with uuid {uuid_str}")

        if conn is None:
            log.info("connecting to cast %s", getattr(info, "friendly_name", uuid_str))
            if self._zconf is None:
                raise RuntimeError("CastManager.start() was never called")
            conn = pychromecast.get_chromecast_from_cast_info(info, self._zconf)
            conn.wait(timeout=10)
            with self._lock:
                self._connections[uuid] = conn

        mc = conn.media_controller
        name = getattr(info, "friendly_name", uuid_str) or uuid_str
        # Register a status listener (once per connection — re-registering is
        # a no-op if already present). Lets us see buffer/play/error events
        # from the Chromecast AFTER play_media has been dispatched. The
        # on_idle callback drops the wake lock if the TV ends / errors out
        # without the popup telling us.
        if not getattr(conn, "_cb_status_logger_attached", False):
            try:
                mc.register_status_listener(
                    _MediaStatusLogger(
                        name,
                        on_idle=lambda u=uuid: self._release_wakelock_for(u),
                        log_token=log_token,           # NEW in P2.4
                    )
                )
                setattr(conn, "_cb_status_logger_attached", True)
            except Exception:
                log.exception("failed to register media status listener")
        log.info(
            "play_media: url=%s content_type=%s",
            url[:120],
            content_type,
        )
        mc.play_media(url, content_type, stream_type=pychromecast.STREAM_TYPE_BUFFERED)
        mc.block_until_active(timeout=15)
        log.info("play_media: active on %s", name)
        # Keep Windows from suspending us while the TV is fetching segments
        # from the proxy. Done AFTER block_until_active so a failed session
        # doesn't leave a dangling hold.
        with self._lock:
            already_held = uuid in self._wakelock_held
            if not already_held:
                self._wakelock_held.add(uuid)
        if not already_held:
            wakelock.acquire(reason=f"Casting to {name}")
        # Spawn the WiFi-radio keepalive (per-UUID, deduped). Runs on the
        # proxy event loop. Cancelled in `_release_wakelock_for`.
        self._start_keepalive(uuid)
        # Nudge one status update so we get an immediate snapshot in the log.
        try:
            mc.update_status()
        except Exception:
            log.exception("update_status failed")
        return name

    def _release_wakelock_for(self, uuid: UUID) -> None:
        """Drop our wake-lock ref for a UUID if we hold one. Idempotent —
        safe to call from the stop button, the IDLE status listener, and
        `_on_remove` without double-releasing. Also tears down the per-UUID
        WiFi keepalive task (also idempotent). Finally, fires the
        on_session_end callback registered by play() (P2.4)."""
        with self._lock:
            had = uuid in self._wakelock_held
            if had:
                self._wakelock_held.discard(uuid)
        if had:
            wakelock.release()
        # Stop the keepalive whether or not we held the wake-lock — the
        # task may exist if play() succeeded and we were in a stream-swap
        # bookkeeping window.
        self._stop_keepalive(uuid)
        # P2.4: fire the on_session_end callback registered by play()
        with self._lock:
            cb = self._session_end_callbacks.pop(uuid, None)
        if cb is not None:
            try:
                cb()
            except Exception:
                log.exception("on_session_end callback failed (uuid=%s)", uuid)

    # ------------------------------------------------------------------
    # WiFi-radio keepalive
    #
    # Even with the OS wake lock held and every adapter power knob pegged,
    # the WiFi radio can still enter AP-negotiated 802.11 power-save during
    # the long quiet windows between segment fetches (10s+ for some streams).
    # When that happens, the next packet from the Chromecast can be dropped
    # and the cast freezes. We can't stop the AP from suggesting power-save,
    # but we CAN ensure our own outbound traffic never goes silent for
    # long enough — sub-DTIM-aging cadence keeps the tx chain warm and the
    # AP marks us awake on every TIM beacon.
    #
    # The task lifecycle parallels `_wakelock_held` exactly: spawned on a
    # successful `play()`, cancelled on stop / device removal / IDLE.
    # ------------------------------------------------------------------

    def _start_keepalive(self, uuid: UUID) -> None:
        """Schedule a per-UUID keepalive task on the proxy's event loop.

        Called from caster.py executor threads (where pychromecast lives).
        Uses run_coroutine_threadsafe to hop onto the proxy loop, which is
        the ONLY thread allowed to mutate `_keepalives`.
        """
        loop = self._proxy_loop
        if loop is None or loop.is_closed():
            log.debug("keepalive: no proxy loop — skipping uuid=%s", uuid)
            return
        try:
            asyncio.run_coroutine_threadsafe(self._spawn_keepalive(uuid), loop)
            # Fire and forget. The spawn coro is microsecond-scale.
        except RuntimeError:
            log.debug("keepalive: failed to schedule spawn", exc_info=True)

    async def _spawn_keepalive(self, uuid: UUID) -> None:
        """Runs ON the proxy loop. Sweeps done tasks, then spawns a new one
        if none is currently running for this UUID. Dedup by skip-if-present
        prevents stream-swaps from creating multiple keepalives per device."""
        # Reap finished tasks first so dedup doesn't see ghosts.
        for k, t in list(self._keepalives.items()):
            if t.done():
                self._keepalives.pop(k, None)
        existing = self._keepalives.get(uuid)
        if existing is not None and not existing.done():
            return
        self._keepalives[uuid] = asyncio.create_task(
            self._keepalive_loop(uuid), name=f"keepalive-{uuid}"
        )

    async def _keepalive_loop(self, uuid: UUID) -> None:
        """Per-UUID loop that opens a TCP socket to the Chromecast, closes it
        immediately, and sleeps ~1.2s. The connect itself is what matters —
        SYN/SYN-ACK/ACK + RST is enough outbound tx to keep the radio awake.

        SO_LINGER=0 forces an RST close instead of FIN, avoiding TIME_WAIT
        accumulation (~2400 sockets/hr at 1.2s cadence would otherwise burn
        ephemeral ports).

        Diagnostics: emits a 60s summary, escalating warnings on consecutive
        failures, and a per-iteration warning when RTT > 100ms (smoking gun
        for a radio that just woke from sleep).
        """
        interval = 1.2
        port = 8009  # CASTV2 control port; always open on a Chromecast.
        sent = 0
        failed = 0
        consecutive_failures = 0
        rtts: List[float] = []
        last_summary = time.monotonic()
        log.info("keepalive: starting uuid=%s", uuid)
        try:
            while True:
                # Re-resolve host every iteration so a Chromecast DHCP renew
                # doesn't strand us on a stale IP. Cheap dict lookup.
                info = self._casts.get(uuid)
                host = getattr(info, "host", None) if info is not None else None
                if not host:
                    # Either the device was removed from discovery or we
                    # haven't seen it yet. Pause briefly and re-check.
                    await asyncio.sleep(interval)
                    continue

                t0 = time.monotonic()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # SO_LINGER l_onoff=1 l_linger=0 → RST on close (no TIME_WAIT).
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )
                sock.setblocking(False)
                try:
                    await asyncio.wait_for(
                        asyncio.get_running_loop().sock_connect(sock, (host, port)),
                        timeout=1.0,
                    )
                    rtt_ms = (time.monotonic() - t0) * 1000.0
                    sent += 1
                    rtts.append(rtt_ms)
                    consecutive_failures = 0
                    if rtt_ms > 100.0:
                        log.warning(
                            "keepalive: slow rtt uuid=%s host=%s rtt=%.1fms",
                            uuid,
                            host,
                            rtt_ms,
                        )
                except (asyncio.TimeoutError, OSError) as e:
                    failed += 1
                    consecutive_failures += 1
                    if consecutive_failures in (1, 5, 30):
                        log.warning(
                            "keepalive: %d consecutive failures uuid=%s host=%s err=%s",
                            consecutive_failures,
                            uuid,
                            host,
                            e,
                        )
                finally:
                    try:
                        sock.close()
                    except Exception:
                        pass

                now = time.monotonic()
                if now - last_summary >= 60.0:
                    avg = (sum(rtts) / len(rtts)) if rtts else 0.0
                    mx = max(rtts) if rtts else 0.0
                    log.info(
                        "keepalive: uuid=%s sent=%d failed=%d avg_rtt_ms=%.1f max_rtt_ms=%.1f",
                        uuid,
                        sent,
                        failed,
                        avg,
                        mx,
                    )
                    rtts.clear()
                    last_summary = now

                # Jitter prevents synchronization with AP beacon intervals.
                await asyncio.sleep(interval + random.uniform(-0.1, 0.1))
        except asyncio.CancelledError:
            log.info(
                "keepalive: cancelled uuid=%s sent=%d failed=%d", uuid, sent, failed
            )
            raise

    def _stop_keepalive(self, uuid: UUID) -> None:
        """Cancel the per-UUID keepalive task on the proxy loop. Fire-and-forget.

        Called from `_release_wakelock_for` (which fans out from stop button,
        IDLE status, and device-remove paths). Idempotent.
        """
        loop = self._proxy_loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._cancel_keepalive(uuid), loop)
        except RuntimeError:
            log.debug("keepalive: failed to schedule cancel", exc_info=True)

    async def _cancel_keepalive(self, uuid: UUID) -> None:
        """Runs ON the proxy loop. Pops and cancels the task, if any."""
        task = self._keepalives.pop(uuid, None)
        if task is not None and not task.done():
            task.cancel()
            # Don't await it here — a single CancelledError propagates fine
            # without us blocking the spawn slot.

    def _cancel_all_keepalives_blocking(self, timeout: float = 2.0) -> None:
        """Synchronously cancel every keepalive and wait for them to settle.

        Called from `stop()` on shutdown, BEFORE we release the wakelock,
        so we don't half-tear-down the loop while tasks are still racing.
        """
        loop = self._proxy_loop
        if loop is None or loop.is_closed():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._cancel_all(), loop)
            fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log.warning("keepalive: timed out waiting for cancel-all")
        except RuntimeError:
            log.debug("keepalive: cancel-all dispatch failed", exc_info=True)

    async def _cancel_all(self) -> None:
        """Runs ON the proxy loop. Cancels every task and awaits completion."""
        tasks = list(self._keepalives.values())
        self._keepalives.clear()
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _require_conn(self, uuid_str: str) -> "pychromecast.Chromecast":
        try:
            uuid = UUID(uuid_str)
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid uuid: {uuid_str}") from e
        with self._lock:
            conn = self._connections.get(uuid)
        if conn is None:
            raise LookupError(f"no active session on {uuid_str}")
        return conn

    def control(
        self,
        uuid_str: str,
        action: str,
        *,
        seconds: Optional[float] = None,
        delta: Optional[float] = None,
        volume: Optional[float] = None,
    ) -> None:
        """Dispatch play/pause/stop/seek/skip_*/set_volume to the cast device.

        Raises LookupError if there's no active session, ValueError on bad args,
        and any pychromecast exception on controller failures.
        """
        conn = self._require_conn(uuid_str)
        mc = conn.media_controller
        if action == "pause":
            mc.pause()
        elif action == "play":
            mc.play()
        elif action == "stop":
            mc.stop()
            try:
                self._release_wakelock_for(UUID(uuid_str))
            except (ValueError, TypeError):
                # _require_conn already validated the UUID above — if we
                # got here, parsing can't fail. Swallow defensively.
                pass
        elif action == "seek":
            if seconds is None:
                raise ValueError("seek requires 'seconds'")
            mc.seek(float(seconds))
        elif action == "skip_forward":
            d = float(delta if delta is not None else 10)
            current = float(getattr(mc.status, "current_time", 0) or 0)
            mc.seek(max(0.0, current + d))
        elif action == "skip_back":
            d = float(delta if delta is not None else 10)
            current = float(getattr(mc.status, "current_time", 0) or 0)
            mc.seek(max(0.0, current - d))
        elif action == "set_volume":
            if volume is None:
                raise ValueError("set_volume requires 'volume'")
            # Receiver-level volume, clamped to [0, 1]. pychromecast exposes
            # this directly on the Chromecast instance (bound from the
            # receiver_controller).
            conn.set_volume(max(0.0, min(1.0, float(volume))))
        else:
            raise ValueError(f"unknown action: {action}")

    def _register_session_end_callback(
        self,
        uuid: UUID,
        callback: Optional[Callable[[], None]],
    ) -> None:
        """Stash an on_session_end callback. If one was already registered for
        this uuid (play-during-play race), fire the previous one outside the
        lock so its transcoder doesn't leak.

        Passing callback=None pops any existing registration WITHOUT firing it
        (used to clear without triggering teardown — e.g. tray-menu reset).

        The previous callback must be idempotent. Transcoder.stop() is idempotent
        by design (P2.2) so this contract is satisfied for our usage.
        """
        prev: Optional[Callable[[], None]] = None
        with self._lock:
            if callback is not None:
                prev = self._session_end_callbacks.get(uuid)
                self._session_end_callbacks[uuid] = callback
            else:
                # Explicit None — clear any registration WITHOUT firing.
                self._session_end_callbacks.pop(uuid, None)
        if callback is not None and prev is not None and prev is not callback:
            try:
                prev()
            except Exception:
                log.exception(
                    "previous on_session_end callback failed during overwrite (uuid=%s)",
                    uuid,
                )

    def record_transcoder_failure(self, reason: str) -> None:
        """Increment the failure counter for `reason`. Safe to call from any thread."""
        with self._lock:
            self._transcoder_failure_counter["total"] += 1
            br = self._transcoder_failure_counter["by_reason"]
            br[reason] = br.get(reason, 0) + 1

    def transcoder_failure_stats(self) -> Dict[str, Any]:
        """Return a deep-copy snapshot of the failure counter. Tray menu (P2.7)
        + tests both use this; callers must NOT mutate the returned dict."""
        with self._lock:
            return {
                "total": self._transcoder_failure_counter["total"],
                "by_reason": dict(self._transcoder_failure_counter["by_reason"]),
            }

    def get_status(self, uuid_str: str) -> Dict[str, object]:
        conn = self._require_conn(uuid_str)
        mc = conn.media_controller
        try:
            mc.update_status()
        except Exception:
            # Non-fatal: return whatever the last snapshot had.
            log.debug("update_status during get_status failed", exc_info=True)
        s = mc.status
        duration = getattr(s, "duration", None)
        # pychromecast returns NaN for live streams — JSON-encode as 0 instead.
        try:
            duration_f = float(duration) if duration is not None else 0.0
            if duration_f != duration_f:  # NaN check
                duration_f = 0.0
        except (TypeError, ValueError):
            duration_f = 0.0
        # Receiver-level volume comes off the cast device's CastStatus,
        # not the MediaStatus — MediaStatus.volume_level is a separate field
        # that tracks per-stream volume and isn't what set_volume() changes.
        receiver_status = getattr(conn, "status", None)
        volume_level = 0.0
        if receiver_status is not None:
            try:
                volume_level = float(getattr(receiver_status, "volume_level", 0) or 0)
            except (TypeError, ValueError):
                volume_level = 0.0
        return {
            "state": getattr(s, "player_state", "UNKNOWN") or "UNKNOWN",
            # idle_reason distinguishes "just loading" (None) from "user stopped"
            # (CANCELLED) / "video ended" (FINISHED) / "error" (ERROR). The popup
            # uses this to avoid mistakenly clearing the session during startup.
            "idleReason": getattr(s, "idle_reason", None) or "",
            "currentTime": float(getattr(s, "current_time", 0) or 0),
            "duration": duration_f,
            "title": getattr(s, "title", "") or "",
            "canSeek": bool(getattr(s, "supports_seek", True)),
            "volume": volume_level,
        }
