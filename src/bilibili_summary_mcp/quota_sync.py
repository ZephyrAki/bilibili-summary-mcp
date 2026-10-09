from __future__ import annotations

import asyncio
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from .aliyun import AliyunQuotaProvider, ProviderError
from .db import Database


SHANGHAI = ZoneInfo("Asia/Shanghai")


class QuotaSynchronizer:
    def __init__(self, db: Database, provider: AliyunQuotaProvider, interval: int) -> None:
        self.db = db
        self.provider = provider
        self.interval = interval

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.sync_once()
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.interval + random.uniform(0, 15)
                )
            except TimeoutError:
                pass

    async def sync_once(self) -> None:
        now = datetime.now(SHANGHAI).isoformat()
        updates: dict[str, object] = {}
        errors: list[str] = []
        try:
            quota = await self.provider.fetch_quota()
            updates.update(vars(quota), quota_synced_at=now, quota_sync_status="success", quota_error_code=None)
        except ProviderError as exc:
            updates.update(quota_synced_at=now, quota_sync_status="failed", quota_error_code=exc.code)
            errors.append(f"quota:{exc.code}")
        try:
            autostop = await self.provider.fetch_autostop()
            updates.update(vars(autostop), autostop_synced_at=now, autostop_sync_status="success", autostop_error_code=None)
        except ProviderError as exc:
            updates.update(autostop_synced_at=now, autostop_sync_status="failed", autostop_error_code=exc.code)
            errors.append(f"autostop:{exc.code}")
        previous = self.db.read_provider_snapshot() or {}
        failures = 0 if not errors else int(previous.get("consecutive_failures", 0)) + 1
        updates["consecutive_failures"] = failures
        updates["recent_error_summary"] = ",".join(errors) or None
        self.db.merge_provider_snapshot(updates)

    async def sync_after_terminal(self) -> None:
        await asyncio.sleep(120)
        await self.sync_once()
