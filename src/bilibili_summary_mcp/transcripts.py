from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import MCPError


@dataclass(frozen=True)
class ParsedTranscript:
    actual_seconds: int | None
    content_duration_ms: int
    original_duration_ms: int | None
    segments: list[dict[str, Any]]


def parse_paraformer_result(payload: dict[str, Any]) -> ParsedTranscript:
    transcripts = payload.get("transcripts")
    if not isinstance(transcripts, list):
        raise MCPError("ASR_RESULT_INVALID", "Paraformer 结果缺少 transcripts。")
    content_duration_ms = 0
    segments: list[dict[str, Any]] = []
    reliable_duration = False
    for transcript in transcripts:
        if not isinstance(transcript, dict) or transcript.get("channel_id", 0) != 0:
            continue
        duration = transcript.get("content_duration_in_milliseconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            content_duration_ms += int(duration)
            reliable_duration = True
        sentences = transcript.get("sentences", [])
        if not isinstance(sentences, list):
            continue
        for sentence in sentences:
            if not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str):
                continue
            segments.append(
                {
                    "begin_ms": sentence.get("begin_time"),
                    "end_ms": sentence.get("end_time"),
                    "text": sentence["text"],
                    "speaker_id": sentence.get("speaker_id"),
                }
            )
    properties = payload.get("properties", {})
    original = properties.get("original_duration_in_milliseconds") if isinstance(properties, dict) else None
    original_duration_ms = int(original) if isinstance(original, (int, float)) else None
    return ParsedTranscript(
        actual_seconds=math.ceil(content_duration_ms / 1000)
        if reliable_duration
        else None,
        content_duration_ms=content_duration_ms,
        original_duration_ms=original_duration_ms,
        segments=segments,
    )
