"""First-run setup wizard: name, language and voice, microphone test, AI models, features.

Shown once (SETUP_DONE=false) and any time from the menu. Every choice is written to the
.env file, exactly as the Settings dialog would; nothing is enabled behind your back.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWizard, QWizardPage,
)

if TYPE_CHECKING:
    from app.ui.controller import AssistantController

RECOMMENDED_CHAT_MODEL = "qwen3:4b-instruct"
RECOMMENDED_EMBEDDING_MODEL = "nomic-embed-text"
RECOMMENDED_VISION_MODEL = "qwen2.5vl:3b"


class _Bridge(QObject):
    """Thread -> UI signals."""
    level = Signal(str)
    progress = Signal(str, int)
    pulled = Signal(str, str)  # (model, error or "")


def _label(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("muted")
    return label


class SetupWizard(QWizard):
    def __init__(self, controller: "AssistantController", parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.settings = controller.ctx.settings
        self.setWindowTitle("Set up Jarvis")
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)  # dark theme friendly (Modern draws a white banner)
        self.setMinimumSize(640, 520)
        self.bridge = _Bridge()
        self.bridge.level.connect(lambda text: self.mic_result.setText(text))
        self.bridge.progress.connect(self._on_progress)
        self.bridge.pulled.connect(self._on_pulled)
        for page in (self._welcome(), self._voice(), self._microphone(), self._models(), self._features()):
            self.addPage(page)
        self.saved = None

    # ------------------------------------------------------------------ pages

    def _welcome(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Welcome")
        page.setSubTitle("A few questions, and Jarvis is ready. Everything can be changed later in Settings.")
        form = QFormLayout(page)
        self.name_edit = QLineEdit(self.settings.assistant_name)
        self.address_edit = QLineEdit(self.settings.user_address)
        self.address_edit.setPlaceholderText("sir, or your name")
        self.city_edit = QLineEdit(self.settings.home_location)
        self.city_edit.setPlaceholderText("e.g. Dhaka")
        form.addRow("Assistant name", self.name_edit)
        form.addRow("Call me", self.address_edit)
        form.addRow("Home city (weather)", self.city_edit)
        return page

    def _voice(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Language and voice")
        page.setSubTitle("Speak Bangla, English or both: Jarvis answers in the language you use.")
        form = QFormLayout(page)
        self.language_combo = QComboBox()
        self.language_combo.addItems(["auto (Bangla + English)", "en (English only)", "bn (Bangla only)"])
        self.language_combo.setCurrentIndex({"auto": 0, "en": 1, "bn": 2}.get(self.settings.voice_language, 0))
        self.voice_combo = QComboBox()
        self.voice_combo.addItems(["JARVIS - calm British voice with an AI finish (online: reply text goes to "
                                   "Microsoft's speech service; offline: a voice on this PC)"])
        self.voice_combo.setCurrentIndex(0)
        test = QPushButton("▶  Test the voice")
        test.clicked.connect(lambda: self.controller.tts.speak("Hello! I'm ready. আমি প্রস্তুত।"))
        form.addRow("I speak", self.language_combo)
        form.addRow("Voice", self.voice_combo)
        form.addRow("", test)
        form.addRow(_label("A new voice choice takes effect after a restart; the test uses the current voice."))
        return page

    def _microphone(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Microphone")
        page.setSubTitle("Test that Jarvis hears you clearly.")
        box = QVBoxLayout(page)
        self.hands_free_check = QCheckBox("Always listening: say \"Jarvis\" any time (hands-free)")
        self.hands_free_check.setChecked(self.settings.hands_free)
        button = QPushButton("🎤  Test microphone (speak for 3 seconds)")
        button.clicked.connect(self._test_microphone)
        self.mic_result = _label("")
        box.addWidget(self.hands_free_check)
        box.addWidget(button)
        box.addWidget(self.mic_result)
        box.addStretch()
        return page

    def _models(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("AI models")
        page.setSubTitle("Local models run in Ollama on this PC.")
        box = QVBoxLayout(page)
        installed = self.controller.installed_models()
        box.addWidget(_label("Installed: " + (", ".join(installed) if installed else "none yet (is Ollama running?)")))
        self.model_rows: dict[str, tuple[QPushButton, QProgressBar]] = {}
        for model, why in ((RECOMMENDED_CHAT_MODEL, "chat and commands, good Bangla (about 2.5 GB)"),
                           (RECOMMENDED_EMBEDDING_MODEL, "search your documents and past conversations (274 MB)"),
                           (RECOMMENDED_VISION_MODEL, "optional: sees your screen for \"what's on my screen\" and "
                                                      "\"click the blue button\" (about 3.2 GB)")):
            row = QHBoxLayout()
            have = any(m.split(":")[0] == model.split(":")[0] and (":" not in model or m.startswith(model))
                       for m in installed)
            button = QPushButton("Installed ✓" if have else f"Download {model}")
            button.setEnabled(not have)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(100 if have else 0)
            button.clicked.connect(lambda _=False, m=model: self._pull(m))
            text = QLabel(f"<b>{model}</b><br>{why}")
            text.setWordWrap(True)
            row.addWidget(text, 1)
            row.addWidget(button)
            box.addLayout(row)
            box.addWidget(bar)
            self.model_rows[model] = (button, bar)
        self.use_recommended = QCheckBox(f"Use {RECOMMENDED_CHAT_MODEL} as the AI model")
        self.use_recommended.setChecked(self.settings.ai_model in ("", RECOMMENDED_CHAT_MODEL))
        box.addWidget(self.use_recommended)
        self.make_jarvis = QCheckBox("Make it the \"jarvis\" model (one name, no more guessing which model to use)")
        self.make_jarvis.setChecked(True)
        box.addWidget(self.make_jarvis)
        self.use_vision = QCheckBox(f"Use {RECOMMENDED_VISION_MODEL} for the screen (also allow screen capture: Settings -> Privacy)")
        self.use_vision.setChecked(self.settings.vision_model == RECOMMENDED_VISION_MODEL
                                   or any(m.startswith(RECOMMENDED_VISION_MODEL) for m in installed))
        box.addWidget(self.use_vision)
        box.addStretch()
        return page

    def _features(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Features")
        page.setSubTitle("Choose what Jarvis may do. Privacy-sensitive features are off unless you turn them on.")
        box = QVBoxLayout(page)
        s = self.settings
        self.memory_check = QCheckBox("Remember our conversations (stored on this PC, secrets redacted)")
        self.memory_check.setChecked(s.conversation_memory)
        self.speaker_check = QCheckBox("Only my voice can give commands (enroll your voice afterwards: Menu)")
        self.speaker_check.setChecked(s.speaker_verification)
        self.presence_check = QCheckBox("Use the webcam to notice when I leave (lock) and come back (greet)")
        self.presence_check.setChecked(s.presence_detection)
        self.autostart_check = QCheckBox("Start with Windows")
        self.autostart_check.setChecked(s.start_with_windows)
        self.orb_check = QCheckBox("Show the mini orb while the window is hidden")
        self.orb_check.setChecked(s.mini_orb)
        folders = QHBoxLayout()
        self.folders_edit = QLineEdit(s.document_folders)
        self.folders_edit.setPlaceholderText("Folders with your documents (for \"ask my documents\")")
        pick = QPushButton("Add folder...")
        pick.clicked.connect(self._pick_folder)
        folders.addWidget(self.folders_edit, 1)
        folders.addWidget(pick)
        for widget in (self.memory_check, self.speaker_check, self.presence_check, self.autostart_check, self.orb_check):
            box.addWidget(widget)
        box.addLayout(folders)
        box.addStretch()
        return page

    # ------------------------------------------------------------------ actions

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Documents folder")
        if folder:
            current = [p for p in self.folders_edit.text().split(";") if p.strip()]
            self.folders_edit.setText(";".join([*current, folder]))

    def _test_microphone(self) -> None:
        self.mic_result.setText("Listening... speak now.")
        microphone = self.controller.microphone

        def work() -> None:
            listener = self.controller.hands_free
            if listener is not None:
                self.controller.stop_hands_free()
            try:
                report, _ = microphone.level_test(3.0)
                self.bridge.level.emit(f"Peak {report.peak_db:.0f} dBFS, average {report.average_db:.0f} dBFS. "
                                       f"{report.verdict}")
            except Exception as exc:  # shown to the user
                self.bridge.level.emit(f"The microphone test failed: {exc}")
            finally:
                if listener is not None:
                    self.controller._restart_hands_free.emit()

        threading.Thread(target=work, name="mic-test", daemon=True).start()

    def _pull(self, model: str) -> None:
        button, bar = self.model_rows[model]
        button.setEnabled(False)
        button.setText("Downloading...")

        def progress(status: str, completed, total) -> None:
            if total:
                self.bridge.progress.emit(model, int(100 * (completed or 0) / total))

        async def run() -> None:
            await self.controller.ai.provider.pull(model, progress)

        future = self.controller.runner.submit(run())
        future.add_done_callback(lambda f: self.bridge.pulled.emit(
            model, "" if not f.exception() else str(f.exception())))

    def _on_progress(self, model: str, percent: int) -> None:
        self.model_rows[model][1].setValue(percent)

    def _on_pulled(self, model: str, error: str) -> None:
        button, bar = self.model_rows[model]
        if error:
            button.setEnabled(True)
            button.setText("Retry")
            bar.setFormat(error[:80])
        else:
            button.setText("Installed ✓")
            bar.setValue(100)
            self.controller.refresh_status()

    # ------------------------------------------------------------------ finish

    def updates(self) -> dict[str, str]:
        language = ("auto", "en", "bn")[self.language_combo.currentIndex()]
        flag = lambda box: "true" if box.isChecked() else "false"  # noqa: E731
        values = {
            "ASSISTANT_NAME": self.name_edit.text().strip() or "Jarvis",
            "USER_ADDRESS": self.address_edit.text().strip(),
            "HOME_LOCATION": self.city_edit.text().strip(),
            "VOICE_LANGUAGE": language,
            "TTS_ENGINE": "jarvis",
            "HANDS_FREE": flag(self.hands_free_check),
            "CONVERSATION_MEMORY": flag(self.memory_check),
            "SPEAKER_VERIFICATION": flag(self.speaker_check),
            "PRESENCE_DETECTION": flag(self.presence_check),
            "START_WITH_WINDOWS": flag(self.autostart_check),
            "MINI_ORB": flag(self.orb_check),
            "DOCUMENT_FOLDERS": self.folders_edit.text().strip(),
            "SETUP_DONE": "true",
        }
        if self.use_recommended.isChecked():
            values["AI_MODEL"] = RECOMMENDED_CHAT_MODEL
        if self.use_vision.isChecked():
            values["VISION_MODEL"] = RECOMMENDED_VISION_MODEL
        return values

    def accept(self) -> None:
        from app.config.env_writer import update_env_file
        from app.config.settings import ConfigError

        try:
            self.saved = update_env_file(self.controller.ctx.paths.env_file, self.updates())
        except (ConfigError, OSError) as exc:
            self.button(QWizard.WizardButton.FinishButton).setText("Fix and finish")
            self.setWindowTitle(f"Set up Jarvis - could not save: {exc}")
            return
        old = self.controller.ctx.settings
        changed = {f for f in type(old).model_fields if getattr(old, f) != getattr(self.saved, f)}
        self.controller.apply_settings(self.saved, changed)
        self.controller.ctx.settings = self.saved
        if self.use_recommended.isChecked() and self.make_jarvis.isChecked():
            installed = self.controller.installed_models()
            if any(m.startswith(RECOMMENDED_CHAT_MODEL) for m in installed):
                self.controller.make_jarvis_model(RECOMMENDED_CHAT_MODEL, [])
        super().accept()
