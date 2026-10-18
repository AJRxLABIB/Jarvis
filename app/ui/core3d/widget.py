"""`GlOrbWidget`: the 3D core as a QOpenGLWidget. Same public API as the painter-based `OrbWidget`
(set_state / set_level / set_telemetry / state / animating), so the main window can use either.

GL work: moderngl draws the scene into the widget's framebuffer inside QPainter's native-painting block; afterwards the
flat HUD (telemetry arcs, tick bezel, gold ring) is painted over it. If moderngl is missing, the context cannot be
created, a shader does not compile or the context is lost, the widget keeps working with the painter-drawn core.
"""

from __future__ import annotations

import logging
import math
import os
import time

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from app.ui.core3d.dynamics import CoreDynamics, canonical
from app.ui.core3d.quality import Governor, Quality, resolve
from app.ui.visualizer import OrbWidget
from app.utils.logging_setup import log_event

_log = logging.getLogger(__name__)
MAX_DRAG = 0.9          # radians of temporary inspection yaw / pitch


def gl_available() -> tuple[bool, str]:
    """Can this process use the OpenGL core at all? (a cheap check; the real test is the context creation)"""
    if os.environ.get("QT_QPA_PLATFORM", "").lower() in ("offscreen", "minimal"):
        return False, "no display"
    try:
        import moderngl  # noqa: F401
    except Exception as exc:
        return False, f"moderngl is not installed ({exc})"
    return True, ""


