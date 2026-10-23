from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts.maintain_runtime import backup_database, prune_records

from nutripilot.application.history import ConversationMessage
from nutripilot.infrastructure.history import SQLiteConversationStore
from nutripilot.infrastructure.run_store import SQLiteRunStore


def test_backup_restore_preserves_records_and_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "live.sqlite"
    store = SQLiteRunStore(source)
    history = SQLiteConversationStore(store)
    history.append(1, "a", ConversationMessage(id="1", role="user", content="保留消息"))
    backup = tmp_path / "backup.sqlite"
    backup_database(source, backup)
    with pytest.raises(FileExistsError):
        backup_database(source, backup)
    restored = tmp_path / "restored.sqlite"
    backup_database(backup, restored)
    messages = SQLiteConversationStore(SQLiteRunStore(restored)).messages(1, "a", 100, 0)
    assert messages[0].content == "保留消息"


def test_prune_is_dry_run_by_default_and_keeps_pending_work_and_conversations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.sqlite"
    store = SQLiteRunStore(path)
    history = SQLiteConversationStore(store)
    history.append(1, "thread", ConversationMessage(id="1", role="user", content="历史消息"))
    old = datetime.now(UTC) - timedelta(days=90)
    for trace_id, phase in (("done", "COMPLETED"), ("pending", "AWAITING_CONFIRMATION")):
        store.append_trace(
            {
                "trace_id": trace_id,
                "thread_id": "thread",
                "turn_id": trace_id,
                "user_id": 1,
                "event_type": "test.finished",
                "component": "test",
                "phase_after": phase,
                "created_at": old,
            }
        )
    cutoff = datetime.now(UTC) - timedelta(days=30)
    assert prune_records(path, cutoff) == {"traces": 1, "evaluation_reports": 0}
    assert store.get_trace("done")
    prune_records(path, cutoff, apply=True)
    assert not store.get_trace("done")
    assert store.get_trace("pending")
    assert history.messages(1, "thread", 100, 0)[0].content == "历史消息"
