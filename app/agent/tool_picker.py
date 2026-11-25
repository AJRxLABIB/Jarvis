"""Two-step tool choice (opt-in, TWO_STEP_TOOLS): first pick WHICH tools, then call one with its full schema.

A small model chooses better from a short list. Step 1 shows it only names and one-line summaries and forces the reply into
``{"tools": [names...]}`` (schema-constrained, so it cannot invent a name); step 2 is the normal chat with only those tools' full schemas (the
"basic" tools always stay). Any failure - no AI, a bad reply, nothing picked - returns the router's original list unchanged, so this can only
narrow the choice, never break a request. It costs one extra short model call per request that has many candidate tools.

Honest status: the published gains for splitting selection from arguments came with fine-tuned adapters; this prompt-only version is UNMEASURED.
Leave it off unless your own comparison shows it helps.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Sequence

from app.ai.base import ChatMessage, ToolSpec

MIN_CANDIDATES = 9          # fewer than this: the list is already short, skip the extra call
MAX_PICK = 4
TIMEOUT = 30.0


def one_line(spec: ToolSpec, limit: int = 110) -> str:
    first = re.split(r"(?<=[.!?:])\s", " ".join((spec.description or "").split()), maxsplit=1)[0]
    return f"{spec.name}: {first[:limit]}"


def schema_for(names: Sequence[str]) -> dict[str, Any]:
    return {"type": "object", "properties": {"tools": {"type": "array", "items": {"type": "string", "enum": list(names)}, "maxItems": MAX_PICK}}, "required": ["tools"]}


async def narrow(ai: Any, text: str, specs: Sequence[ToolSpec], keep: Sequence[str] = ()) -> list[ToolSpec]:
    """The specs to offer after the model picked by name. ``keep`` (always-on tools) is never removed. Returns ``specs`` unchanged on any problem."""
    specs = list(specs)
    pickable = [s for s in specs if s.name not in set(keep)]
    if ai is None or not hasattr(ai, "structured") or len(pickable) < MIN_CANDIDATES or not (text or "").strip():
        return specs
    listing = "\n".join(one_line(s) for s in pickable)
    try:
        data = await asyncio.wait_for(ai.structured([
            ChatMessage.system("Pick the tools needed to do what the user asks. Reply with their names only. Pick none if the request is just conversation."),
            ChatMessage.user(f"Request: {text[:600]}\n\nTools:\n{listing}")], schema_for([s.name for s in pickable])), TIMEOUT)
    except Exception:
        return specs
    picked = [n for n in (data or {}).get("tools", []) if isinstance(n, str)] if isinstance(data, dict) else []
    names = {s.name for s in pickable}
    chosen = [n for n in dict.fromkeys(picked) if n in names][:MAX_PICK]
    if not chosen:
        return specs                                     # nothing usable picked: do not strip the model of its tools
    wanted = set(chosen) | set(keep)
    return [s for s in specs if s.name in wanted]
