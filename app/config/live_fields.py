"""Settings that take effect while Jarvis runs without a restart (beyond the ones applied one by one)."""

from __future__ import annotations

REMOTE_FIELDS = frozenset({"remote_enabled", "remote_port", "remote_https", "remote_allow_tailscale"})
# read only at start-up (window state, intro, autostart flags): saving them is enough, they apply next time Jarvis starts
STARTUP_ONLY_FIELDS = frozenset({"start_minimized", "start_fullscreen", "intro_animation", "setup_done",
                                 "settings_version", "auto_start_ollama", "hide_ollama_window", "user_name", "hud_scale"})
# read by the action executor on every call: saving them applies at once
POLICY_FIELDS = frozenset({"full_power", "show_sources", "learn_from_use", "autonomy", "autonomy_remote", "autonomy_ask_categories", "autonomy_block_categories",
                           "taint_lockdown"})
# the 3D core / dashboard look: applied by the window at once (controller.apply_settings -> display_settings_changed)
DISPLAY_FIELDS = frozenset({"visual_quality", "core_speed", "glow_intensity", "reduced_motion", "core_hud_rings",
                            "core_pulses", "core_trails", "hidden_panels", "ui_sounds", "ui_sound_volume"})
