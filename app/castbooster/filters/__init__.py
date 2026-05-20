"""FilterStages with their own process topology.

This package houses FilterStages that go beyond a single -vf fragment:

  interpolation.RIFEFilter  — 24->60 fps via rife-ncnn-vulkan (P3.2)
  anime4k.Anime4KFilter     — shader-based enhancement (P4, not yet written)

Each filter returns a PipelineSpec from .pipeline_spec(), telling the
transcoder how to wire its inputs/outputs. See castbooster.pipeline_spec.
"""
