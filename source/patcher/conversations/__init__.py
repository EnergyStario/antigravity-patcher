"""
patcher.conversations -- Antigravity Conversation Recovery & Index Rebuilder.
Reconstructs corrupted, missing, or out-of-order conversation histories in state.vscdb.
"""
from patcher.conversations.rebuilder import (
    rebuild_conversations_index,
    run_rebuild_wizard,
    restore_latest_backup,
    list_backups,
)

__all__ = [
    "rebuild_conversations_index",
    "run_rebuild_wizard",
    "restore_latest_backup",
    "list_backups",
]

