# JARVIS 3.14 -> 4.0: audit and plan

Written after inspecting the repository (3.14.0, 160 tools, 2,422 tests on Windows / 2,412 on Linux CI).
Rule followed: reuse what works, add only what's missing, never weaken the safety chain.

## 1. What exists (mapped to the 4.0 spec)

| Spec area | Status in 3.14 | Where |
|---|---|---|
| Safety chain (validate -> permission -> confirm -> timeout -> run -> verify -> audit) | **done** | `app/agent/tool_manager.py`, `app/security/*` |
| Permission levels SAFE/MODERATE/DANGEROUS/CRITICAL | **done** | `app/security/permissions.py`, `config/permissions.json` |
| Untrusted content (web, mail, OCR) can't authorise actions | **done** (taint) | `app/security/taint.py` |
| Emergency stop + switches | **done** | `app/core/emergency.py`, Ctrl+Shift+F12 |
| Context engine (structured snapshot) | **partial**: world model + context compiler + activity + active window | `app/core/world.py`, `app/agent/activity.py`, `this_window` |
| Memory 2.0 (provenance, privacy, expiry, revisions, explain) | **mostly done**; missing: episodic/procedural *types*, confidence, contradictions | `app/memory/manager.py` |
| Knowledge graph | **partial**: entities + relations in world model | `app/core/world.py` |
| Proactive intelligence + modes | **done** (rules, notification center, modes) | `app/core/notifications.py`, `modes.py` |
| Missions (plan, dry run, pause/resume, retry) | **done**; missing milestones/dependencies UI | `app/core/missions.py`, Mission Control |
| Multi-agent | **missing** as named agents (tools are grouped by category) | - |
| Model router | **partial**: fast/smart/vision; no coding model route | `app/ai/manager.py` |
| Computer agent (UIA controls, OCR, verify) | **done** (ui_elements, screen_agent, verify) | `app/tools/ui_elements.py` |
| Developer agent (backup, edit, test, git) | **done** | `app/tools/code_tools.py`, `developer.py` |
| Research agent (cited, untrusted) | **done** 3.12 | `app/tools/research.py` |
| Temporal ("2 hours after the build finishes") | **partial**: time/date parsing, reminders; no event-relative triggers | `app/automation/*` |
| Device registry + capabilities | **done** (PCs); phones are separate (web pairing) | `app/core/devices.py`, `app/remote/pairing.py` |
| Wake-on-LAN with honest verification | **done** | `app/core/devices.py`, `wake_device` |
| Phone remote (web page, per-phone key, approvals, voice, status) | **done** (bearer key per phone) | `app/remote/server.py` |
| Android app | **partial**: Jarvis Wake (WoL + check), not built/verified yet | `android/JarvisWake` |
| Cryptographic device identity, signed commands, replay protection | **missing** | - |
| Biometric-bound keys | **missing** | - |
| Device trust states / revoke / security alerts | **partial** (web phones can be removed) | - |
| Remote screen / input from phone | **missing** | - |
| Windows lock state + supported auth detection | **missing** | - |
| Self-evolution, plugin trust | **done** 3.11 / 3.13 | `app/evolution`, `app/plugins.py` |
| Diagnostics / self-test / health | **done** (self-test, health report, doctor) | `app/selftest.py`, `app/health.py` |
| Installer / update check / migrations with backup | **done** | `packaging/*`, `app/memory/database.py` |

## 2. What can't be built as asked, and the supported alternative

