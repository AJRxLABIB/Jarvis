"""Document answers say which page they come from and warn when a file changed since indexing; sensitive files are
never indexed; Bangla digits work in direct commands."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

from app.memory import semantic


def test_paged_chunks_carry_the_page():
    text = "\n\n".join(f"[p. {n}]\n" + (f"Page {n} sentence. " * 60) for n in (1, 2, 3))
    pieces = semantic.paged_chunks(text)
    assert all(p.startswith("[p. ") for p in pieces)
    assert pieces[0].startswith("[p. 1]") and pieces[-1].startswith("[p. 3]")
    assert semantic.paged_chunks("no pages here. " * 10) == semantic.chunks("no pages here. " * 10)


def test_sensitive_files_are_not_indexed():
    for name in ("passwords.txt", "My Passwords.docx", "aws_credentials.md", ".env", "recovery-codes.txt"):
        assert semantic.is_sensitive(Path(name)), name
    assert semantic.is_sensitive(Path("AppData/notes.txt"))
    for name in ("report.pdf", "lecture 3.pdf", "Windows notes.md"):
        assert not semantic.is_sensitive(Path(name)), name


def test_answer_has_page_and_stale_warning(tmp_path, monkeypatch):
    from app.memory.database import Database
    from app.tools import knowledge

    db = Database(tmp_path / "x.db")
    db.initialize()
    doc = tmp_path / "guide.pdf"
    doc.write_text("x")
    with db.transaction() as conn:
        conn.execute("INSERT INTO semantic_items (kind, source, chunk, text, mtime, created_at) "
                     "VALUES ('document', ?, 0, '[p. 4] the warranty is two years', ?, 'now')",
                     (str(doc), os.path.getmtime(doc) - 100))
    hit = SimpleNamespace(source=str(doc), chunk=0, text="[p. 4] the warranty is two years", score=0.9)

    async def fake_search(ctx, question, kind, count):
        return [hit]

    monkeypatch.setattr(knowledge, "_search", fake_search)
    ctx = SimpleNamespace(database=db)
    result = asyncio.run(knowledge.ask_documents(knowledge.AskDocsArgs(question="warranty?"), ctx))
    passage = result.data["passages"][0]
    assert passage["page"] == 4 and passage["file"] == "guide.pdf p. 4" and passage["stale"]
    assert "changed (or were removed) since they were indexed" in result.message


def test_bangla_digits_in_direct_commands():
    from app.agent.conversation import _BN_DIGITS

    assert "ভলিউম ৫০".translate(_BN_DIGITS) == "ভলিউম 50"
