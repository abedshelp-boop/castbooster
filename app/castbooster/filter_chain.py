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
