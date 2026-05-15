"""Filter chain abstraction for castbooster.transcoder.

A FilterStage is anything with a render() -> str method. FilterChain composes
a sequence of stages into a single ffmpeg -vf fragment.

Built-in stages:
  NoopFilter        — identity, renders ffmpeg's null passthrough
  SubtitleBurnIn    — burns an embedded subtitle stream into the video frame
                      (added in Task 4)

P3 will add RIFEFilter; P4 will add Anime4KFilter.
"""
from __future__ import annotations

from typing import Protocol, Sequence


class FilterStage(Protocol):
    """A single ffmpeg -vf fragment producer.

    Implementations are stateless from the chain's POV — the chain calls
    render() at command-line-build time. render() MUST return a non-empty
    string. Use NoopFilter (renders "null") for the identity case.

    Optional method `_bind(input_url: str)` is called by FilterChain.render()
    before this stage's render() if present. SubtitleBurnIn uses this to
    learn the transcoder's input URL.
    """
    def render(self) -> str: ...


class NoopFilter:
    """Identity filter — renders ffmpeg's `null` passthrough."""
    def render(self) -> str:
        return "null"


class FilterChain:
    """Composes a sequence of FilterStages into a single -vf string.

    Empty chains default to a single NoopFilter. Chain.render(input_url) is
    called by the transcoder at argv-build time; it binds the input URL into
    any stage with a `_bind` method (currently SubtitleBurnIn).
    """

    def __init__(self, stages: Sequence[FilterStage] | None = None) -> None:
        self._stages: list[FilterStage] = list(stages) if stages else [NoopFilter()]

    def render(self, input_url: str) -> str:
        """Returns the full -vf fragment, never empty.

        - Single-stage chain: returns that stage's render() output.
        - Multi-stage chain: drops literal "null" fragments (cleaner output);
          if everything was null, returns "null".
        - Stages with _bind() get input_url before render().
        """
        for stage in self._stages:
            bind = getattr(stage, "_bind", None)
            if callable(bind):
                bind(input_url)
        parts = [s.render() for s in self._stages]
        if len(parts) > 1:
            non_null = [p for p in parts if p != "null"]
            parts = non_null or ["null"]
        return ",".join(parts)


def _escape_for_subtitles_filter(path: str) -> str:
    """Escape a filesystem path for use inside ffmpeg's subtitles= filter argument.

    Strategy per ffmpeg-all docs (filtergraph escaping §32.1):
      1. Normalize backslashes to forward slashes — ffmpeg accepts forward
         slashes on Windows AND it simplifies the escape rules.
      2. Escape colons with backslash — ffmpeg uses ':' as AVOption separator
         INSIDE a filter argument; the surrounding single quotes do NOT
         protect option-value colons. Windows drive letters (C:) require this.
      3. Wrap in single quotes — preserves ',' inside the option value.
      4. If the path contains a literal apostrophe, splice with '\\'' per the
         ffmpeg example "Crime d'\\''Amour".

    Examples:
        /tmp/sample.mkv  →  '/tmp/sample.mkv'
        C:\\foo\\bar.mkv  →  'C\\:/foo/bar.mkv'
        /x/Crime d'Amour.mkv  →  '/x/Crime d'\\''Amour.mkv'
    """
    path = path.replace("\\", "/")
    path = path.replace(":", "\\:")
    path = path.replace("'", "'\\''")
    return f"'{path}'"


class SubtitleBurnIn:
    """Burns an embedded subtitle stream into the video frame.

    Args:
        stream_index: zero-based subtitle stream index in the source.
                      Default 0 (first embedded sub track).

    The filter's input file is supplied via FilterChain._bind() — it must
    match the transcoder's -i target because ffmpeg's subtitles= filter
    re-reads the file to demux the sub stream.

    Render: subtitles=<escaped_input_url>:si=<stream_index>

    Requires ffmpeg compiled with --enable-libass. The vendored Gyan
    Windows build includes libass.

    External .srt / .ass file support (path argument instead of bound
    input_url) is deferred to P2.5+.
    """

    def __init__(self, stream_index: int = 0) -> None:
        self.stream_index = stream_index
        self._input_url: str | None = None

    def _bind(self, input_url: str) -> None:
        """Called by FilterChain.render() before this stage's render()."""
        self._input_url = input_url

    def render(self) -> str:
        if self._input_url is None:
            raise RuntimeError(
                "SubtitleBurnIn.render() called before FilterChain bound an input. "
                "Pass this stage to a FilterChain whose render(input_url) is called "
                "by the Transcoder."
            )
        escaped = _escape_for_subtitles_filter(self._input_url)
        return f"subtitles={escaped}:si={self.stream_index}"
