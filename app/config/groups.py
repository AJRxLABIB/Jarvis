"""The settings, grouped by what they're for (a map, not a second copy of the settings).

``Settings`` stays one flat model (so .env keeps working unchanged), but every field belongs to exactly one
group here, and a test makes sure a new setting can't be added without choosing one. The kinds the
architecture separates:

* **user** settings - the groups below, written to .env
* **secrets** - ``SECRET_FIELDS`` in settings.py (stored DPAPI-encrypted as ``dpapi:...``)
* **runtime state** - the settings_kv table (e.g. the model you picked in the app), not .env
* **experimental** - the "experimental" group: off by default, may change between versions
"""

from __future__ import annotations

GROUPS: dict[str, tuple[str, ...]] = {
    "identity": ("assistant_name", "user_address", "user_name", "setup_done", "settings_version"),
    "ai": ("ai_provider", "ollama_host", "ai_model", "auto_start_ollama", "hide_ollama_window", "ai_request_timeout",
           "ai_temperature", "ai_context_window", "ai_fast_model", "ai_smart_model", "ai_coding_model", "ai_think", "ai_auto_smart", "ai_auto_switch", "taint_lockdown", "ollama_keep_alive",
           "context_max_messages", "context_max_chars", "idle_unload_minutes", "ollama_fast_memory",
           "gpu_memory_saver", "vision_model", "embedding_model"),
    "memory": ("conversation_memory", "memory_suggestions", "clipboard_history", "clipboard_retention_days", "document_folders", "world_context"),
    "wake_and_listening": ("wake_word", "hands_free", "wake_word_engine", "wake_word_threshold", "wake_on_name",
                           "wake_word_model", "follow_up_seconds", "wake_sound", "microphone_enabled",
                           "microphone_device", "personal_wake_check", "barge_in", "voice_interrupt"),
    "speech_recognition": ("voice_language", "voice_languages", "stt_model", "stt_device", "stt_compute_type",
                           "stt_beam_size", "custom_vocabulary", "stt_second_pass", "early_transcription",
                           "stt_bangla_model", "stt_min_confidence", "vad_engine", "silence_timeout",
                           "speech_start_timeout", "max_record_seconds", "tone_aware"),
    "speech_output": ("tts_enabled", "tts_engine", "tts_voice", "tts_neural_voice_en", "tts_neural_voice_bn",
                      "tts_kokoro_voice", "tts_sapi_voice", "tts_style", "tts_speed", "tts_volume", "output_device",
                      "speak_typed_replies", "show_sources", "learn_from_use"),
    "security": ("full_power", "autonomy", "autonomy_remote", "autonomy_ask_categories", "autonomy_block_categories", "confirm_power_actions", "save_before_power", "voice_confirm", "speaker_verification",
                 "speaker_threshold", "update_require_signature", "screen_blocked_windows", "direct_commands"),
    "privacy": ("speech_dataset", "offline_mode", "presence_detection", "presence_lock", "presence_away_minutes",
                "screen_capture_enabled", "context_window_titles"),
    "window": ("mini_orb_size", "mini_orb", "start_minimized", "start_with_windows", "intro_animation",
               "step_aside", "always_on_top", "start_fullscreen", "animations", "monitor_interval",
               "metrics_history", "auto_backup", "core_renderer", "visual_quality", "core_speed", "glow_intensity", "reduced_motion", "core_hud_rings",
               "core_pulses", "core_trails", "ui_sounds", "ui_sound_volume", "hud_scale", "hidden_panels",
               "memory_event_retention_days", "metrics_retention_days",
               "process_history"),
    "browser_and_web": ("browser_enabled", "browser_channel", "browser_headless", "browser_idle_minutes",
                        "search_provider", "web_browser", "chrome_profile", "edge_profile", "reuse_tabs",
                        "news_region", "music_default_query"),
    "vision": ("screenshot_dir", "ocr_engine", "ocr_language"),
    "proactive": ("proactive_alerts", "pc_watcher", "low_battery_percent", "proactive_suggestions", "proactive_level", "quiet_hours", "automation_quiet_hours", "briefing_length", "briefing_sections",
                  "proactive_max_per_hour", "proactive_categories", "proactive_emergency_exceptions", "focus_apps",
                  "auto_sort_downloads", "weekly_health_check"),
    "places_and_units": ("home_location", "temperature_unit"),
    "developer": ("dev_project_dir", "dev_command_timeout", "jarvis_source_dir"),
    "devices_and_remote": ("home_assistant_url", "home_assistant_token", "github_token", "google_client_id", "google_client_secret", "remote_enabled", "remote_port", "remote_allow_tailscale", "wake_public_address", "wake_public_port",
                           "remote_https", "fcm_service_account"),
    "extensions": ("plugins_enabled", "mcp_enabled", "update_manifest_url"),
    "diagnostics": ("debug", "log_level", "log_max_bytes", "log_backup_count", "data_dir"),
    "experimental": (),
}


def group_of(field: str) -> str | None:
    return next((group for group, fields in GROUPS.items() if field in fields), None)


def grouped(values: dict[str, object]) -> dict[str, dict[str, object]]:
    """{group: {field: value}} in the order above (for the diagnostics bundle and the settings window)."""
    out: dict[str, dict[str, object]] = {}
    for group, fields in GROUPS.items():
        present = {f: values[f] for f in fields if f in values}
        if present:
            out[group] = present
    rest = {k: v for k, v in values.items() if group_of(k) is None}
    if rest:
        out["ungrouped"] = rest
    return out
