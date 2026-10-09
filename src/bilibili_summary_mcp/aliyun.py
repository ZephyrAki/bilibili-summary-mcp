from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo


Runner = Callable[[list[str], float], Awaitable[tuple[int, str, str]]]
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class QuotaState:
    quota_total_seconds: int
    quota_remaining_seconds: int
    quota_used_seconds: int
    quota_expires_at: str
    quota_observed_at: str


@dataclass(frozen=True)
class AutoStopState:
    free_tier_autostop: bool
    autostop_observed_at: str


class ProviderError(RuntimeError):
    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


def _redact(value: str) -> str:
    value = re.sub(
        r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)((?:access[_-]?key|api[_-]?key|secret|token)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        value,
    )
    return value[:500]


async def _subprocess_runner(
    command: list[str], timeout: float
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ProviderError("CLI_TIMEOUT", "百炼额度查询超时。") from None
    return (
        int(process.returncode or 0),
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


class AliyunQuotaProvider:
    """The only module that understands Bailian CLI output shapes."""

    def __init__(
        self,
        *,
        runner: Runner = _subprocess_runner,
        model: str = "paraformer-v2",
        region: str = "cn-beijing",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.runner = runner
        self.model = model
        self.region = region
        self.timeout_seconds = timeout_seconds

    def _command(self, operation: str) -> list[str]:
        if operation == "autostop-status":
            return [
                "bl",
                "console",
                "call",
                "--api",
                "zeldaEasy.bailian-commerce.freeTrial.queryFreeTierOnlyStatus",
                "--data",
                '{"queryFreeTierOnlyStatusRequest":{"models":["paraformer-v2"]}}',
                "--console-region",
                self.region,
                "--output",
                "json",
            ]
        return [
            "bl",
            "usage",
            operation,
            "--model",
            self.model,
            "--console-region",
            self.region,
            "--output",
            "json",
        ]

    async def _run(self, operation: str) -> object:
        try:
            code, stdout, stderr = await self.runner(
                self._command(operation), self.timeout_seconds
            )
        except ProviderError:
            raise
        except FileNotFoundError:
            raise ProviderError("CLI_NOT_FOUND", "未找到百炼 CLI。") from None
        except Exception as exc:
            raise ProviderError("CLI_EXECUTION_FAILED", _redact(str(exc))) from None
        if code != 0:
            if code == 3 and any(marker in (stderr + stdout).lower()
                                 for marker in ("not logged in", "expired", "no console access token")):
                raise ProviderError("CLI_AUTH_EXPIRED", "百炼控制台授权失效；需重新授权或配置专用 RAM 自动续期。")
            summary = _redact(stderr or stdout or f"百炼 CLI 退出码 {code}")
            raise ProviderError("CLI_FAILED", summary)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            raise ProviderError("CLI_INVALID_JSON", "百炼 CLI 未返回合法 JSON。") from None
        if operation == "autostop-status":
            return payload
        if isinstance(payload, list):
            matches = [item for item in payload if isinstance(item, dict)]
            item = next(
                (candidate for candidate in matches if candidate.get("model") == self.model),
                None,
            )
        elif isinstance(payload, dict):
            item = payload
        else:
            item = None
        if not isinstance(item, dict) or item.get("model", self.model) != self.model:
            raise ProviderError("CLI_SCHEMA_MISMATCH", "额度响应中没有目标模型。")
        return item

    @staticmethod
    def _integer(item: dict[str, object], *names: str) -> int:
        for name in names:
            value = item.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        raise ProviderError("CLI_SCHEMA_MISMATCH", f"额度响应缺少字段 {names[0]}。")

    async def fetch_quota(self) -> QuotaState:
        item = await self._run("free")
        if not isinstance(item, dict):
            raise ProviderError("CLI_SCHEMA_MISMATCH", "额度响应结构无效。")
        total = self._integer(item, "total", "totalSeconds", "quota_total_seconds")
        remaining = self._integer(
            item, "remaining", "remainingSeconds", "quota_remaining_seconds"
        )
        used_value = item.get("used", item.get("usedSeconds"))
        used = int(used_value) if isinstance(used_value, (int, float)) else total - remaining
        expires = item.get("expires", item.get("expiresAt", item.get("resetAt")))
        if not isinstance(expires, str) or not expires:
            raise ProviderError("CLI_SCHEMA_MISMATCH", "额度响应缺少刷新时间。")
        if len(expires) == 10:
            expires = f"{expires}T00:00:00+08:00"
        now = datetime.now(SHANGHAI).isoformat()
        return QuotaState(total, remaining, max(0, used), expires, now)

    async def fetch_autostop(self) -> AutoStopState:
        payload = await self._run("autostop-status")
        item = self._find_autostop_item(payload)
        value = item.get(
            "freeTierOnly", item.get("autoStop", item.get("freeTierAutoStop"))
        )
        if not isinstance(value, bool):
            raise ProviderError("CLI_SCHEMA_MISMATCH", "用完即停响应缺少布尔状态。")
        return AutoStopState(value, datetime.now(SHANGHAI).isoformat())

    def _find_autostop_item(self, payload: object) -> dict[str, object]:
        candidates: list[object] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                statuses = value.get("freeTierOnlyStatuses")
                if isinstance(statuses, list):
                    candidates.extend(statuses)
                else:
                    for nested in value.values():
                        visit(nested)
            elif isinstance(value, list):
                if any(
                    isinstance(item, dict)
                    and any(
                        key in item
                        for key in ("freeTierOnly", "autoStop", "freeTierAutoStop")
                    )
                    for item in value
                ):
                    candidates.extend(value)
                else:
                    for nested in value:
                        visit(nested)

        visit(payload)
        item = next(
            (
                candidate
                for candidate in candidates
                if isinstance(candidate, dict)
                and candidate.get("model", self.model) == self.model
            ),
            None,
        )
        if not isinstance(item, dict):
            raise ProviderError("CLI_SCHEMA_MISMATCH", "用完即停响应缺少目标模型。")
        return item
