import sqlite3
import stat

from bilibili_summary_mcp.admin import backup
from bilibili_summary_mcp.db import Database


def test_online_backup_is_restorable(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    Database(source).initialize()
    backup(source, destination)

    with sqlite3.connect(destination) as restored:
        tables = {
            row[0]
            for row in restored.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "asr_jobs" in tables
    assert "provider_quota_snapshot" in tables
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
