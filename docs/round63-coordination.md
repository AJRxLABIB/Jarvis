# Round 63: transactions, event intelligence, attention, what-changed, dependency-aware health

All of it reuses what exists (event bus, notification classifier, device mesh, snapshots, perf plan, diagnostics, settings store). Unit-tested with made-up devices / events / folders; **none of it has been run on your real PC and phone.**

## Cross-device transactions (`core/transactions.py`, tool `transaction`)
A ledger for ONE task spanning devices, not a second executor. Steps have an owner device, may wait for earlier steps, and are done by the normal tools / phone commands.
* Start only when its dependencies are verified and its device is reachable (device list = `device_mesh`).
* VERIFIED needs evidence reported by the owning device. PC step: the evidence you give. Phone step: a `phone_commands` row of that device with status `done` (`phone_command` id) - the PC's word is never enough.
* COMPLETE only when every step is verified (skipped steps are listed, and a skipped transaction is not "complete" unless something was verified).
* A device dropping turns its running steps into UNCERTAIN (not done, not replayed). `options` offers: check, wait, move to another capable device, retry (max 2 attempts), skip, stop. Nothing is chosen for you.
* A checkpoint of verified steps is stored after every change (settings store, 30 kept), so a restart resumes from the last verified step.
Limits: it does not send phone commands itself, and a step that a tool "reports" is only as good as the evidence you or the phone give.

## Event intelligence (`core/event_intel.py`, tool `situations`)
Normalises every stored event to the full schema (category, source, device, mission, project, run, evidence, confidence, priority, privacy, expiry, responses). Related events (same run / mission / project, or problems on one device within 10 minutes) are merged into one situation with a diagnosis: first event, what changed before (optional reader, e.g. git), earlier similar events, next steps as text. Confidence is "high" only when a log or exit code was captured; otherwise the text says the cause is a guess and what is missing. New event `BuildFailed` is published when a project started by `dev_session` crashes (notification: "<project> crashed"). `decide` reports each situation once.
Limits: git / history readers are not wired to the real repository yet (the diagnosis says "what changed before the failure" is unavailable).

## Attention firewall and mode scheduling (`core/attention.py`, `core/modes.py`, tool `attention`)
* `interrupt`: now / digest / hold from priority, category, mode, focus timer, full-screen app, meeting. Urgent security notices and emergencies are never delayed. Held notices stay in the notification center.
* `allow`: jobs are critical / light (never held), background, heavy, network. Modes carry `background`, `heavy`, `network` flags: gaming and presentation hold chores and heavy work; meeting holds heavy work; power saving holds both; **offline** stops research re-checks and other internet work while everything local keeps running; **quiet** only silences speech. The perf plan (busy CPU, low battery) also holds chores. The daily backup is still forced after 3 days.
* Wired into: spoken notices (`speak_up`), the daily backup deferral and research re-checks in `Upkeep`. New voice words: quiet / do not disturb, offline, power saving / battery saver mode.
Limits: the full-screen reader (Windows API) is unverified on hardware; if it cannot tell, notices are NOT silenced. There is no real meeting detector yet (the `meeting` input is only set by the meeting mode / notes).

## What changed while away (`core/away.py`, tool `what_changed`)
Compares a past moment (your words, or the stored "last active" marker) with now: events, notifications (classified), missions, files in watched folders (names + times only, bounded), devices (connection changes, new, gone), guardian incidents. A source that cannot be read is listed under "could not check". The tool then stores now as the new marker.

## Dependency-aware health (`core/service_health.py`, tool `service_impact`)
Fixed dependency table. If the AI engine is down: conversation is unavailable, meaning-based search and missions are limited, reminders / monitoring / automations / keyword search keep working; task history is untouched. Recovery levels 1-5 are stated; Jarvis stops at level 3 until you approve (existing `diagnostics.repair` only touches Jarvis's own bookkeeping).
