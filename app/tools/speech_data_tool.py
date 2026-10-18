"""``voice_data``: what of your speech is kept to train the recognizer (SPEECH_DATASET), export it for training,
or erase it ("how much of my voice do you have", "export my Bangla recordings", "forget my voice recordings")."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class VoiceDataArgs(ToolArgs):
    action: Literal["report", "export", "forget"] = Field(
        default="report", description="report = how much is kept; export = write the corrected ones for training; "
                                      "forget = erase all kept recordings")
    language: str = Field(default="bn", max_length=8, description="language code for export, e.g. bn or en")


def _dataset(ctx: ToolContext):
    if ctx.paths is None:
        raise ToolError("This needs Jarvis's data folder.", "unavailable")
    from app.voice.speech_dataset import SpeechDataset

    return SpeechDataset(ctx.paths.data_dir)


def voice_data(args: VoiceDataArgs, ctx: ToolContext) -> ToolResult:
    data = _dataset(ctx)
    mode = getattr(ctx.settings, "speech_dataset", "off") if ctx.settings is not None else "off"
    if args.action == "forget":
        removed = data.forget()
        return ToolResult.success(f"Erased {removed} kept voice recordings. Your corrections (the words) stay.")
    if args.action == "export":
        target = ctx.paths.data_dir / "exports" / f"speech_{args.language}"
        count = data.export(args.language, target)
        if not count:
            return ToolResult.success(f"Nothing to export for '{args.language}' yet: only corrected recordings are "
                                      "exported (say 'no, I said ...' when I mishear you, with SPEECH_DATASET on).")
        return ToolResult.success(f"Exported {count} corrected recordings to {target}. Train with "
                                  "scripts/finetune_whisper.py on a GPU, then set STT_BANGLA_MODEL to the result.",
                                  path=str(target), clips=count)
    summary = data.summary()
    if not summary:
        hint = "" if mode != "off" else " Keeping recordings is off (Settings > Privacy > SPEECH_DATASET)."
        return ToolResult.success("No voice recordings are kept." + hint, mode=mode)
    parts = [f"{lang}: {v['clips']} clips ({v['corrected']} corrected, {v['minutes']} min)" for lang, v in summary.items()]
    return ToolResult.success("Kept for training: " + "; ".join(parts) + f". Mode: {mode}.", summary=summary, mode=mode)


def _risk(args: VoiceDataArgs, _ctx: ToolContext) -> RiskLevel:
    return RiskLevel.DANGEROUS if args.action == "forget" else RiskLevel.SAFE


TOOLS = (
    Tool("voice_data", "The user's own voice recordings kept to train speech recognition (SPEECH_DATASET): report how "
         "much is kept, export the corrected ones for training, or erase them ('forget my voice recordings').",
         voice_data, VoiceDataArgs, RiskLevel.SAFE, "memory", risk=_risk,
         confirmation=lambda a, _: "Erase all kept voice recordings?"),
)
