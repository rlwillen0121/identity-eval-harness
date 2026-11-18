"""Bounded, credential-free extraction for a caller-owned review transport.

Pass a binary iterable or an object with ``read1(size)``/``read(size)``. This
module never opens a connection or reads configuration. The caller must impose
a read timeout or external hard wall: an elapsed deadline cannot interrupt a
blocked iterator/read. Progress starts with the supplied, sanitized handshake;
``visible_delta`` contains only newly extracted answer text, never raw events.
"""

from __future__ import annotations

import json
import math
import re
import time
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

_TOKEN_KEYS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "audio_tokens",
        "text_tokens",
        "image_tokens",
        "video_tokens",
        "thinking_tokens",
        "accepted_prediction_tokens",
        "rejected_prediction_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_creation_5m_input_tokens",
        "cache_creation_1h_input_tokens",
        "prompt_token_count",
        "candidates_token_count",
        "total_token_count",
        "thoughts_token_count",
        "cached_content_token_count",
        "tool_use_prompt_tokens",
        "ephemeral_5m_input_tokens",
        "ephemeral_1h_input_tokens",
    }
)
_DETAIL_KEYS = frozenset(
    {
        "prompt_tokens_details",
        "completion_tokens_details",
        "input_tokens_details",
        "output_tokens_details",
        "cache_creation",
    }
)
_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
_TIERS = frozenset({"auto", "default", "standard", "fast", "priority", "flex", "scale"})
_FINISHES = frozenset(
    {
        "stop",
        "end_turn",
        "completed",
        "length",
        "max_tokens",
        "max_output_tokens",
        "content_filter",
        "tool_calls",
        "function_call",
        "pause_turn",
        "native_continuation",
    }
)
_STATUSES = frozenset({"completed", "incomplete", "failed", "cancelled", "in_progress", "queued"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_ERROR_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


@dataclass(frozen=True)
class CaptureResult:
    visible_text: str
    valid_review: bool
    issues: tuple[str, ...]
    metadata: dict[str, Any]


class _Stopped(Exception):
    """Internal control flow; never contains transport or provider text."""


def _safe_usage(value: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(value, dict) or depth > 3:
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _TOKEN_KEYS and isinstance(item, (int, float)) and not isinstance(item, bool):
            if item >= 0 and (not isinstance(item, float) or math.isfinite(item)):
                result[key] = item
        elif key in _DETAIL_KEYS and isinstance(item, dict):
            nested = _safe_usage(item, depth + 1)
            if nested:
                result[key] = nested
    return result


def _text_blocks(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        block["text"]
        for block in value
        if isinstance(block, dict)
        and block.get("type") in {"text", "output_text"}
        and isinstance(block.get("text"), str)
    )


class _Capture:
    def __init__(
        self,
        *,
        status: int,
        content_type: str,
        content_encoding: str,
        timeout_seconds: float,
        clock: Callable[[], float],
        on_progress: Callable[[dict[str, Any]], None] | None,
        max_response_bytes: int,
        max_line_bytes: int,
        max_event_bytes: int,
        max_visible_chars: int,
    ) -> None:
        self.clock = clock
        self.started = clock()
        self.timeout = timeout_seconds
        self.callback = on_progress
        self.max_bytes = max_response_bytes
        self.max_line = max_line_bytes
        self.max_event = max_event_bytes
        self.max_visible = max_visible_chars
        media = content_type.partition(";")[0].strip().lower()
        self.protocol = (
            "sse"
            if media == "text/event-stream"
            else "json"
            if (
                media == "application/json"
                or (media.startswith("application/") and media.endswith("+json"))
            )
            else "unsupported"
        )
        encoding = content_encoding.strip().lower() or "identity"
        self.encoding = encoding if encoding in {"identity", "gzip"} else "unsupported"
        self.meta: dict[str, Any] = {
            "http_status": status,
            "content_type": "text/event-stream"
            if self.protocol == "sse"
            else ("application/json" if self.protocol == "json" else "UNSUPPORTED"),
            "content_encoding": self.encoding,
            "model": "UNAVAILABLE",
            "reasoning_effort": "UNAVAILABLE",
            "service_tier": "UNAVAILABLE",
            "usage": {},
            "finish_reason": "UNAVAILABLE",
            "response_status": "UNAVAILABLE",
            "api_error_code": "UNAVAILABLE",
            "raw_bytes": 0,
            "decoded_bytes": 0,
            "raw_lines": 0,
            "events": 0,
            "comments": 0,
            "visible_chars": 0,
            "done_marker": False,
            "terminal_event": False,
            "native_continuation": False,
        }
        self.issues: list[str] = []
        self.parts: list[str] = []
        self.lines = bytearray()
        self.skip_lf = False
        self.first_line = True
        self.data: list[str] = []
        self.event_bytes = 0
        self.error_event = False
        self.body = bytearray()
        self.chat = False
        self.responses = False
        self.response_text_index: tuple[int | None, int | None] | None = None
        self.response_done_text: str | None = None
        self.emit("handshake")

    def emit(self, observation: str, visible_delta: str = "") -> None:
        if self.callback is not None:
            # A fresh dictionary keeps callbacks from altering capture state.
            snapshot = {
                "observation": observation,
                "elapsed_seconds": max(0.0, self.clock() - self.started),
                "http_status": self.meta["http_status"],
                "content_type": self.meta["content_type"],
                "content_encoding": self.meta["content_encoding"],
                **{
                    key: self.meta[key]
                    for key in (
                        "raw_bytes",
                        "decoded_bytes",
                        "raw_lines",
                        "events",
                        "comments",
                        "visible_chars",
                        "done_marker",
                        "terminal_event",
                    )
                },
                "visible_delta": visible_delta,
            }
            try:
                self.callback(snapshot)
            except Exception:
                # Callback exceptions may also contain private paths or URLs.
                self.issue("progress_callback_error")
                self.callback = None

    def issue(self, code: str) -> None:
        if code not in self.issues:
            self.issues.append(code)

    def stop(self, code: str) -> None:
        self.issue(code)
        raise _Stopped

    def deadline(self) -> None:
        if self.clock() - self.started >= self.timeout:
            self.stop("deadline_exceeded")

    def append(self, text: str) -> None:
        if not text:
            return
        remaining = self.max_visible - self.meta["visible_chars"]
        kept = text[:remaining]
        if kept:
            self.parts.append(kept)
            self.meta["visible_chars"] += len(kept)
            self.emit("visible_text", kept)
        if len(text) > remaining:
            self.stop("visible_text_limit")

    def finish(self, value: Any) -> None:
        if not isinstance(value, str):
            return
        reason = value if value in _FINISHES else "UNKNOWN"
        self.meta["finish_reason"] = reason
        if reason in {"length", "max_tokens", "max_output_tokens"}:
            self.issue("output_limit")
        elif reason == "content_filter":
            self.issue("content_filter")
        elif reason in {"tool_calls", "function_call", "pause_turn", "native_continuation"}:
            self.continuation()
        elif reason == "UNKNOWN":
            self.issue("unknown_finish_reason")

    def continuation(self) -> None:
        self.meta["native_continuation"] = True
        self.issue("native_continuation")

    def envelope_metadata(self, value: dict[str, Any]) -> None:
        model = value.get("model")
        if isinstance(model, str) and _IDENTIFIER.fullmatch(model) and "//" not in model:
            self.meta["model"] = model
        effort = value.get("reasoning_effort")
        if effort is None and isinstance(value.get("reasoning"), dict):
            effort = value["reasoning"].get("effort")
        if isinstance(effort, str) and effort in _EFFORTS:
            self.meta["reasoning_effort"] = effort
        tier = value.get("service_tier")
        if isinstance(tier, str) and tier in _TIERS:
            self.meta["service_tier"] = tier
        if isinstance(value.get("usage"), dict):
            self.meta["usage"] = _safe_usage(value["usage"])
        error = value.get("error")
        if error:
            self.issue("api_error")
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
                self.meta["api_error_code"] = code
        for key in ("continuation", "continuation_token", "native_continuation"):
            if value.get(key):
                self.continuation()
        self.finish(value.get("finish_reason"))
        self.finish(value.get("stop_reason"))

    def response_output(self, value: dict[str, Any]) -> str:
        output = value.get("output")
        chunks: list[str] = []
        text_indices: list[tuple[int, int]] = []
        if isinstance(output, list):
            for output_index, item in enumerate(output):
                if not isinstance(item, dict):
                    continue
                if item.get("type") in {
                    "function_call",
                    "tool_call",
                    "computer_call",
                    "custom_tool_call",
                }:
                    self.continuation()
                elif item.get("type") == "message" and item.get("role", "assistant") == "assistant":
                    content = item.get("content")
                    if isinstance(content, list):
                        for content_index, block in enumerate(content):
                            if (
                                isinstance(block, dict)
                                and block.get("type") in {"text", "output_text"}
                                and isinstance(block.get("text"), str)
                            ):
                                chunks.append(block["text"])
                                text_indices.append((output_index, content_index))
        if self.protocol == "sse" and len(chunks) > 1:
            self.issue("unsupported_multiple_response_text_items")
        elif (
            self.protocol == "sse"
            and text_indices
            and self.response_text_index is not None
            and any(
                streamed is not None and streamed != final
                for streamed, final in zip(self.response_text_index, text_indices[0], strict=True)
            )
        ):
            self.issue("response_text_index_mismatch")
        # output_text is an alternate rendering of output, not a second answer.
        if isinstance(value.get("output_text"), str):
            if isinstance(output, list) and value["output_text"] != "".join(chunks):
                self.issue("response_text_mismatch")
            return value["output_text"]
        return "".join(chunks)

    def response_index(self, value: dict[str, Any]) -> bool:
        """Support one text item; refuse to merge independent item streams."""
        indices = tuple(value.get(key) for key in ("output_index", "content_index"))
        if any(
            index is not None
            and (isinstance(index, bool) or not isinstance(index, int) or index < 0)
            for index in indices
        ):
            self.issue("unsupported_response_text_index")
            return False
        if self.response_text_index is None:
            self.response_text_index = indices
            return True
        if any(
            previous is not None and current is not None and previous != current
            for previous, current in zip(self.response_text_index, indices, strict=True)
        ):
            self.issue("unsupported_multiple_response_text_items")
            return False
        self.response_text_index = tuple(
            previous if current is None else current
            for previous, current in zip(self.response_text_index, indices, strict=True)
        )
        return True

    def reconcile_response_text(self, text: str, *, done: bool = False) -> None:
        """Recover only a missing suffix; conflicting final evidence is invalid."""
        current = "".join(self.parts)
        if self.response_done_text is not None and text != self.response_done_text:
            self.issue("response_text_mismatch")
            return
        if not text.startswith(current):
            self.issue("response_text_mismatch")
            return
        self.append(text[len(current) :])
        if done:
            self.response_done_text = text

    def response_status(self, value: dict[str, Any]) -> None:
        status = value.get("status")
        if status in _STATUSES:
            self.meta["response_status"] = status
        if status == "completed" and self.protocol == "json":
            self.meta["terminal_event"] = True
        elif status in {"incomplete", "failed", "cancelled"}:
            if self.protocol == "json":
                self.meta["terminal_event"] = True
            self.issue("response_" + status)
        details = value.get("incomplete_details")
        if isinstance(details, dict):
            self.finish(details.get("reason"))

    def envelope(self, value: Any) -> None:
        if not isinstance(value, dict):
            self.issue("unsupported_envelope")
            return
        self.envelope_metadata(value)
        kind = value.get("type")
        choices = value.get("choices")
        recognized = False
        if isinstance(choices, list):
            recognized = True
            self.chat = True
            if len(choices) > 1:
                self.issue("multiple_choices")
            selected = [
                item for item in choices if isinstance(item, dict) and item.get("index", 0) == 0
            ]
            if len(selected) > 1:
                self.issue("ambiguous_choice_zero")
            elif len(selected) == 1:
                choice = selected[0]
                self.finish(choice.get("finish_reason"))
                message = choice.get("delta" if self.protocol == "sse" else "message")
                if isinstance(message, dict):
                    self.append(_text_blocks(message.get("content")))
                    if message.get("tool_calls") or message.get("function_call"):
                        self.continuation()
            elif choices:
                self.issue("unsupported_choice_index")
        if isinstance(kind, str) and kind.startswith("response."):
            recognized = True
            self.responses = True
            if kind == "response.output_text.delta":
                delta = value.get("delta")
                if isinstance(delta, str) and self.response_index(value):
                    if self.response_done_text is not None:
                        self.issue("response_delta_after_done")
                    else:
                        self.append(delta)
            elif kind == "response.output_text.done":
                if isinstance(value.get("text"), str) and self.response_index(value):
                    self.reconcile_response_text(value["text"], done=True)
            elif "function_call" in kind or "tool_call" in kind:
                self.continuation()
            item = value.get("item")
            if isinstance(item, dict) and item.get("type") in {
                "function_call",
                "tool_call",
                "computer_call",
                "custom_tool_call",
            }:
                self.continuation()
            response = value.get("response")
            if isinstance(response, dict):
                self.envelope_metadata(response)
                self.response_status(response)
                if "output" in response or "output_text" in response:
                    text = self.response_output(response)
                    if kind in {"response.completed", "response.incomplete", "response.failed"}:
                        self.reconcile_response_text(text)
                    elif text:
                        self.issue("unsupported_nonterminal_response_text")
                        if not self.parts:
                            self.append(text)
            if kind in {"response.completed", "response.incomplete", "response.failed"}:
                self.meta["terminal_event"] = True
                expected = kind.partition(".")[2]
                if self.meta["response_status"] != expected:
                    self.issue("terminal_status_mismatch")
        elif self.protocol == "json" and any(
            key in value for key in ("output", "output_text", "status")
        ):
            recognized = True
            self.responses = True
            self.response_status(value)
            self.append(self.response_output(value))
        if kind == "error":
            recognized = True
            self.issue("api_error")
            code = value.get("code")
            if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
                self.meta["api_error_code"] = code
        if not recognized and not value.get("error") and not isinstance(value.get("usage"), dict):
            self.issue("unsupported_envelope")
        if self.chat and self.responses:
            self.issue("mixed_envelopes")

    def dispatch(self) -> None:
        if not self.data and not self.error_event:
            return
        self.meta["events"] += 1
        self.emit("event")
        is_error = self.error_event
        if is_error:
            self.issue("api_error")
        payload = "\n".join(self.data)
        self.data = []
        self.event_bytes = 0
        self.error_event = False
        if payload.strip() == "[DONE]":
            self.meta["done_marker"] = True
            self.emit("completion_marker")
            return
        try:
            value = json.loads(payload)
        except (ValueError, RecursionError):
            self.issue("invalid_event_json")
            return
        if is_error and isinstance(value, dict):
            code = value.get("code")
            if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
                self.meta["api_error_code"] = code
        self.envelope(value)

    def line(self, raw: bytes) -> None:
        self.meta["raw_lines"] += 1
        self.emit("raw_line")
        self.deadline()
        if self.protocol != "sse":
            return
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.stop("invalid_utf8")
        if self.first_line:
            line = line.removeprefix("\ufeff")
            self.first_line = False
        if not line:
            self.dispatch()
        elif line.startswith(":"):
            self.meta["comments"] += 1
        else:
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                self.event_bytes += len(raw) + 1
                if self.event_bytes > self.max_event:
                    self.stop("event_size_limit")
                self.data.append(value)
            elif field == "event" and value == "error":
                self.error_event = True

    def decoded(self, chunk: bytes) -> None:
        self.meta["decoded_bytes"] += len(chunk)
        if self.meta["decoded_bytes"] > self.max_bytes:
            self.stop("decoded_size_limit")
        if self.protocol == "sse" and self.terminal():
            return
        if self.protocol == "json":
            self.body.extend(chunk)
        self.lines.extend(chunk)
        while self.lines:
            if self.skip_lf:
                if self.lines[0] == 10:
                    del self.lines[0]
                self.skip_lf = False
                if not self.lines:
                    break
            lf, cr = self.lines.find(b"\n"), self.lines.find(b"\r")
            endings = [index for index in (lf, cr) if index >= 0]
            if not endings:
                break
            index = min(endings)
            if index > self.max_line:
                self.stop("line_size_limit")
            raw = bytes(self.lines[:index])
            self.skip_lf = self.lines[index] == 13
            del self.lines[: index + 1]
            self.line(raw)
            if self.protocol == "sse" and self.terminal():
                self.lines.clear()
                return
        if len(self.lines) > self.max_line:
            self.stop("line_size_limit")
        self.deadline()

    def terminal(self) -> bool:
        return bool(self.meta["done_marker"] or self.meta["terminal_event"])

    def result(self) -> CaptureResult:
        if not 200 <= self.meta["http_status"] < 300:
            self.issue("http_status")
        if not self.parts or not "".join(self.parts).strip():
            self.issue("missing_visible_text")
        if self.protocol == "sse" and self.chat and not self.meta["done_marker"]:
            self.issue("missing_done_marker")
        if self.chat and self.meta["finish_reason"] not in {"stop", "end_turn", "completed"}:
            self.issue("missing_successful_finish")
        if self.responses and self.meta["response_status"] != "completed":
            self.issue("missing_completed_response")
        if self.responses and not self.meta["terminal_event"]:
            self.issue("missing_terminal_event")
        if not self.chat and not self.responses:
            self.issue("missing_terminal_envelope")
        self.meta["elapsed_seconds"] = max(0.0, self.clock() - self.started)
        self.emit("finished")
        return CaptureResult(
            "".join(self.parts), not self.issues, tuple(self.issues), self.meta.copy()
        )


def _chunks(stream: Any, read_size: int) -> Iterable[bytes]:
    reader = getattr(stream, "read1", None) or getattr(stream, "read", None)
    if callable(reader):
        while True:
            chunk = reader(read_size)
            if not chunk:
                return
            yield chunk
    else:
        yield from stream


def capture_response(
    stream: Any,
    *,
    status: int,
    content_type: str,
    content_encoding: str = "identity",
    timeout_seconds: float = 1800.0,
    clock: Callable[[], float] = time.monotonic,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    max_response_bytes: int = 16 * 1024 * 1024,
    max_line_bytes: int = 1024 * 1024,
    max_event_bytes: int = 2 * 1024 * 1024,
    max_visible_chars: int = 2 * 1024 * 1024,
) -> CaptureResult:
    """Capture answer text and safe evidence; never return raw protocol bodies.

    A JSON fallback is selected only by an explicit JSON Content-Type. Gzip is
    decoded with a bound on total decompressed bytes. Other encodings/types are
    reported without consuming opaque bytes. Missing effective effort/tier are
    UNAVAILABLE; usage retains supplied numeric token fields without arithmetic.
    Responses SSE supports one visible text item and reconciles its deltas with
    authoritative done/final text; multiple text items are explicitly unsupported.
    """
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("status must be a safe HTTP status integer")
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("timeout_seconds must be finite and nonnegative")
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        for limit in (
            max_response_bytes,
            max_line_bytes,
            max_event_bytes,
            max_visible_chars,
        )
    ):
        raise ValueError("capture limits must be positive integers")
    capture = _Capture(
        status=status,
        content_type=content_type,
        content_encoding=content_encoding,
        timeout_seconds=timeout_seconds,
        clock=clock,
        on_progress=on_progress,
        max_response_bytes=max_response_bytes,
        max_line_bytes=max_line_bytes,
        max_event_bytes=max_event_bytes,
        max_visible_chars=max_visible_chars,
    )
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if capture.encoding == "gzip" else None
    try:
        if capture.protocol == "unsupported":
            capture.stop("unsupported_content_type")
        if capture.encoding == "unsupported":
            capture.stop("unsupported_content_encoding")
        capture.deadline()
        iterator = iter(_chunks(stream, 8192))
        while True:
            capture.deadline()
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            if not isinstance(chunk, bytes):
                capture.stop("invalid_transport_chunk")
            capture.meta["raw_bytes"] += len(chunk)
            capture.emit("raw_bytes")
            capture.deadline()
            if capture.meta["raw_bytes"] > max_response_bytes:
                capture.stop("raw_size_limit")
            if decoder is None:
                capture.decoded(chunk)
            else:
                pending = chunk
                while pending:
                    decoded = decoder.decompress(pending, 65536)
                    pending = decoder.unconsumed_tail
                    capture.decoded(decoded)
                    if decoder.unused_data:
                        capture.stop("unsupported_gzip_trailing_data")
            if (
                capture.protocol == "sse"
                and capture.terminal()
                and (decoder is None or decoder.eof)
            ):
                break
        if decoder is not None and not decoder.eof:
            capture.issue("incomplete_gzip")
        if capture.lines:
            capture.line(bytes(capture.lines))
            capture.lines.clear()
        if capture.protocol == "sse":
            if capture.data or capture.error_event:
                capture.issue("unterminated_sse_event")
                capture.dispatch()
        else:
            try:
                body = json.loads(capture.body.decode("utf-8-sig"))
            except UnicodeDecodeError:
                capture.stop("invalid_utf8")
            except (ValueError, RecursionError):
                capture.stop("invalid_json_body")
            capture.meta["events"] += 1
            capture.emit("event")
            capture.envelope(body)
        capture.deadline()
    except _Stopped:
        pass
    except zlib.error:
        capture.issue("invalid_gzip")
    except Exception:
        # Exception strings can include URLs, credentials, or echoed requests.
        capture.issue("transport_or_callback_error")
    return capture.result()