class GlOrbWidget(QOpenGLWidget):
    failed = Signal(str)          # the GL core could not start; the painter core is used instead
    quality_changed = Signal(str)
    frame_rendered = Signal()     # after every frame: the window background paces its holographic layer on it
    gl_stable = Signal()          # once: the core has drawn steadily for a while (start-up crash guard, gl_guard.py)
    STABLE_FRAMES, STABLE_SECONDS = 120, 15.0

    def __init__(self, quality_setting: str = "auto", speed: float = 1.0, glow: float = 1.0, reduced_motion: bool = False,
                 parent=None, backdrop: bool = False) -> None:
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setAlphaBufferSize(8)
        fmt.setDepthBufferSize(0)
        self.setFormat(fmt)
        # backdrop mode: this widget IS the window background (hexagons, holographic waves, the core in `slot`), and the
        # dashboard's panels are its children, composited on top. Otherwise: a translucent widget inside a panel.
        self.backdrop_mode = backdrop
        self.slot = None                    # backdrop mode: the widget whose rectangle the core is drawn in
        self._backdrop = None
        self._bg_for = None                 # size the backdrop image was made for
        self._hex = None
        self._beam_t0 = time.monotonic()
        if not backdrop:
            # blend the transparent GL output over the panel behind it (without this Windows shows a black box)
            self.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop, True)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.compact = False
        self.setMinimumSize(300, 300)
        self.setMouseTracking(True)
        self.setAccessibleName("Jarvis core")
        self.model = OrbWidget()              # hidden: telemetry values, animated read-outs and the painter fallback
        self.dyn = CoreDynamics(speed_scale=speed, glow_scale=glow, reduced_motion=reduced_motion)
        self.governor = Governor()
        self.quality_setting = quality_setting
        self.quality: Quality = resolve(quality_setting, self.governor.level)
        self._ctx = None
        self._scene = None
        self.gl_ok = True                     # becomes False after a failure: painter core from then on
        self.gl_error = ""
        self._mic_level: float | None = None
        self._last = time.monotonic()
        self._yaw = self._pitch = 0.0         # smoothed camera offsets
        self._target = [0.0, 0.0]             # pointer parallax
        self._drag: QPointF | None = None
        self._drag_offset = [0.0, 0.0]
        self.frame_ms = 0.0                   # smoothed frame time, for the performance read-out
        # frame pacing: the next frame is requested when the last one reached the screen (frameSwapped), at most at the
        # quality's rate; a watchdog restarts the chain if a frame is ever lost (no more freezes)
        self._frame_started = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self.update)
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(250)
        self._watchdog.timeout.connect(self._kick)
        self.frameSwapped.connect(self._schedule_next)
        from app.ui.core3d.fx import Effects

        self.fx = Effects()                   # data pulses to the panels + spark trails (fx.py)
        self.speech_level = None              # callable -> 0..1 loudness of Jarvis's voice right now, or None
        self.frames = 0
        self._first_frame_at: float | None = None
        self._stable_sent = False
        self._apply_rate()

    # ------------------------------------------------------------------ the OrbWidget API
    @property
    def state(self) -> str:
        return self.model.state

    def set_state(self, state: str) -> None:
        self.model.set_state(state)
        self.dyn.set_state(canonical(state))
        self._apply_rate()
        self.update()

    def set_level(self, dbfs: float) -> None:
        self.model.set_level(dbfs)
        self._mic_level = max(0.0, min(1.0, (dbfs + 60.0) / 50.0))

    def set_telemetry(self, key: str, value: float | None, text: str | None = None) -> None:
        self.model.set_telemetry(key, value, text)

    @property
    def animating(self) -> bool:
        return self._watchdog.isActive()

    @property
    def state_label(self) -> str:
        return self.dyn.label

    # ------------------------------------------------------------------ settings
    def apply_settings(self, quality_setting: str, speed: float, glow: float, reduced_motion: bool) -> None:
        self.quality_setting = quality_setting
        self.dyn.speed_scale, self.dyn.glow_scale, self.dyn.reduced_motion = speed, glow, reduced_motion
        self._set_quality(resolve(quality_setting, self.governor.level))

    def _set_quality(self, quality: Quality) -> None:
        if quality.name == self.quality.name:
            return
        self.quality = quality
        if self._scene is not None and self.gl_ok:
            self.makeCurrent()
            try:
                self._scene.build(quality)
            except Exception as exc:
                self._fail(f"rebuilding the scene failed: {exc}")
            finally:
                self.doneCurrent()
        self._apply_rate()
        self.quality_changed.emit(quality.name)

    @property
    def budget(self) -> float:
        """Seconds per frame at this quality's target rate, rounded to whole display refreshes (even pacing)."""
        target = 1.0 / max(5, self.quality.fps)
        screen = self.screen()
        refresh = screen.refreshRate() if screen is not None else 60.0
        if refresh and refresh > 20:
            vsync = 1.0 / refresh
            target = vsync * max(1, round(target / vsync))
        return target

    def _apply_rate(self) -> None:
        if not self.isVisible():
            self._timer.stop()
            self._watchdog.stop()
            return
        if not self._watchdog.isActive():
            self._watchdog.start()
        self.update()

    def _schedule_next(self) -> None:
        if not self.isVisible():
            return
        wait = self.budget - (time.perf_counter() - self._frame_started)
        if wait <= 0.002:
            self.update()
        else:
            self._timer.start(max(1, int(wait * 1000)))

    def _kick(self) -> None:
        """Watchdog: a frame that never reached the screen must not stop the animation."""
        if self.isVisible() and time.perf_counter() - self._frame_started > max(0.2, 3 * self.budget):
            self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._last = time.monotonic()
        self._apply_rate()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()           # nothing is drawn (or computed) while hidden or minimised
        self._watchdog.stop()

    # ------------------------------------------------------------------ OpenGL lifecycle
    def initializeGL(self) -> None:  # noqa: N802
        try:
            import moderngl

            from app.ui.core3d.scene import CoreScene

            self._ctx = moderngl.create_context()
            self._scene = CoreScene(self._ctx, self.quality)
            if self.backdrop_mode:
                from app.ui.core3d.backdrop import Backdrop

                self._backdrop = Backdrop(self._ctx)
                self._bg_for = None
            self.context().aboutToBeDestroyed.connect(self._release_gl)
            self.gl_ok = True
            info = getattr(self._ctx, "info", {}) or {}
            log_event(_log, "gl_initialized", vendor=str(info.get("GL_VENDOR", "?")),
                      renderer=str(info.get("GL_RENDERER", "?")), version=str(info.get("GL_VERSION", "?")),
                      quality=self.quality.name)
        except Exception as exc:
            self._fail(f"OpenGL core could not start: {exc}")

    def _release_gl(self) -> None:
        """The context is going away (window closed or driver reset): free everything, rebuild on the next init."""
        if self._scene is not None:
            try:
                self.makeCurrent()
                self._scene.release()
                if self._backdrop is not None:
                    self._backdrop.release()
            except Exception:
                pass
            finally:
                self._scene = None
                self._backdrop = None
                self._ctx = None

    def _reset_gl_state(self) -> None:
        """Hand the context back to Qt the way its painter expects it: no VAO / buffers / program bound, texture unit 0,
        Qt's own framebuffer. (A leftover bound buffer makes the painter's later draws read from it - a driver crash.)"""
        try:
            ctx = self.context()
            f, x = ctx.functions(), ctx.extraFunctions()
            x.glBindVertexArray(0)
            f.glBindBuffer(0x8892, 0)        # GL_ARRAY_BUFFER
            f.glBindBuffer(0x8893, 0)        # GL_ELEMENT_ARRAY_BUFFER
            f.glUseProgram(0)
            f.glActiveTexture(0x84C0)        # GL_TEXTURE0
            f.glBindTexture(0x0DE1, 0)       # GL_TEXTURE_2D
            f.glBindFramebuffer(0x8D40, self.defaultFramebufferObject())
        except Exception:
            pass

    def _fail(self, reason: str) -> None:
        if self.gl_ok:
            _log.warning("3D core disabled, using the painter-drawn core: %s", reason)
        self.gl_ok, self.gl_error = False, reason
        self._scene = None
        self.failed.emit(reason)

    # ------------------------------------------------------------------ camera: idle sway, parallax, drag-to-inspect
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is not None:
            d = event.position() - self._drag
            self._drag = event.position()
            self._drag_offset[0] = max(-MAX_DRAG, min(MAX_DRAG, self._drag_offset[0] + d.x() * 0.008))
            self._drag_offset[1] = max(-MAX_DRAG, min(MAX_DRAG, self._drag_offset[1] + d.y() * 0.008))
        elif self.width() and self.height():
            self._target = [(event.position().x() / self.width() - 0.5) * 0.18,
                            (event.position().y() / self.height() - 0.5) * 0.12]
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag = None             # the view glides back to the default (see _camera)
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._target = [0.0, 0.0]
        super().leaveEvent(event)

    def _camera(self, dt: float) -> tuple[float, float]:
        k = 1.0 - math.exp(-dt * 6.0)
        if self._drag is None:                                  # return smoothly from an inspection
            self._drag_offset = [v * math.exp(-dt * 2.2) for v in self._drag_offset]
        sway = 0.0 if self.dyn.reduced_motion else 1.0
        wanted_yaw = 0.16 * math.sin(self.dyn.clock * 0.13) * sway + self._target[0] + self._drag_offset[0]
        wanted_pitch = 0.07 * math.sin(self.dyn.clock * 0.09 + 1.0) * sway + self._target[1] + self._drag_offset[1]
        self._yaw += (wanted_yaw - self._yaw) * k
        self._pitch += (wanted_pitch - self._pitch) * k
        return self._yaw, self._pitch

    # ------------------------------------------------------------------ drawing
    def paintGL(self) -> None:  # noqa: N802
        started = time.perf_counter()
        self._frame_started = started
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        # a late frame continues the motion where it was (at most 1/20 s further) instead of jumping ahead:
        # the animation never visibly skips, even if Windows or another program held the window up for a moment
        dt = min(dt, 0.05)
        self.model.anim_clock = (self.model.anim_clock or 0.0) + dt
        self.dyn.step(dt, self._audio_level())
        self.model._frame()          # the read-outs' animated values and the fallback's wave history
        self.frame_ms = self.frame_ms * 0.9 + dt * 1000.0 * 0.1 if self.frame_ms else dt * 1000.0
        if self.backdrop_mode:
            self._paint_backdrop_frame(dt)
        else:
            self._paint_widget_frame(dt)
        cost = time.perf_counter() - started
        self.frames += 1
        if self._first_frame_at is None:
            self._first_frame_at = now
            log_event(_log, "gl_first_frame", gl=self.gl_ok)
        elif (not self._stable_sent and self.frames >= self.STABLE_FRAMES
              and now - self._first_frame_at >= self.STABLE_SECONDS):
            self._stable_sent = True
            self.gl_stable.emit()
        if self.quality_setting == "auto" and self.isVisible() and self.gl_ok:
            changed = self.governor.frame(cost, self.budget, dt)
            if changed:
                self._set_quality(resolve("auto", changed))
        self.frame_rendered.emit()

    def _audio_level(self) -> float | None:
        """Listening: the microphone. Speaking: the REAL loudness of the voice being played (falls back to the
        synthetic cadence in dynamics.py only when the player can't report levels)."""
        if self.dyn.state == "LISTENING":
            return self._mic_level
        if self.dyn.state == "SPEAKING" and self.speech_level is not None:
            try:
                return self.speech_level()
            except Exception:
                return None
        return None

    def pulse_to(self, target, color=(0.3, 0.9, 1.0)) -> None:
        """A data pulse from the core to ``target`` (a panel widget)."""
        self.fx.pulse(target, time.monotonic(), color)

    def _rect_of(self, target):
        from PySide6.QtCore import QPoint

        if target is None or not target.isVisible() or target.width() < 4:
            return None
        top_left = self.mapFromGlobal(target.mapToGlobal(QPoint(0, 0)))
        return (top_left.x(), top_left.y(), target.width(), target.height())

    def _paint_widget_frame(self, dt: float) -> None:
        w, h = self.width(), self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.gl_ok and self._scene is not None:
            painter.beginNativePainting()
            try:
                ratio = self.devicePixelRatioF()
                size = (max(2, int(w * ratio)), max(2, int(h * ratio)))
                target = self._ctx.detect_framebuffer(self.defaultFramebufferObject())
                yaw, pitch = self._camera(dt)
                self._scene.render(target, size, self.dyn, yaw, pitch)
            except Exception as exc:
                self._fail(f"drawing failed: {exc}")
            finally:
                self._reset_gl_state()
                painter.endNativePainting()
        if self.gl_ok and self._scene is not None:
            tint = QColor.fromRgbF(*self.dyn.tint)
            self.model.paint_overlay(painter, w, h, tint, waves=not self.model.external_holo)
        else:
            self.model.paint_full(painter, w, h)         # painter-drawn fallback
        painter.end()

    def _slot_rect(self):
        slot = self.slot
        if slot is None or not slot.isVisibleTo(self) or slot.width() < 50 or slot.height() < 50:
            return None
        from PySide6.QtCore import QPoint, QRect

        return QRect(slot.mapTo(self, QPoint(0, 0)), slot.size())

    def _background_image(self):
        """The hexagon backdrop at this size (made once per size by the painter backdrop's own code)."""
        from app.ui.hud import HexBackground

        if self._hex is None:
            self._hex = HexBackground()
        self._hex.resize(self.size())
        return self._hex._render()

    def _paint_backdrop_frame(self, dt: float) -> None:
        w, h = self.width(), self.height()
        rect = self._slot_rect()
        if rect is not None:
            self.model.resize(rect.width(), rect.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.gl_ok and self._scene is not None and self._backdrop is not None:
            painter.beginNativePainting()
            try:
                import numpy as np

                from app.ui.core3d.backdrop import holo_geometry

                ratio = self.devicePixelRatioF()
                full = (max(2, int(w * ratio)), max(2, int(h * ratio)))
                if self._bg_for != (w, h):
                    from PySide6.QtGui import QImage

                    image = self._background_image().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
                    self._backdrop.set_image(bytes(image.constBits()), (image.width(), image.height()))
                    self._bg_for = (w, h)
                target = self._ctx.detect_framebuffer(self.defaultFramebufferObject())
                target.use()
                target.viewport = (0, 0, *full)
                beam = ((time.monotonic() - self._beam_t0) % 9.0) / 9.0 * 1.2 - 0.1
                self._backdrop.draw_background(beam, self.dyn.tint)
                if rect is not None:
                    cx, cy = (rect.x() + rect.width() / 2) * ratio, (rect.y() + rect.height() / 2) * ratio
                    radius = min(rect.width(), rect.height()) * 0.41 * ratio
                    t = self.model.anim_time()
                    geometry = holo_geometry(full[0], full[1], cx, cy, radius, self.dyn.tint, list(self.model._wave), t)
                    self._backdrop.draw_holo(full, np.ascontiguousarray(geometry))
                    size = (max(2, int(rect.width() * ratio)), max(2, int(rect.height() * ratio)))
                    viewport = (int(rect.x() * ratio), int(full[1] - (rect.y() + rect.height()) * ratio), *size)
                    yaw, pitch = self._camera(dt)
                    self._scene.render(target, size, self.dyn, yaw, pitch, viewport=viewport, over=True)
                    # the streams continue inside the globe, right into its heart (drawn over the core, masked to it)
                    from app.ui.core3d.scene import CAMERA_DISTANCE, FOV_DEGREES

                    import math as _m

                    limb = 1.0 / (_m.sqrt(CAMERA_DISTANCE ** 2 - 1.0) * _m.tan(_m.radians(FOV_DEGREES) / 2))
                    target.use()
                    target.viewport = (0, 0, *full)
                    self._backdrop.draw_holo_inside(full, (cx, cy), limb * min(size) / 2)
            except Exception as exc:
                self._fail(f"drawing failed: {exc}")
            finally:
                self._reset_gl_state()
                painter.endNativePainting()
        if not (self.gl_ok and self._scene is not None):
            painter.drawPixmap(0, 0, self._background_image())         # painter fallback for the whole backdrop
        if rect is not None:
            painter.save()
            painter.translate(rect.topLeft())
            if self.gl_ok and self._scene is not None:
                self.model.paint_overlay(painter, rect.width(), rect.height(), QColor.fromRgbF(*self.dyn.tint))
            else:
                self.model.paint_full(painter, rect.width(), rect.height())
            painter.restore()
            center = (rect.x() + rect.width() / 2, rect.y() + rect.height() / 2)
            self.fx.paint(painter, center, min(rect.width(), rect.height()) * 0.41, time.monotonic(), self.dyn.clock,
                          self._rect_of, self.dyn.reduced_motion)
        painter.end()

    # the painter backdrop's API, so the window can treat this widget as its background
    def start_motion(self) -> None:
        pass

    def resizeGL(self, w: int, h: int) -> None:  # noqa: N802
        if not self.backdrop_mode:
            self.model.resize(max(1, w), max(1, h))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        self._watchdog.stop()
        self._release_gl()
        super().closeEvent(event)


def create_core(settings, backdrop: bool = False) -> OrbWidget | GlOrbWidget:
    """The core widget for the main window: the OpenGL core when allowed and possible, else the painter-drawn one."""
    renderer = getattr(settings, "core_renderer", "auto")
    ok, reason = gl_available()
    if renderer == "painter" or not ok:
        if renderer == "gl" and not ok:
            _log.warning("3D core requested but unavailable: %s", reason)
        return OrbWidget()
    return GlOrbWidget(getattr(settings, "visual_quality", "auto"), float(getattr(settings, "core_speed", 1.0)),
                       float(getattr(settings, "glow_intensity", 1.0)), bool(getattr(settings, "reduced_motion", False)),
                       backdrop=backdrop)

