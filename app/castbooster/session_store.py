import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from castbooster.transcoder import Transcoder

log = logging.getLogger(__name__)


@dataclass
class StreamSession:
    token: str
    upstream_url: str
    cookies: List[Dict[str, Any]] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    created_at: float = field(default_factory=time.time)
    # NEW in P2.4 — all default None / False so existing tests don't need updates.
    transcoder: Optional["Transcoder"] = None
    output_dir: Optional[Path] = None
    passthrough_only: bool = False


class SessionStore:
    """In-memory map of token -> StreamSession. No persistence — sessions are
    scoped to app runtime. Stale entries get pruned by calling prune_older_than.
    """

    def __init__(self) -> None:
        self._by_token: Dict[str, StreamSession] = {}

    def create(
        self,
        upstream_url: str,
        cookies: Optional[List[Dict[str, Any]]] = None,
        headers: Optional[Dict[str, str]] = None,
        user_agent: str = "",
    ) -> StreamSession:
        token = uuid.uuid4().hex
        sess = StreamSession(
            token=token,
            upstream_url=upstream_url,
            cookies=list(cookies or []),
            headers=dict(headers or {}),
            user_agent=user_agent or "",
        )
        self._by_token[token] = sess
        log.info(
            "session created: token=%s cookies=%d headers=%d ua=%r url=%s",
            token[:8],
            len(sess.cookies),
            len(sess.headers),
            sess.user_agent[:60],
            upstream_url[:120],
        )
        return sess

    def get(self, token: str) -> Optional[StreamSession]:
        return self._by_token.get(token)

    def prune_older_than(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        stale = [t for t, s in self._by_token.items() if s.created_at < cutoff]
        for t in stale:
            del self._by_token[t]
        if stale:
            log.info("pruned %d stale sessions", len(stale))
        return len(stale)

    def __len__(self) -> int:
        return len(self._by_token)
