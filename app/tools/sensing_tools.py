"""``camera_session`` (open / close the explicit photo session), ``analyze_photo`` (a photo of the real world: text read + the vision model's interpretation, labelled apart;
or two photos compared) and ``sound_monitor`` (opt-in listening for configured sounds). Local only: no image or audio is uploaded or kept."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.core import events, object_detect as OD, physical_vision as PV, sound_events as SE
from app.security.permissions import RiskLevel
from app.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

CAMERA = PV.Camera()


class CameraArgs(ToolArgs):
    action: Literal["start", "stop", "status"] = Field(default="status", description="start opens a 5-minute photo session, stop closes it")


def camera_session(args: CameraArgs, ctx: ToolContext) -> ToolResult:
    if args.action == "start":
        CAMERA.start()
        return ToolResult.success("Photo session open for 5 minutes. Photos you send are analysed on this PC only and are not kept unless you ask. Say 'stop camera' to close it.")
    if args.action == "stop":
        CAMERA.stop()
        return ToolResult.success("Photo session closed.")
    s = CAMERA.session
    return ToolResult.success("A photo session is open." if s and s.usable(CAMERA.clock()) else "No photo session is open.")


class PhotoArgs(ToolArgs):
    question: str = Field(default="", max_length=500, description="What to find out ('what is this device', 'what does this light mean'); empty = describe the photo")
    path: str = Field(default="", max_length=400, description="An image file; empty = the newest photo the phone sent (needs an open photo session)")
    compare_with: str = Field(default="", max_length=400, description="A second image file to compare the first with")


def _image_path(args: PhotoArgs, ctx: ToolContext) -> Path:
    if args.path.strip():
        return ctx.get_path_policy().resolve(args.path, must_exist=True)
    if ctx.paths is None:
        raise ToolError("No data folder is available.", "no_data")
    inbox = ctx.paths.data_dir / "phone_inbox"
    found = [p for p in inbox.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")] if inbox.is_dir() else []
    # a photo you took on purpose with JARVIS Mobile ("Take a photo for JARVIS", named camera-*) is its own consent for ten minutes;
    # any other file the phone sent needs an open photo session first
    fresh = [p for p in found if p.name.startswith("camera-") and time.time() - p.stat().st_mtime <= PV.TAKEN_PHOTO_S]
    if fresh:
        return max(fresh, key=lambda p: p.stat().st_mtime)
    CAMERA.require()                                                    # no path = "whatever the phone sent": only while a session is open
    if not found:
        raise ToolError("The phone has not sent a photo yet (share one to JARVIS Mobile, or use its 'Take a photo for JARVIS').", "no_photo")
    return max(found, key=lambda p: p.stat().st_mtime)


def _read(path: Path) -> bytes:
    if path.stat().st_size > PV.MAX_BYTES:
        raise PV.VisionError(f"{path.name} is larger than {PV.MAX_BYTES // (1024 * 1024)} MB.")
    return path.read_bytes()


async def _detector_note(image: bytes, ctx: ToolContext) -> str:
    """The optional object detector's evidence (core/object_detect.py), labelled apart from the language model's description. Empty when no detector model is installed."""
    if ctx.paths is None:
        return ""
    try:
        det = OD.Detector.from_folder(ctx.paths.data_dir)
        if det is None:
            return ""
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image)) as im:
            size = im.size
        found = await asyncio.to_thread(det.detect, image)
        from app.core import scene_graph as SG

        return "\n\n" + OD.describe(found, size, det.name) + ("\n\n" + SG.build(found, size).text() if len(found) > 1 else "")
    except OD.DetectorError as exc:
        return f"\n\nObject detector skipped: {exc}."
    except Exception as exc:
        return f"\n\nObject detector skipped ({exc.__class__.__name__})."


async def analyze_photo(args: PhotoArgs, ctx: ToolContext) -> ToolResult:
    if ctx.ai is None:
        raise ToolError("The AI service is not available here.", "no_ai")
    try:
        first = await asyncio.to_thread(_read, _image_path(args, ctx))
        model = await ctx.ai.vision_model()
        if model is None:
            raise PV.VisionError("No installed model can read images. Install a vision model in Ollama (for example one with 'vision' in its capabilities) and try again.")

        async def vlm(image: bytes, system: str, prompt: str) -> tuple[str, str]:
            response = await ctx.ai.vision_chat(model, system, prompt, image)
            return response.message.content, model.name

        if args.compare_with.strip():
            second = await asyncio.to_thread(_read, ctx.get_path_policy().resolve(args.compare_with, must_exist=True))
            text = await PV.compare(first, second, vlm)
        else:
            async def ocr(image: bytes) -> tuple[str, str]:
                import io

                from PIL import Image

                from app.vision.ocr import create_ocr

                engine = create_ocr(ctx.settings)
                with Image.open(io.BytesIO(image)) as im:
                    res = await engine.recognize(im.convert("RGB"))
                return res.text, res.engine

            result = await PV.analyze(first, args.question, vlm, ocr)
            text = result.format() + await _detector_note(first, ctx)
        try:
            events.BUS.publish(events.VISION_ANALYSIS_COMPLETED, compared=bool(args.compare_with.strip()))
        except Exception:
            pass
        return ToolResult.success(text)
    except PV.VisionError as exc:
        raise ToolError(str(exc), "vision_refused") from exc


# ------------------------------------------------------------------ sound monitoring

_RUN: dict = {"monitor": SE.Monitor(), "stop": None, "thread": None, "close": None}


def _classifier(ctx: ToolContext, labels: list[str]):
    base = (ctx.paths.data_dir / "models" / "yamnet") if ctx.paths is not None else None
    if base is not None and (base / "yamnet.onnx").is_file() and (base / "yamnet_class_map.csv").is_file():
        try:
            return SE.YamnetOnnxClassifier.from_files(str(base / "yamnet.onnx"), str(base / "yamnet_class_map.csv"), set(labels)), "YAMNet (ONNX)"
        except Exception as exc:
            raise ToolError(f"The sound model could not be loaded ({exc.__class__.__name__}).", "model_unavailable") from exc
    if labels != ["loud_sound"]:
        raise ToolError("Recognising specific sounds (doorbell, knock, glass...) needs a sound-classification model, which is not installed: put yamnet.onnx and "
                        "yamnet_class_map.csv in data/models/yamnet (nothing is downloaded automatically). Without it only 'loud_sound' (anything much louder than the room) is available.",
                        "model_missing")
    return SE.LoudSoundClassifier(), "loudness only (no model)"


class SoundArgs(ToolArgs):
    action: Literal["start", "stop", "status"] = Field(default="status", description="start = begin listening, stop = stop at once")
    sounds: str = Field(default="loud_sound", max_length=200, description="start: comma-separated sounds to listen for (default 'loud_sound'; named sounds need the YAMNet model)")
    minutes: int = Field(default=30, ge=1, le=240, description="start: stop by itself after this many minutes")
    min_score: float = Field(default=0.6, ge=0.05, le=1.0, description="start: how confident a detection must be (0-1)")


def sound_monitor(args: SoundArgs, ctx: ToolContext) -> ToolResult:
    mon: SE.Monitor = _RUN["monitor"]
    if args.action == "status":
        return ToolResult.success(SE.describe(mon))
    if args.action == "stop":
        if _RUN["stop"] is not None:
            _RUN["stop"].set()
        mon.stop()
        if _RUN["close"]:
            _RUN["close"]()
            _RUN["close"] = None
        return ToolResult.success("Sound monitoring is off. Nothing was recorded.")
    if mon.session is not None and mon.session.state == "listening":
        return ToolResult.success("Already listening. " + SE.describe(mon))
    labels = [x.strip().lower().replace(" ", "_") for x in args.sounds.split(",") if x.strip()][:6]
    classifier, how = _classifier(ctx, labels)
    try:
        read, close = SE.sounddevice_reader()
        mon.start([SE.SoundRule(label, args.min_score) for label in labels], classifier, args.minutes)
    except (ValueError, RuntimeError) as exc:
        raise ToolError(str(exc), "sound_unavailable") from exc
    except Exception as exc:
        raise ToolError(f"The microphone could not be opened ({exc.__class__.__name__}).", "sound_unavailable") from exc
    stop = threading.Event()

    def notify(d: SE.Detection) -> None:
        try:
            events.BUS.publish(events.SOUND_EVENT_DETECTED, label=d.label, score=d.score)
        except Exception:
            pass
        if ctx.notify:
            ctx.notify("Sound detected", f"{d.label.replace('_', ' ')} (confidence {d.score:.2f})")

    def loop() -> None:
        try:
            SE.capture_loop(mon, read, notify, stop)
        finally:
            close()

    _RUN.update(stop=stop, close=None, thread=threading.Thread(target=loop, name="sound-monitor", daemon=True))
    _RUN["thread"].start()
    return ToolResult.success(f"Listening for {', '.join(labels)} for up to {args.minutes} min using {how}. Audio is scored and thrown away, never saved or uploaded. "
                              "This is not an emergency system: it can miss or mislabel sounds. Say 'stop listening' to end it.")


TOOLS: tuple[Tool, ...] = (
    Tool("camera_session", "Open, close or check the explicit photo session used to analyse photos from the phone ('start camera', 'stop camera').", camera_session, CameraArgs, RiskLevel.MODERATE, "vision", timeout=10),
    Tool("analyze_photo", "Analyse a photo of the real world (an object, a device, a document, a screen of lights): the text read from it plus the vision model's description, clearly labelled as "
         "a guess; or compare two photos ('what is this device', 'what does this router light mean', 'compare these two photos').", analyze_photo, PhotoArgs, RiskLevel.SAFE, "vision", timeout=180),
    Tool("sound_monitor", "Start, stop or check listening for configured sounds ('tell me when someone knocks', 'stop listening'); local, nothing recorded, not an emergency system.",
         sound_monitor, SoundArgs, RiskLevel.MODERATE, "vision", timeout=15),
)
