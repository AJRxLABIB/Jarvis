"""Short-term conversation context with size limits.

Trimming removes whole turns from the oldest end (a turn is a user message
plus the assistant/tool messages that follow it), so a tool call is never
separated from its result.

What trimming removes is not forgotten outright: a short digest keeps what the user asked and which tools ran
("you asked: 'plan my trip' (tools: web_search)"), so a long conversation can still answer "what did I ask you
first?". The digest holds only the user's own words (secret-looking text redacted) and tool names - never the
assistant's replies or tool output, which may contain text from the web - so it cannot carry an injected instruction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from app.ai.base import ChatMessage
from app.utils.redaction import redact

DIGEST_ENTRIES = 12
DIGEST_CHARS = 1400
_ASKED = 140


def message_size(message: ChatMessage) -> int:
    size = len(message.content)
    for call in message.tool_calls:
        size += len(call.name) + len(json.dumps(dict(call.arguments), ensure_ascii=False, default=str))
    return size


class ConversationContext:
    def __init__(self, max_messages: int = 24, max_chars: int = 16000) -> None:
        if max_messages < 2 or max_chars < 1:
            raise ValueError("context limits are too small")
        self.max_messages = max_messages
        self.max_chars = max_chars
        self._messages: list[ChatMessage] = []
        self.digest: list[str] = []

    def note_dropped(self, messages: Iterable[ChatMessage]) -> None:
        """Remember, in one line each, the user requests in ``messages`` (which are leaving the context) and their tools."""
        batch = list(messages)
        for i, message in enumerate(batch):
            if message.role != "user" or not (message.content or "").strip():
                continue
            asked = " ".join(redact(message.content).split())
            asked = asked[:_ASKED] + ("..." if len(asked) > _ASKED else "")
            tools: list[str] = []
            for follower in batch[i + 1:]:
                if follower.role == "user":
                    break
                for call in follower.tool_calls:
                    if call.name not in tools:
                        tools.append(call.name)
            entry = f"you asked: \"{asked}\"" + (f" (tools: {', '.join(tools[:4])})" if tools else "")
            if entry in self.digest:
                continue
            self.digest.append(entry)
        del self.digest[:-DIGEST_ENTRIES]
        while len(self.digest) > 1 and sum(len(e) for e in self.digest) > DIGEST_CHARS:
            del self.digest[0]

    def __len__(self) -> int:
        return len(self._messages)

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return tuple(self._messages)

    @property
    def total_chars(self) -> int:
        return sum(message_size(m) for m in self._messages)

    def add_turn(self, messages: Iterable[ChatMessage]) -> None:
        """Append a complete turn (must start with a user message), then trim."""
        turn = list(messages)
        if not turn or turn[0].role != "user":
            raise ValueError("a turn must start with a user message")
        if any(m.role == "system" for m in turn):
            raise ValueError("system messages are not stored in the context")
        self._messages.extend(turn)
        self.trim()

    def trim(self) -> None:
        while len(self._messages) > self.max_messages or self.total_chars > self.max_chars:
            next_turn = next((i for i, m in enumerate(self._messages) if i > 0 and m.role == "user"), None)
            if next_turn is None:
                break  # only the latest turn is left; keep it
            self.note_dropped(self._messages[:next_turn])
            del self._messages[:next_turn]

    def clear(self) -> None:
        self._messages.clear()
        self.digest.clear()