* **Unlocking Windows from the phone.** Windows offers no supported API for a third-party app to sign a user in
  from a phone. The old "companion device framework" is deprecated; Windows Hello for Business trusted signals
  need Entra/AD. A custom *credential provider* is Microsoft's supported extension point, but it is a signed
  native COM DLL that handles credentials inside LogonUI - a large, high-risk component that the spec itself
  (rule 25) asks us not to build. So 4.0 will: detect the lock state honestly; detect Windows Hello, TPM,
  edition and Remote Desktop availability; offer **Remote Desktop** (Microsoft's Windows App) as the supported
  way in; and say "Windows requires local/supported authentication" otherwise. **Dynamic Lock** (the phone
  *locks* the PC when you walk away) is a supported Windows feature and will be surfaced.
* **Wake from full shutdown / over Wi-Fi** depends on hardware; detected, never assumed (already the case).
* **Hardware attestation of the phone key**: Android key attestation certificates can be sent, but verifying the
  Google root chain offline is out of scope for now; the key's "biometric-bound / hardware" flags are recorded
  as *reported by the app*, and labelled so.
* **Full file contents pasted into chat** (spec 113.3): your saved preference is that changes go straight to the
  project files on disk. Every file is written there, tested, and listed; the diff is reviewable in VS Code.

## 3. Security risks found

1. The phone remote authenticates with a bearer key (header). Anyone who copies the key from the phone can act
   as that phone. -> 3.15 adds signed requests with a private key that never leaves the phone.
2. Approvals from the phone are bound to the request ID but not signed. -> signed approvals in 3.15.
3. No replay protection on phone requests (HTTPS helps, but not against a copied request). -> nonce + timestamp.
4. Pairing is a 6-digit code (good) but has no PC-side "trust this device?" step. -> explicit trust in 3.15.
5. Self-signed TLS: the phone must pin the PC's certificate fingerprint -> the pairing QR carries it (3.15/3.16).

## 4. Release plan (each one stable, tested, installable)

| Version | Content |
|---|---|
| **3.15** | Secure device protocol on the PC: device key registry (P-256 public keys, fingerprints, trust states, permissions, revocation), short-lived pairing sessions with PC-side trust, signed command envelopes, replay protection, capability permissions, every command through the safety kernel, audit, security alerts. Desktop "Mobile devices" screen. |
| 3.16 | JARVIS Mobile (Android): evolve Jarvis Wake -> keystore key (biometric-bound), pairing from QR/link, certificate pinning, home/status/wake/emergency, signed approvals |
| 3.17 | Mobile chat + voice (reuses PC speech recognition), notifications / events, approvals UI |
| 3.18 | Power & control commands by capability (lock, sleep, restart, shutdown, volume, media, apps) |
| 3.19 | Windows state: lock detection, Hello/TPM/edition/RDP detection, Remote Desktop hand-off, Dynamic Lock |
| 3.20 | Boot status timeline (offline -> waking -> booting -> Jarvis starting -> locked -> ready) from real signals |
| 3.21 | Remote screen (view-only, visible indicator, timeout) |
| 3.22 | Remote input (explicit permission, "Remote control active" banner, stop button) |
| 3.23 | Remote files (approved folders only) |
| 3.24 | Context snapshot API + "continue what we were doing" |
| 3.25 | Memory 2.0 types (episodic/procedural), confidence, contradiction detection |
| 3.26 | Knowledge graph queries ("which project contains...") |
| 3.27 | Named agents (planner, developer, device, security...) over the same executor |
| 3.28 | Model router 2.0 (coding model), resource governor |
| 3.29 | Event-relative automations, background task center |
| 3.30 | Diagnostics 2.0 / self-recovery, installer options, docs set |
| **4.0** | Full regression, adversarial tests, release |

## 5. Revised order (second master prompt: context first, Android later)

| Version | Content | State |
|---|---|---|
| 3.15 | Secure phone identity (PC side) | done - 2439 tests |
| 3.16 | Context Engine: snapshot with sources/certainty, prompt line, "run the tests" continuity, context panel | **done - 2466 tests** |
| 3.17 | Memory 2.0: types, confidence, importance, links, contradiction detection, supersession, stated vs recorded | **done** |
| 3.18 | Knowledge graph queries over projects, files, goals, decisions, requests | **done** |
| 3.19 | Model router 2.0 (coding / sensitive=local-only / fallback) | **done** |
| 3.20 | Proactive priority engine (levels, quiet hours, limits, explanations) | **done** |
| 3.21 | Goals/missions 2.0 (milestones, dependencies, checkpoints, rollback) + background task center; JARVIS voice profile | **done** |
| 3.22 | JARVIS Mobile 1.0 (secure remote terminal: control key, Windows state, sessions, files, screen) | **done** |
| 3.23 | Agent orchestrator + agent contracts over the same executor | **done** |
| 3.24 | Computer agent before/after verification + developer pipeline (test-gated edits) | **done** |
| 3.25 | Explainability ("why did you do that?") + diagnostics/recovery | **done** |
| 3.26+ | Research depth, device mesh, approval system 2.0, security hardening, command center UI | |
| 3.16.x | JARVIS Mobile app (Android side of 3.15) resumes after you've verified the Wake app build | paused |
