"""Synthetic, in-memory proof for the future review capture helper."""

from __future__ import annotations

import gzip
import io
import json
import unittest

from stream_capture_v2 import capture_response


def event(value: object, spaced: bool = True) -> bytes:
    return (
        "data:" + (" " if spaced else "") + json.dumps(value, ensure_ascii=False) + "\n\n"
    ).encode()


def chat(text: object, finish: str | None = None, **extra: object) -> dict[str, object]:
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}], **extra}


def complete_sse(text: object = "review") -> bytes:
    return event(chat(text), False) + event(chat(None, "stop")) + b"data: [DONE]\n\n"


class CaptureTests(unittest.TestCase):
    def capture(self, body: bytes, **kwargs: object):
        return capture_response(
            io.BytesIO(body), status=200, content_type="text/event-stream", **kwargs
        )

    def test_handshake_and_raw_line_progress_precede_first_event(self):
        observations = []
        result = self.capture(
            b": still waiting\n\n" + complete_sse(), on_progress=observations.append
        )
        self.assertTrue(result.valid_review, result.issues)
        kinds = [item["observation"] for item in observations]
        self.assertEqual(kinds[0], "handshake")
        self.assertLess(kinds.index("raw_bytes"), kinds.index("raw_line"))
        self.assertLess(kinds.index("raw_line"), kinds.index("event"))
        self.assertEqual(result.metadata["comments"], 1)
        self.assertEqual("".join(item["visible_delta"] for item in observations), "review")

    def test_chunked_utf8_multiline_and_crlf(self):
        body = (
            b"\xef\xbb\xbf: comment\r\n"
            b'data:{"choices":\r\n'
            + 'data: [{"index":0,"delta":{"content":"café ☃"}}]}\r\n\r\n'.encode()
            + event(chat(None, "stop"))
            + b"data:[DONE]\r\r"
        )
        result = capture_response(
            (bytes([byte]) for byte in body), status=200, content_type="text/event-stream"
        )
        self.assertTrue(result.valid_review, result.issues)
        self.assertEqual(result.visible_text, "café ☃")
        self.assertEqual(result.metadata["events"], 3)

    def test_json_chat_and_filtered_usage(self):
        usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 999,
            "completion_tokens_details": {"reasoning_tokens": 4, "secret": "hidden"},
            "request": "hidden",
            "input_tokens": True,
        }
        body = {
            "choices": [
                {
                    "message": {"content": [{"type": "text", "text": "answer"}]},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
            "model": "safe-model",
            "reasoning_effort": "xhigh",
            "service_tier": "fast",
        }
        result = capture_response(
            io.BytesIO(json.dumps(body).encode()),
            status=200,
            content_type="application/json; charset=utf-8",
        )
        self.assertTrue(result.valid_review, result.issues)
        self.assertEqual(result.visible_text, "answer")
        self.assertEqual(
            result.metadata["usage"],
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 999,
                "completion_tokens_details": {"reasoning_tokens": 4},
            },
        )
        self.assertEqual(result.metadata["reasoning_effort"], "xhigh")
        self.assertEqual(result.metadata["service_tier"], "fast")

    def test_json_responses_output_and_output_text_are_alternates(self):
        for extra in ({}, {"output_text": "answer"}):
            body = {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "answer"}],
                    }
                ],
                **extra,
            }
            result = capture_response(
                [json.dumps(body).encode()], status=200, content_type="application/problem+json"
            )
            self.assertTrue(result.valid_review, result.issues)
            self.assertEqual(result.visible_text, "answer")

    def test_observed_completed_missing_output_is_not_a_review(self):
        body = {"status": "completed", "usage": {"input_tokens": 4, "output_tokens": 30}}
        result = capture_response(
            [json.dumps(body).encode()], status=200, content_type="application/json"
        )
        self.assertFalse(result.valid_review)
        self.assertIn("missing_visible_text", result.issues)
        self.assertEqual(result.metadata["usage"], body["usage"])
        self.assertEqual(result.metadata["reasoning_effort"], "UNAVAILABLE")
        self.assertEqual(result.metadata["service_tier"], "UNAVAILABLE")

    def test_empty_max_tokens_json_preserves_evidence(self):
        body = {
            "choices": [{"message": {"content": ""}, "finish_reason": "max_tokens"}],
            "usage": {"completion_tokens": 400},
        }
        result = capture_response(
            [json.dumps(body).encode()], status=200, content_type="application/json"
        )
        self.assertFalse(result.valid_review)
        self.assertIn("output_limit", result.issues)
        self.assertIn("missing_visible_text", result.issues)
        self.assertEqual(result.metadata["usage"], {"completion_tokens": 400})

    def test_missing_done_and_length_are_incomplete(self):
        for body, issue in (
            (event(chat("partial", "stop")), "missing_done_marker"),
            (event(chat("partial", "length")) + b"data:[DONE]\n\n", "output_limit"),
        ):
            result = self.capture(body)
            self.assertFalse(result.valid_review)
            self.assertIn(issue, result.issues)
            self.assertEqual(result.visible_text, "partial")

    def test_done_without_successful_finish_is_not_complete(self):
        result = self.capture(event(chat("answer")) + b"data:[DONE]\n\n")
        self.assertFalse(result.valid_review)
        self.assertIn("missing_successful_finish", result.issues)

    def test_response_stream_completed_does_not_duplicate_text(self):
        body = event({"type": "response.output_text.delta", "delta": "answer"})
        body += event({"type": "response.output_text.done", "text": "answer"})
        body += event(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output_text": "answer",
                    "usage": {"output_tokens": 7},
                },
            }
        )
        result = self.capture(body)
        self.assertTrue(result.valid_review, result.issues)
        self.assertEqual(result.visible_text, "answer")
        self.assertFalse(result.metadata["done_marker"])

    def test_response_partial_delta_recovers_done_and_final_suffix(self):
        progress = []
        body = event({"type": "response.output_text.delta", "delta": "Partial "})
        body += event({"type": "response.output_text.done", "text": "Partial complete report"})
        body += event(
            {
                "type": "response.completed",
                "response": {"status": "completed", "output_text": "Partial complete report"},
            }
        )
        result = self.capture(body, on_progress=progress.append)
        self.assertTrue(result.valid_review, result.issues)
        self.assertEqual(result.visible_text, "Partial complete report")
        self.assertEqual(result.metadata["visible_chars"], len(result.visible_text))
        self.assertEqual("".join(item["visible_delta"] for item in progress), result.visible_text)

    def test_response_partial_delta_recovers_indexed_final_message_without_done(self):
        body = event(
            {
                "type": "response.output_text.delta",
                "delta": "Partial ",
                "output_index": 1,
                "content_index": 0,
            }
        )
        body += event(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {"type": "reasoning", "summary": []},
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Partial complete report"}],
                        },
                    ],
                },
            }
        )
        result = self.capture(body)
        self.assertTrue(result.valid_review, result.issues)
        self.assertEqual(result.visible_text, "Partial complete report")

    def test_response_conflicting_authoritative_text_is_invalid(self):
        for done_text, final_text in (
            (None, "different answer"),
            ("answer", "different answer"),
            ("answer", "answer extended after done"),
            ("answer", ""),
        ):
            with self.subTest(done=done_text, final=final_text):
                body = event({"type": "response.output_text.delta", "delta": "answer"})
                if done_text is not None:
                    body += event({"type": "response.output_text.done", "text": done_text})
                body += event(
                    {
                        "type": "response.completed",
                        "response": {"status": "completed", "output_text": final_text},
                    }
                )
                result = self.capture(body)
                self.assertFalse(result.valid_review)
                self.assertIn("response_text_mismatch", result.issues)
                self.assertEqual(result.visible_text, "answer")

    def test_response_multiple_indices_and_final_blocks_are_explicitly_unsupported(self):
        for indices in ((0, 1), (1, 0)):
            with self.subTest(indices=indices):
                body = b"".join(
                    event(
                        {
                            "type": "response.output_text.delta",
                            "delta": text,
                            "output_index": index,
                            "content_index": 0,
                        }
                    )
                    for index, text in zip(indices, ("A", "B"), strict=True)
                )
                body += event(
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {"type": "output_text", "text": "A"},
                                        {"type": "output_text", "text": "B"},
                                    ],
                                }
                            ],
                        },
                    }
                )
                result = self.capture(body)
                self.assertFalse(result.valid_review)
                self.assertIn("unsupported_multiple_response_text_items", result.issues)

    def test_response_delta_after_done_is_invalid_and_not_duplicated(self):
        body = event({"type": "response.output_text.done", "text": "answer"})
        body += event({"type": "response.output_text.delta", "delta": "answer"})
        body += event(
            {
                "type": "response.completed",
                "response": {"status": "completed", "output_text": "answer"},
            }
        )
        result = self.capture(body)
        self.assertFalse(result.valid_review)
        self.assertIn("response_delta_after_done", result.issues)
        self.assertEqual(result.visible_text, "answer")

    def test_response_streamed_index_must_match_structured_final(self):
        for output_index, content_index in ((1, 0), (0, 1)):
            with self.subTest(output_index=output_index, content_index=content_index):
                body = event(
                    {
                        "type": "response.output_text.delta",
                        "delta": "answer",
                        "output_index": output_index,
                        "content_index": content_index,
                    }
                )
                body += event(
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": "answer"}],
                                }
                            ],
                        },
                    }
                )
                result = self.capture(body)
                self.assertFalse(result.valid_review)
                self.assertIn("response_text_index_mismatch", result.issues)

    def test_response_conflicting_output_renderings_are_invalid(self):
        body = event(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output_text": "answer",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "different"}],
                        }
                    ],
                },
            }
        )
        result = self.capture(body)
        self.assertFalse(result.valid_review)
        self.assertIn("response_text_mismatch", result.issues)

    def test_response_status_alone_does_not_complete_an_sse_review(self):
        body = event(
            {
                "type": "response.created",
                "response": {"status": "completed", "output_text": "answer"},
            }
        )
        result = self.capture(body)
        self.assertFalse(result.valid_review)
        self.assertIn("missing_terminal_event", result.issues)
        self.assertEqual(result.visible_text, "answer")

    def test_completion_stops_before_unrelated_trailing_text(self):
        result = self.capture(complete_sse() + event(chat("unrelated answer", "stop")))
        self.assertTrue(result.valid_review, result.issues)
        self.assertEqual(result.visible_text, "review")

    def test_gzip_sse_and_json(self):
        result = self.capture(gzip.compress(complete_sse()), content_encoding="gzip")
        self.assertTrue(result.valid_review, result.issues)
        body = json.dumps({"status": "completed", "output_text": "json answer"}).encode()
        result = capture_response(
            [gzip.compress(body)],
            status=200,
            content_type="application/json",
            content_encoding="gzip",
        )
        self.assertTrue(result.valid_review, result.issues)
        self.assertEqual(result.visible_text, "json answer")

    def test_invalid_and_truncated_gzip(self):
        for body, issue in (
            (b"not gzip", "invalid_gzip"),
            (gzip.compress(complete_sse())[:-4], "incomplete_gzip"),
        ):
            result = self.capture(body, content_encoding="gzip")
            self.assertFalse(result.valid_review)
            self.assertIn(issue, result.issues)

    def test_unsupported_encoding_and_type_do_not_consume_opaque_bytes(self):
        def unread():
            self.fail("unsupported bodies must not be consumed")
            yield b"opaque"

        for content_type, encoding, issue in (
            ("application/json", "br", "unsupported_content_encoding"),
            ("application/octet-stream", "identity", "unsupported_content_type"),
        ):
            result = capture_response(
                unread(), status=200, content_type=content_type, content_encoding=encoding
            )
            self.assertIn(issue, result.issues)
            self.assertEqual(result.metadata["raw_bytes"], 0)

    def test_reasoning_signature_and_continuation_values_do_not_leak(self):
        hidden = "DO_NOT_RETURN_PRIVATE_STATE"
        body = event(
            chat(
                [{"type": "reasoning", "text": hidden}, {"type": "text", "text": "answer"}],
                reasoning_content=hidden,
                thought_signature=hidden,
                continuation_token=hidden,
                private_url="https://" + hidden,
            )
        )
        body += event(chat(None, "stop")) + b"data:[DONE]\n\n"
        progress = []
        result = self.capture(body, on_progress=progress.append)
        self.assertNotIn(hidden, repr(result))
        self.assertNotIn(hidden, repr(progress))
        self.assertEqual(result.visible_text, "answer")
        self.assertIn("native_continuation", result.issues)

    def test_api_errors_return_only_safe_code(self):
        hidden = "PRIVATE_REQUEST_AND_CREDENTIAL"
        body = {
            "error": {"code": "quota_exceeded", "message": hidden, "request": hidden},
            "usage": {"input_tokens": 3},
        }
        result = capture_response(
            [json.dumps(body).encode()], status=429, content_type="application/json"
        )
        self.assertNotIn(hidden, repr(result))
        self.assertIn("api_error", result.issues)
        self.assertIn("http_status", result.issues)
        self.assertEqual(result.metadata["api_error_code"], "quota_exceeded")
        self.assertEqual(result.metadata["usage"], {"input_tokens": 3})

    def test_multiple_choices_are_not_concatenated(self):
        value = {
            "choices": [
                {"index": 0, "delta": {"content": "first"}, "finish_reason": "stop"},
                {"index": 1, "delta": {"content": "second"}, "finish_reason": "stop"},
            ]
        }
        result = self.capture(event(value) + b"data:[DONE]\n\n")
        self.assertFalse(result.valid_review)
        self.assertIn("multiple_choices", result.issues)
        self.assertEqual(result.visible_text, "first")

    def test_deadline_is_not_reset_by_keepalives(self):
        now = [0.0]
        observations = []

        def stream():
            for _ in range(10):
                now[0] += 0.4
                yield b": keepalive\n\n"

        result = capture_response(
            stream(),
            status=200,
            content_type="text/event-stream",
            timeout_seconds=1.0,
            clock=lambda: now[0],
            on_progress=observations.append,
        )
        self.assertIn("deadline_exceeded", result.issues)
        self.assertLessEqual(result.metadata["raw_bytes"], 3 * len(b": keepalive\n\n"))
        self.assertEqual(observations[0]["observation"], "handshake")

    def test_transport_exception_does_not_echo_error_text(self):
        def stream():
            yield event(chat("partial"))
            raise OSError("PRIVATE_URL_OR_CREDENTIAL")

        result = capture_response(stream(), status=200, content_type="text/event-stream")
        self.assertEqual(result.visible_text, "partial")
        self.assertIn("transport_or_callback_error", result.issues)
        self.assertNotIn("PRIVATE_URL_OR_CREDENTIAL", repr(result))

    def test_callback_failure_is_safe_even_at_handshake(self):
        def broken_callback(snapshot):
            raise RuntimeError("PRIVATE_CALLBACK_MATERIAL")

        result = self.capture(complete_sse(), on_progress=broken_callback)
        self.assertEqual(result.visible_text, "review")
        self.assertIn("progress_callback_error", result.issues)
        self.assertNotIn("PRIVATE_CALLBACK_MATERIAL", repr(result))

    def test_limits_bound_capture(self):
        result = self.capture(complete_sse("abcdef"), max_visible_chars=3)
        self.assertEqual(result.visible_text, "abc")
        self.assertIn("visible_text_limit", result.issues)
        result = self.capture(b"data:" + b"x" * 100, max_line_bytes=20)
        self.assertIn("line_size_limit", result.issues)
        result = self.capture(
            gzip.compress(b"x" * 10000), content_encoding="gzip", max_response_bytes=200
        )
        self.assertIn("decoded_size_limit", result.issues)

    def test_error_event_invalid_utf8_and_incomplete_frame(self):
        result = self.capture(b'event:error\ndata: {"code":"failed"}\n\n')
        self.assertIn("api_error", result.issues)
        self.assertEqual(result.metadata["api_error_code"], "failed")
        result = self.capture(b"data: \xff\n\n")
        self.assertIn("invalid_utf8", result.issues)
        result = self.capture(event(chat("partial", "stop")).rstrip(b"\n"))
        self.assertEqual(result.visible_text, "partial")
        self.assertIn("unterminated_sse_event", result.issues)


if __name__ == "__main__":
    unittest.main()
