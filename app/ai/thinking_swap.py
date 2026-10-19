"""Hard question -> a bigger installed model, when it really fits (AI_AUTO_SMART).

For requests the router calls "smart" (explanations, analysis, plans, multi-step tasks) and no AI_SMART_MODEL is set (or
it isn't installed), pick the largest installed chat model that

* can use tools (when tools are offered), is not an embedding model, is not resting after failures, and
* is bigger than the model that would answer anyway, and
* fits ENTIRELY in this PC's GPU memory (with room for the context and, if asked, the speech model). A model that spills
  onto the CPU answers 5-10x slower, so it is never chosen. If the GPU memory can't be read, nothing is swapped.

Facts only: the model's name is never used to judge quality. The swap is shown with the answer the first time, and
"which model are you using" reports it.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.ai.base import ModelInfo

HEADROOM = 1.25            # weights x this = what it needs on the GPU (context + buffers)
SPEECH_RESERVE_MB = 1024   # kept free for speech recognition when it shares a small GPU


def gpu_budget_mb(total_mb: float | None, reserve_speech: bool) -> float | None:
    if not total_mb or total_mb <= 0:
        return None
    return total_mb - (SPEECH_RESERVE_MB if reserve_speech and total_mb <= 8192 else 0)


def pick(default: ModelInfo, models: Sequence[ModelInfo], budget_mb: float | None, *, tools: bool,
         resting: Sequence[str] = ()) -> ModelInfo | None:
    """The bigger model to use for a hard request, or None (keep ``default``)."""
    if budget_mb is None or not default.size_bytes:
        return None
    candidates = []
    for m in models:
        if not m.can_chat or not m.size_bytes or m.name == default.name or m.name in resting:
            continue
        if tools and m.supports_tools is False:
            continue
        if m.size_bytes <= default.size_bytes:
            continue
        if m.size_bytes * HEADROOM / 1e6 > budget_mb:
            continue
        candidates.append(m)
    if not candidates:
        return None
    # thinking-capable first, then the largest that fits
    return max(candidates, key=lambda m: (m.supports_thinking, m.size_bytes or 0))
