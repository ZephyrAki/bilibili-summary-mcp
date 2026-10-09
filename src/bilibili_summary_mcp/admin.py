from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import Database


def backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)
    destination.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description="Zephyr MCP local administrator commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("destination", type=Path)
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("job_id")
    group = reconcile_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--actual-seconds", type=int)
    group.add_argument("--confirm-never-submitted", action="store_true")
    args = parser.parse_args()
    database_path = Path(
        os.environ.get("ZEPHYR_DATA_DIR", "/var/lib/bilibili-summary-mcp")
    ) / "mcp.db"
    if args.command == "backup":
        backup(database_path, args.destination)
    elif args.command == "reconcile":
        Database(database_path).reconcile_job(
            args.job_id,
            now=datetime.now(ZoneInfo("Asia/Shanghai")),
            actual_seconds=args.actual_seconds,
            confirm_never_submitted=args.confirm_never_submitted,
        )


if __name__ == "__main__":
    main()
