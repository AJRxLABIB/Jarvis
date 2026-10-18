"""Local SQLite database with versioned, forward-only migrations.

* One short-lived connection per unit of work (safe across threads).
* WAL journal mode so the GUI can read while a worker writes.
* Before migrating an existing database, a backup copy is written next to it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.utils.logging_setup import get_logger, log_event

_log = get_logger("database")


class StorageError(Exception):
    """Database problem with a message that can be shown to the user."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "initial_schema",
        (
            """
            CREATE TABLE memories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 2000),
                category    TEXT NOT NULL DEFAULT 'general',
                subject_key TEXT,
                source      TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'assistant')),
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_memories_subject ON memories (subject_key)",
            "CREATE INDEX idx_memories_category ON memories (category)",
            """
            CREATE TABLE conversations (
                id               TEXT PRIMARY KEY,
                title            TEXT,
                model            TEXT,
                started_at       TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                message_count    INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0)
            )
            """,
            """
            CREATE TABLE automation_tasks (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                trigger_type   TEXT NOT NULL,
                trigger_config TEXT NOT NULL DEFAULT '{}',
                actions        TEXT NOT NULL DEFAULT '[]',
                last_run_at    TEXT,
                next_run_at    TEXT,
                last_status    TEXT,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_automation_next_run ON automation_tasks (enabled, next_run_at)",
            """
            CREATE TABLE settings_kv (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE audit_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT NOT NULL,
                event            TEXT NOT NULL,
                tool             TEXT,
                permission_level INTEGER CHECK (permission_level BETWEEN 0 AND 3),
                decision         TEXT,
                details          TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX idx_audit_ts ON audit_log (ts)",
        ),
    ),
    Migration(
        2,
        "memory_values",
        (
            # the part of a fact after "is", e.g. the path in "my project folder is D:\\Projects"
            "ALTER TABLE memories ADD COLUMN value TEXT",
            "CREATE INDEX idx_memories_updated ON memories (updated_at)",
        ),
    ),
    Migration(
        3,
        "semantic_search",
        (
            # searchable text with its embedding: past conversations ("history") and your documents ("document")
            """
            CREATE TABLE semantic_items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT NOT NULL CHECK (kind IN ('history', 'document')),
                source     TEXT NOT NULL,
                chunk      INTEGER NOT NULL DEFAULT 0,
                text       TEXT NOT NULL,
                embedding  BLOB,
                model      TEXT,
                mtime      REAL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_semantic_kind ON semantic_items (kind, source)",
        ),
    ),
    Migration(
        4,
        "habits",
        (
            # which apps you open when (local time), for "you usually open VS Code now" suggestions
            """
            CREATE TABLE usage_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL,
                day      TEXT NOT NULL,
                weekday  INTEGER NOT NULL,
                minute   INTEGER NOT NULL
            )
            """,
            "CREATE INDEX idx_usage_day ON usage_events (day)",
        ),
    ),
    Migration(
        5,
        "todos",
        (
            # your to-do list ("add buy milk to my list")
            """
            CREATE TABLE todos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                text       TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 300),
                due        TEXT,
                done       INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
                created_at TEXT NOT NULL,
                done_at    TEXT
            )
            """,
            "CREATE INDEX idx_todos_done ON todos (done, due)",
        ),
    ),
    Migration(
        6,
        "memory_suggestions",
        (
            # facts the assistant noticed in conversation; saved as memories only when you accept them
            """
            CREATE TABLE memory_suggestions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                text       TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 300),
                status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_suggestions_status ON memory_suggestions (status)",
        ),
    ),
    Migration(
        7,
        "learning",
        (
            # what went wrong (not understood, cancelled, corrected), for the weekly self-review
            """
            CREATE TABLE feedback_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT NOT NULL,
                text       TEXT NOT NULL CHECK (length(text) <= 300),
                detail     TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_feedback_kind ON feedback_events (kind, created_at)",
            # answers you said were good: examples for the model
            """
            CREATE TABLE answer_examples (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                question   TEXT NOT NULL CHECK (length(question) <= 500),
                answer     TEXT NOT NULL CHECK (length(answer) <= 1500),
                created_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        8,
        "agent_runs",
        (
            # one row per request (voice, text, phone, automation): its ID ties the audit entries together
            """
            CREATE TABLE agent_runs (
                id         TEXT PRIMARY KEY,
                source     TEXT NOT NULL,
                text       TEXT NOT NULL DEFAULT '',
                state      TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at   TEXT,
                elapsed_ms INTEGER,
                tools      TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX idx_runs_started ON agent_runs (started_at)",
            "ALTER TABLE audit_log ADD COLUMN run_id TEXT",
            "CREATE INDEX idx_audit_run ON audit_log (run_id)",
        ),
    ),
    Migration(
        9,
        "undo",
        (
            # how to reverse an action ("undo your last action"): the inverse is an ordinary tool call
            """
            CREATE TABLE undo_records (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT,
                created_at   TEXT NOT NULL,
                tool         TEXT NOT NULL,
                description  TEXT NOT NULL,
                inverse_tool TEXT NOT NULL,
                inverse_args TEXT NOT NULL,
                undone       INTEGER NOT NULL DEFAULT 0 CHECK (undone IN (0, 1))
            )
            """,
            "CREATE INDEX idx_undo_created ON undo_records (undone, created_at)",
        ),
    ),
    Migration(
        10,
        "goals_and_missions",
        (
            # goals you set ("finish my project this month") and their tasks
            """
            CREATE TABLE goals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                title        TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
                why          TEXT NOT NULL DEFAULT '',
                deadline     TEXT,
                priority     INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
                status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done', 'dropped')),
                progress     INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                completed_at TEXT
            )
            """,
            """
            CREATE TABLE goal_tasks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id    INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
                position   INTEGER NOT NULL,
                text       TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 200),
                depends_on INTEGER,
                done       INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
                done_at    TEXT
            )
            """,
            "CREATE INDEX idx_goal_tasks ON goal_tasks (goal_id, position)",
            # missions: multi-step tasks that survive a restart; every step is an ordinary tool call
            """
            CREATE TABLE missions (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
                goal_id    INTEGER,
                state      TEXT NOT NULL,
                run_id     TEXT,
                note       TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE mission_steps (
                mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
                n          INTEGER NOT NULL,
                title      TEXT NOT NULL,
                tool       TEXT NOT NULL,
                arguments  TEXT NOT NULL DEFAULT '{}',
                depends_on TEXT NOT NULL DEFAULT '[]',
                state      TEXT NOT NULL DEFAULT 'pending',
                attempts   INTEGER NOT NULL DEFAULT 0,
                result     TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (mission_id, n)
            )
            """,
            "CREATE INDEX idx_missions_state ON missions (state, updated_at)",
        ),
    ),
    Migration(
        11,
        "memory_firewall",
        (
            # where each memory came from, how far it's trusted, how private it is, until when, and its history
            "ALTER TABLE memories ADD COLUMN provenance TEXT NOT NULL DEFAULT 'user'",
            "ALTER TABLE memories ADD COLUMN run_id TEXT",
            "ALTER TABLE memories ADD COLUMN sensitivity TEXT NOT NULL DEFAULT 'normal'",
            "ALTER TABLE memories ADD COLUMN expires_at TEXT",
            "ALTER TABLE memories ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE memories ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0",
            """
            CREATE TABLE memory_revisions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id  INTEGER NOT NULL,
                revision   INTEGER NOT NULL,
                content    TEXT NOT NULL,
                provenance TEXT NOT NULL,
                changed_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_memory_revisions ON memory_revisions (memory_id, revision)",
        ),
    ),
    Migration(
        12,
        "events_and_notifications",
        (
            # what happened (the event bus log, bounded) and the notification center
            """
            CREATE TABLE events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                name    TEXT NOT NULL,
                run_id  TEXT,
                data    TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX idx_events_name ON events (name, ts)",
            """
            CREATE TABLE notifications (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                source    TEXT NOT NULL,
                category  TEXT NOT NULL,
                priority  TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
                title     TEXT NOT NULL,
                body      TEXT NOT NULL DEFAULT '',
                action    TEXT NOT NULL DEFAULT '',
                read      INTEGER NOT NULL DEFAULT 0 CHECK (read IN (0, 1)),
                archived  INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
            )
            """,
            "CREATE INDEX idx_notifications_open ON notifications (archived, read, ts)",
        ),
    ),
    Migration(
        13,
        "devices",
        (
            # your devices (this PC and others you add) and what each can really do
            """
            CREATE TABLE devices (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 60),
                kind       TEXT NOT NULL DEFAULT 'computer',
                mac        TEXT NOT NULL DEFAULT '',
                address    TEXT NOT NULL DEFAULT '',
                is_self    INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0, 1)),
                last_seen  TEXT,
                last_state TEXT NOT NULL DEFAULT 'unknown',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE device_capabilities (
                device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                capability  TEXT NOT NULL,
                status      TEXT NOT NULL CHECK (status IN ('supported', 'verified', 'unsupported', 'unknown')),
                detail      TEXT NOT NULL DEFAULT '',
                checked_at  TEXT NOT NULL,
                PRIMARY KEY (device_id, capability)
            )
            """,
        ),
    ),
    Migration(
        14,
        "evolution",
        (
            # improvement proposals for Jarvis itself: evidence, the change, test results, what was applied
            """
            CREATE TABLE evolution_proposals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                kind        TEXT NOT NULL CHECK (kind IN ('insight', 'code')),
                title       TEXT NOT NULL,
                reason      TEXT NOT NULL DEFAULT '',
                evidence    TEXT NOT NULL DEFAULT '{}',
                risk        TEXT NOT NULL DEFAULT 'low',
                state       TEXT NOT NULL,
                edits       TEXT NOT NULL DEFAULT '[]',
                report      TEXT NOT NULL DEFAULT '',
                applied_at  TEXT,
                backup      TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX idx_evolution_state ON evolution_proposals (state, created_at)",
        ),
    ),
    Migration(
        15,
        "skill_versions",
        (
            # earlier versions of workflows / automations ("skills"): kept when one is changed or deleted
            """
            CREATE TABLE automation_revisions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id        TEXT NOT NULL,
                name           TEXT NOT NULL,
                version        INTEGER NOT NULL,
                saved_at       TEXT NOT NULL,
                reason         TEXT NOT NULL CHECK (reason IN ('changed', 'deleted')),
                trigger_type   TEXT NOT NULL,
                trigger_config TEXT NOT NULL,
                actions        TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_automation_revisions ON automation_revisions (name, version)",
        ),
    ),
    Migration(
        16,
        "mobile_devices",
        (
            # JARVIS Mobile: each phone's PUBLIC key (the private key never leaves the phone), trust and permissions
            """
            CREATE TABLE mobile_devices (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                platform     TEXT NOT NULL DEFAULT 'android',
                os_version   TEXT NOT NULL DEFAULT '',
                app_version  TEXT NOT NULL DEFAULT '',
                public_key   TEXT NOT NULL,
                fingerprint  TEXT NOT NULL UNIQUE,
                key_info     TEXT NOT NULL DEFAULT '{}',
                trust        TEXT NOT NULL CHECK (trust IN ('pending', 'trusted', 'restricted', 'revoked')),
                permissions  TEXT NOT NULL DEFAULT '[]',
                paired_at    TEXT NOT NULL,
                trusted_at   TEXT,
                last_seen    TEXT,
                revoked_at   TEXT
            )
            """,
            # nonces already used (replay protection); kept a little longer than the accepted clock window
            """
            CREATE TABLE mobile_nonces (
                device_id TEXT NOT NULL,
                nonce     TEXT NOT NULL,
                seen_at   REAL NOT NULL,
                PRIMARY KEY (device_id, nonce)
            )
            """,
            "CREATE INDEX idx_mobile_nonces_seen ON mobile_nonces (seen_at)",
            """
            CREATE TABLE mobile_security_events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                at        TEXT NOT NULL,
                device_id TEXT,
                kind      TEXT NOT NULL,
                detail    TEXT NOT NULL DEFAULT ''
            )
            """,
        ),
    ),
    Migration(
        17,
        "memory_2",
        (
            # memory types, confidence (stated = 1.0, inferred lower), importance, tags, and what replaced it
            "ALTER TABLE memories ADD COLUMN kind TEXT NOT NULL DEFAULT 'semantic'",
            "ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0",
            "ALTER TABLE memories ADD COLUMN importance INTEGER NOT NULL DEFAULT 2",
            "ALTER TABLE memories ADD COLUMN stated INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE memories ADD COLUMN superseded_by INTEGER",
            # relationships between memories: supersedes, contradicts, related_to
            """
            CREATE TABLE memory_links (
                from_id    INTEGER NOT NULL,
                to_id      INTEGER NOT NULL,
                relation   TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (from_id, to_id, relation)
            )
            """,
            # possible contradictions waiting for you to say which one is true
            """
            CREATE TABLE memory_conflicts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                new_id     INTEGER NOT NULL,
                old_id     INTEGER NOT NULL,
                reason     TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'kept_new', 'kept_old', 'kept_both')),
                created_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        18,
        "missions_2",
        (
            # milestones, approval checkpoints, and how to undo each finished step (for rolling a mission back)
            "ALTER TABLE mission_steps ADD COLUMN milestone TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE mission_steps ADD COLUMN checkpoint INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE mission_steps ADD COLUMN inverse TEXT NOT NULL DEFAULT ''",
        ),
    ),
    Migration(
        19,
        "mobile_2",
        (
            # JARVIS Mobile 1.0: a second phone key usable only after fingerprint / face / phone PIN (signs power,
            # control, files, screen and approvals), and the remote-session audit trail
            "ALTER TABLE mobile_devices ADD COLUMN auth_public_key TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE mobile_devices ADD COLUMN auth_fingerprint TEXT NOT NULL DEFAULT ''",
            """
            CREATE TABLE mobile_sessions (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                last_activity TEXT NOT NULL,
                ended_at TEXT,
                end_reason TEXT NOT NULL DEFAULT '',
                scopes TEXT NOT NULL DEFAULT '[]',
                network TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX idx_mobile_sessions_device ON mobile_sessions (device_id)",
        ),
    ),
    Migration(
        20,
        "approval_grants",
        (
            # Approval system 2.0: "don't ask me again about <this> for <a while>" - moderate actions only,
            # always time-limited, revocable, ignored while untrusted text is steering or an emergency switch is on
            """
            CREATE TABLE approval_grants (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tool        TEXT NOT NULL,
                match       TEXT NOT NULL DEFAULT '{}',
                scope       TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                revoked_at  TEXT,
                uses        INTEGER NOT NULL DEFAULT 0,
                note        TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX idx_grants_tool ON approval_grants (tool)",
        ),
    ),
    Migration(
        21,
        "task_events",
        (
            # Execution history: every state change of a mission/task with the reason, so "what happened and why"
            # can be answered without reconstructing the session
            """
            CREATE TABLE task_events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id   TEXT NOT NULL,
                kind      TEXT NOT NULL DEFAULT 'mission',
                ts        TEXT NOT NULL,
                old_state TEXT NOT NULL DEFAULT '',
                new_state TEXT NOT NULL,
                note      TEXT NOT NULL DEFAULT '',
                run_id    TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX idx_task_events_task ON task_events (task_id, id)",
        ),
    ),
    Migration(
        22,
        "todos_2",
        (
            # priorities, categories, repeating items, "after X" dependencies and how often an item was postponed
            "ALTER TABLE todos ADD COLUMN priority INTEGER NOT NULL DEFAULT 2",
            "ALTER TABLE todos ADD COLUMN category TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE todos ADD COLUMN repeat TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE todos ADD COLUMN depends_on INTEGER",
            "ALTER TABLE todos ADD COLUMN postponed INTEGER NOT NULL DEFAULT 0",
        ),
    ),
    Migration(
        23,
        "metrics",
        (
            # a light history of how the PC is doing (one row a minute at most), for trends and alerts
            """
            CREATE TABLE metrics (
                ts           TEXT NOT NULL,
                cpu          REAL,
                mem_percent  REAL,
                disk_percent REAL,
                battery      REAL,
                plugged      INTEGER
            )
            """,
            "CREATE INDEX idx_metrics_ts ON metrics (ts)",
        ),
    ),
    Migration(
        24,
        "workflow_progress",
        (
            # where a workflow stopped, so "resume" continues from that step instead of starting over
            """
            CREATE TABLE workflow_progress (
                task_id    TEXT PRIMARY KEY,
                next_step  INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        25,
        "voice_events",
        (
            # how voice interaction is actually going: one row per thing heard, no audio and no text
            """
            CREATE TABLE voice_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                status       TEXT NOT NULL,
                language     TEXT,
                confidence   REAL,
                stt_ms       INTEGER,
                first_reply_ms INTEGER,
                handled      INTEGER
            )
            """,
            "CREATE INDEX idx_voice_events_ts ON voice_events (ts)",
        ),
    ),
    Migration(
        26,
        "personal_workspace",
        (
            # estimated minutes for a to-do (the daily plan needs a time budget)
            "ALTER TABLE todos ADD COLUMN minutes INTEGER",
            # the plan made for a day: only to-do ids and reasons, so no task is ever duplicated
            """
            CREATE TABLE day_plans (
                day        TEXT PRIMARY KEY,
                plan       TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            # named research projects: sources and the claims each one supports
            """
            CREATE TABLE research_projects (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
                question   TEXT NOT NULL,
                criteria   TEXT NOT NULL DEFAULT '',
                conclusion TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE research_sources (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                url         TEXT NOT NULL,
                title       TEXT NOT NULL DEFAULT '',
                source_date TEXT,
                fetched_at  TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                UNIQUE (project_id, url)
            )
            """,
            """
            CREATE TABLE research_claims (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
                source_id  INTEGER REFERENCES research_sources(id) ON DELETE CASCADE,
                topic      TEXT NOT NULL,
                claim      TEXT NOT NULL,
                kind       TEXT NOT NULL DEFAULT 'sourced' CHECK (kind IN ('sourced', 'inference')),
                needs_recheck INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                CHECK (kind = 'inference' OR source_id IS NOT NULL)
            )
            """,
            # learning: one row per thing being learned, with spaced-repetition state
            """
            CREATE TABLE learning_items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                topic      TEXT NOT NULL,
                question   TEXT NOT NULL,
                answer     TEXT NOT NULL,
                source     TEXT NOT NULL DEFAULT 'you',
                language   TEXT NOT NULL DEFAULT 'en',
                ease       REAL NOT NULL DEFAULT 2.5,
                interval_days INTEGER NOT NULL DEFAULT 0,
                repetitions INTEGER NOT NULL DEFAULT 0,
                due        TEXT NOT NULL,
                correct    INTEGER NOT NULL DEFAULT 0,
                wrong      INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_learning_due ON learning_items (due)",
            # email drafts: written, edited and approved by you; never sent from here
            """
            CREATE TABLE mail_drafts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                to_addr    TEXT NOT NULL DEFAULT '',
                subject    TEXT NOT NULL,
                body       TEXT NOT NULL,
                in_reply_to TEXT NOT NULL DEFAULT '',
                state      TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'approved', 'discarded', 'sent')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        27,
        "audit_chain_and_process_samples",
        (
            # tamper-evident audit trail (see app/core/auditchain.py)
            "ALTER TABLE audit_log ADD COLUMN prev_hash TEXT",
            "ALTER TABLE audit_log ADD COLUMN entry_hash TEXT",
            """
            CREATE TABLE audit_head (
                id        INTEGER PRIMARY KEY CHECK (id = 1),
                last_id   INTEGER NOT NULL,
                last_hash TEXT NOT NULL
            )
            """,
            # per-process resource samples (name and numbers only; never command lines or window titles)
            """
            CREATE TABLE process_samples (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                name    TEXT NOT NULL,
                cpu     REAL NOT NULL,
                mem_mb  REAL NOT NULL
            )
            """,
            "CREATE INDEX idx_process_samples_ts ON process_samples (ts)",
            "CREATE INDEX idx_process_samples_name ON process_samples (name)",
            # watched research projects: re-checked on a schedule you switch on
            "ALTER TABLE research_projects ADD COLUMN watch_days INTEGER",
            "ALTER TABLE research_projects ADD COLUMN last_checked TEXT",
        ),
    ),
    Migration(
        28,
        "integration_tests",
        (
            # the last smoke test of each outside service: what was checked, the result and when (never a secret)
            """
            CREATE TABLE integration_tests (
                key       TEXT PRIMARY KEY,
                verdict   TEXT NOT NULL,
                detail    TEXT NOT NULL DEFAULT '',
                tested_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        29,
        "workflow_progress_outcome",
        (
            # how the checkpointed step ended: 'failed' = it definitely did nothing; NULL = unknown (a crash, a timeout, a
            # lost answer). An unknown outcome of a state-changing step is never repeated without you saying so.
            "ALTER TABLE workflow_progress ADD COLUMN outcome TEXT",
        ),
    ),
    Migration(
        30,
        "phone_sync",
        (
            # which data each paired phone may sync (nothing until YOU switch a dataset on, on the PC)
            """
            CREATE TABLE sync_prefs (
                device_id    TEXT NOT NULL,
                dataset      TEXT NOT NULL,
                enabled      INTEGER NOT NULL DEFAULT 0,
                updated_at   TEXT NOT NULL,
                last_sync_at TEXT,
                last_error   TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (device_id, dataset)
            )
            """,
            # an append-only change log of the synced data (written by triggers, so every change is seen whoever made it)
            """
            CREATE TABLE sync_log (
                seq       INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset   TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                op        TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
                rev       INTEGER NOT NULL,
                payload   TEXT NOT NULL DEFAULT '{}',
                ts        TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_sync_log_record ON sync_log (dataset, record_id)",
            # operations already applied for a phone: a repeated delivery returns the saved answer instead of applying twice
            """
            CREATE TABLE sync_ops (
                device_id  TEXT NOT NULL,
                op_id      TEXT NOT NULL,
                result     TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                PRIMARY KEY (device_id, op_id)
            )
            """,
            """
            CREATE TRIGGER sync_todos_insert AFTER INSERT ON todos BEGIN
                INSERT INTO sync_log (dataset, record_id, op, rev, payload, ts)
                VALUES ('todos', NEW.id, 'upsert',
                        (SELECT COALESCE(MAX(rev), 0) + 1 FROM sync_log WHERE dataset = 'todos' AND record_id = NEW.id),
                        json_object('text', NEW.text, 'due', NEW.due, 'done', NEW.done, 'done_at', NEW.done_at,
                                    'minutes', NEW.minutes),
                        strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
            END
            """,
            """
            CREATE TRIGGER sync_todos_update AFTER UPDATE ON todos BEGIN
                INSERT INTO sync_log (dataset, record_id, op, rev, payload, ts)
                VALUES ('todos', NEW.id, 'upsert',
                        (SELECT COALESCE(MAX(rev), 0) + 1 FROM sync_log WHERE dataset = 'todos' AND record_id = NEW.id),
                        json_object('text', NEW.text, 'due', NEW.due, 'done', NEW.done, 'done_at', NEW.done_at,
                                    'minutes', NEW.minutes),
                        strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
            END
            """,
            """
            CREATE TRIGGER sync_todos_delete AFTER DELETE ON todos BEGIN
                INSERT INTO sync_log (dataset, record_id, op, rev, payload, ts)
                VALUES ('todos', OLD.id, 'delete',
                        (SELECT COALESCE(MAX(rev), 0) + 1 FROM sync_log WHERE dataset = 'todos' AND record_id = OLD.id),
                        '{}', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
            END
            """,
        ),
    ),    Migration(
        31,
        "push_tokens",
        (
            # the Firebase Cloud Messaging token a paired phone registered (a push address, not a login) and how far the
            # push dispatcher has read the events table for it
            """
            CREATE TABLE push_tokens (
                device_id     TEXT PRIMARY KEY,
                token         TEXT NOT NULL CHECK (length(token) BETWEEN 20 AND 4096),
                registered_at TEXT NOT NULL,
                last_event_id INTEGER NOT NULL DEFAULT 0,
                last_push_at  TEXT,
                last_error    TEXT NOT NULL DEFAULT ''
            )
            """,
        ),
    ),    Migration(
        32,
        "clipboard_items",
        (
            # opt-in clipboard memory (CLIPBOARD_HISTORY=true): text only, secrets and card numbers are never stored
            """
            CREATE TABLE clipboard_items (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     TEXT NOT NULL,
                text   TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 4000),
                sha    TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX idx_clipboard_ts ON clipboard_items (ts)",
            "CREATE INDEX idx_clipboard_sha ON clipboard_items (sha)",
        ),
    ),
    Migration(
        33,
        "expenses",
        (
            # manual, local expense records and monthly budgets (no bank connection, nothing leaves the PC)
            """
            CREATE TABLE expenses (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                amount   REAL NOT NULL CHECK (amount > 0 AND amount < 1e12),
                currency TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'other' CHECK (length(category) BETWEEN 1 AND 40),
                note     TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 200)
            )
            """,
            "CREATE INDEX idx_expenses_ts ON expenses (ts)",
            """
            CREATE TABLE budgets (
                category TEXT PRIMARY KEY CHECK (length(category) BETWEEN 1 AND 40),
                monthly  REAL NOT NULL CHECK (monthly > 0 AND monthly < 1e12)
            )
            """,
        ),
    ),
    Migration(
        34,
        "phone_link",
        (
            # PC -> phone actions (the phone collects them when it checks in) and the phone's last reported state
            """
            CREATE TABLE phone_commands (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id  TEXT NOT NULL,
                action     TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 30),
                args       TEXT NOT NULL DEFAULT '{}',
                created    TEXT NOT NULL,
                expires    TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'sent', 'done', 'failed', 'expired')),
                result     TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX idx_phone_commands_device ON phone_commands (device_id, status)",
            """
            CREATE TABLE phone_state (
                device_id TEXT PRIMARY KEY,
                seen      TEXT NOT NULL,
                state     TEXT NOT NULL DEFAULT '{}'
            )
            """,
            # journal entries (typed or spoken), searchable; separate from memories and project notes
            """
            CREATE TABLE journal (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     TEXT NOT NULL,
                text   TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 8000),
                mood   TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'text' CHECK (source IN ('text', 'voice'))
            )
            """,
            "CREATE INDEX idx_journal_ts ON journal (ts)",
            # personal habits you tick off ("drink water", "exercise"); separate from the app-usage habits of v4
            """
            CREATE TABLE habit_defs (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(name) BETWEEN 1 AND 60),
                created  TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE habit_checks (
                habit_id INTEGER NOT NULL REFERENCES habit_defs (id) ON DELETE CASCADE,
                day      TEXT NOT NULL,
                PRIMARY KEY (habit_id, day)
            )
            """,
        ),
    ),
    Migration(
        35,
        "learning_loop",
        (
            # every request and how it turned out (decided by what happened next) - app/agent/growth.py
            """
            CREATE TABLE learning_turns (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                request  TEXT NOT NULL,
                tools    TEXT NOT NULL DEFAULT '[]',
                model    TEXT NOT NULL DEFAULT '',
                used     TEXT NOT NULL DEFAULT '[]',
                outcome  TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending', 'good', 'bad')),
                reason   TEXT NOT NULL DEFAULT '',
                corrects INTEGER
            )
            """,
            "CREATE INDEX idx_learning_turns_ts ON learning_turns (ts)",
            # what was learned: skills (request -> tools that worked) and lessons (a mistake and what fixed it)
            """
            CREATE TABLE growth_items (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                kind    TEXT NOT NULL CHECK (kind IN ('skill', 'lesson')),
                request TEXT NOT NULL,
                content TEXT NOT NULL,
                tool    TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL,
                uses    INTEGER NOT NULL DEFAULT 0,
                good    INTEGER NOT NULL DEFAULT 0,
                bad     INTEGER NOT NULL DEFAULT 0,
                retired INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE UNIQUE INDEX idx_growth_items_key ON growth_items (kind, request, tool)",
        ),
    ),
    Migration(
        36,
        "phone_command_states",
        (
            # PC -> phone actions can also be 'cancelled' (before the phone took them) or 'unsupported' (the phone's
            # app doesn't know the action): SQLite can't change a CHECK, so the table is rebuilt, rows kept
            """
            CREATE TABLE phone_commands_v36 (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id  TEXT NOT NULL,
                action     TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 30),
                args       TEXT NOT NULL DEFAULT '{}',
                created    TEXT NOT NULL,
                expires    TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'sent', 'done', 'failed', 'expired', 'cancelled', 'unsupported')),
                result     TEXT NOT NULL DEFAULT ''
            )
            """,
            "INSERT INTO phone_commands_v36 (id, device_id, action, args, created, expires, status, result) "
            "SELECT id, device_id, action, args, created, expires, status, result FROM phone_commands",
            "DROP TABLE phone_commands",
            "ALTER TABLE phone_commands_v36 RENAME TO phone_commands",
            "CREATE INDEX idx_phone_commands_device ON phone_commands (device_id, status)",
        ),
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path, *, timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.timeout = timeout

    # ---------------------------------------------------------------- connections

    def _open(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not open the database at {self.path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Autocommit connection, closed on exit."""
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Connection inside BEGIN IMMEDIATE ... COMMIT (ROLLBACK on any error)."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # ----------------------------------------------------------------- migrations

    @staticmethod
    def _current_version(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return int(row[0])

    def _backup(self, version: int) -> Path:
        target = self.path.with_name(f"{self.path.stem}.v{version}.bak{self.path.suffix}")
        source = self._open()
        try:
            dest = sqlite3.connect(str(target))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        log_event(_log, "db_backup_created", path=str(target), schema_version=version)
        return target

    def initialize(self) -> list[int]:
        """Create or upgrade the schema. Returns the migration versions applied."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Could not create the data folder {self.path.parent}: {exc}") from exc
        try:
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                current = self._current_version(conn)

            if current > LATEST_SCHEMA_VERSION:
                raise StorageError(
                    f"The database at {self.path} was created by a newer version of the assistant "
                    f"(schema v{current}; this version supports up to v{LATEST_SCHEMA_VERSION}). "
                    "Update the application or point JARVIS_DATA_DIR at a different folder."
                )

            pending = [m for m in MIGRATIONS if m.version > current]
            if pending and current > 0:
                self._backup(current)

            applied: list[int] = []
            for migration in pending:
                with self.transaction() as conn:
                    for statement in migration.statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, utc_now_iso()),
                    )
                applied.append(migration.version)
                log_event(_log, "db_migration_applied", version=migration.version, name=migration.name)
            return applied
        except sqlite3.Error as exc:  # e.g. "file is not a database", locked, disk I/O
            raise StorageError(f"The database at {self.path} could not be initialised: {exc}") from exc

    # ---------------------------------------------------------------- diagnostics

    def schema_version(self) -> int:
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            return self._current_version(conn) if exists else 0

    def integrity_check(self) -> str:
        """'ok' when healthy, otherwise SQLite's description of the problem."""
        try:
            with self.connect() as conn:
                rows = conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.Error as exc:
            return str(exc)
        return "; ".join(str(r[0]) for r in rows) or "no result"
