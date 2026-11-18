"""Regenerate offline Python-reference request state-machine vectors; no provider calls."""

import asyncio
import copy
import dataclasses
import json
import time
from pathlib import Path

from identitybench.providers.base import ModelResponse, ProviderCapabilities, Usage
from identitybench.v5.runner import RunConfigV5, _execute_logical_request

GOOD = {"text": "done", "usage": {"input_tokens": 11, "output_tokens": 7}}
TRANSIENT = {"error": "synthetic", "raw": {"error_class": "TRANSIENT_FAILURE"}}


class FakeProvider:
    kind = "replay"
    model = "offline-test"

    def __init__(self, responses, retrieval_response=None):
        self.responses = list(responses)
        self.retrieval_response = retrieval_response

    async def retrieve_response(self, request_id):
        del request_id
        if self.retrieval_response is None:
            return None
        raw = copy.deepcopy(self.retrieval_response)
        raw["usage"] = Usage(**raw.get("usage", {}))
        return ModelResponse(**raw)

    def capabilities(self):
        return ProviderCapabilities(True, True, True, True)

    async def complete(self, request):
        del request
        raw = copy.deepcopy(self.responses.pop(0))
        raw["usage"] = Usage(**raw.get("usage", {}))
        return ModelResponse(**raw)


async def generate():
    cases = []

    async def add(name, responses, config=None, retrieval_response=None):
        cfg = RunConfigV5.model_validate(config or {})
        provider = FakeProvider(responses, retrieval_response)
        output = await _execute_logical_request(
            attempt_id="attempt:test",
            dispatch_round=1,
            reservation_ordinal=(1, 1, 0, "unified", 0),
            logical_request_id="attempt:test:logical:1",
            role="unified",
            agent_id="agent:unified",
            role_provider=provider,
            config=cfg,
            messages=(),
            phase_index=1,
            token_counter=lambda _: 100,
            physical_seq_counter=[0],
            active_intervals_ms=[],
            run_start_ns=time.monotonic_ns(),
        )
        expected = {
            "physical_requests": [r.model_dump(mode="json") for r in output.physical_requests],
            "response": dataclasses.asdict(output.merged_response)
            if output.merged_response
            else None,
            "status": output.infrastructure_status,
            "terminal_cause": output.terminal_cause,
            "context_limit_cause": output.context_limit_cause,
            "completed_generation": output.completed_generation,
            "normalized_decode_status": output.normalized_decode_status,
            "usage_sum": dataclasses.asdict(output.valid_usage_sum),
            "usage_problem_detected": output.usage_problem_detected,
            "provider_error_detected": output.provider_error_detected,
        }
        case = {"name": name, "responses": responses, "config": config or {}, "expected": expected}
        if retrieval_response is not None:
            case["retrieval_response"] = retrieval_response
        cases.append(case)

    await add("normal", [GOOD])
    await add("transient_then_success", [TRANSIENT, GOOD])
    await add("retry_to_recovery", [TRANSIENT] * 3 + [GOOD])
    await add("retry_exhausted", [TRANSIENT] * 3, {"max_recovery_continuations": 0})
    await add(
        "no_retries", [TRANSIENT], {"max_transient_retries": 0, "max_recovery_continuations": 0}
    )
    await add("policy_rejection", [{"error": "synthetic", "raw": {"error_code": "content_filter"}}])
    await add("structured_refusal", [{**GOOD, "refused": True}])
    await add("output_limit", [{**GOOD, "stop_reason": "length"}])
    await add("partial_decode", [{**TRANSIENT, "text": "partial"}])
    await add("ambiguous", [{"error": "synthetic", "raw": {"ambiguous_timeout": True}}])
    await add(
        "ambiguous_retrieved",
        [
            {
                "error": "synthetic",
                "raw": {
                    "error_class": "AMBIGUOUS_FAILURE",
                    "provider_request_id": "provider:retrievable",
                },
            }
        ],
        retrieval_response=GOOD,
    )
    await add(
        "quota_then_success",
        [{"error": "synthetic", "raw": {"error_class": "QUOTA_INTERRUPTION"}}, GOOD],
    )
    await add(
        "quota_exhausted",
        [{"error": "synthetic", "raw": {"error_class": "QUOTA_INTERRUPTION"}}],
        {"max_quota_continuations": 0},
    )
    await add(
        "segment_merge",
        [
            {
                **GOOD,
                "text": "part",
                "stop_reason": "pause_turn",
                "raw": {"continuation_token": "SYNTHETIC_OPAQUE"},
            },
            GOOD,
        ],
        {"segment_continuation_capability": "SUPPORTED"},
    )
    await add("segment_unsupported", [{**GOOD, "stop_reason": "pause_turn"}])
    await add(
        "segment_limit",
        [{**GOOD, "stop_reason": "pause_turn"}],
        {"segment_continuation_capability": "SUPPORTED", "max_segment_continuations": 0},
    )
    for disposition in ["CONFIRMED_LIMIT", "MISCOUNT_OR_FRAMING_FAULT", "UNEXPLAINED_REJECTION"]:
        await add(
            disposition,
            [
                {
                    "error": "synthetic",
                    "raw": {
                        "error_class": "CONTEXT_LIMIT_REJECTION",
                        "context_reconciliation": {"disposition": disposition},
                    },
                }
            ],
        )
    await add(
        "reconciled",
        [
            {
                "error": "synthetic",
                "raw": {
                    "error_class": "CONTEXT_LIMIT_REJECTION",
                    "context_reconciliation": {
                        "disposition": "RECONCILED_RESUMABLE",
                        "reconciled_output_allowance_tokens": 32000,
                    },
                },
            },
            GOOD,
        ],
    )
    await add(
        "native_segment_budget",
        [{**GOOD, "stop_reason": "pause_turn"}, GOOD],
        {
            "segment_continuation_capability": "SUPPORTED",
            "native_effort_policy": "MAXIMUM_VALID_NATIVE_BUDGET",
            "native_numeric_budget_cap_tokens": 10000,
            "native_numeric_budget_min_tokens": 1000,
            "native_visible_output_reserve_tokens": 24,
            "effort_minimum_output_tokens": 1024,
        },
    )
    await add(
        "reconciled_native_segment_budget",
        [
            {
                "error": "synthetic",
                "raw": {
                    "error_class": "CONTEXT_LIMIT_REJECTION",
                    "context_reconciliation": {
                        "disposition": "RECONCILED_RESUMABLE",
                        "reconciled_output_allowance_tokens": 6000,
                    },
                },
            },
            {**GOOD, "text": "part", "stop_reason": "pause_turn"},
            GOOD,
        ],
        {
            "segment_continuation_capability": "SUPPORTED",
            "native_effort_policy": "MAXIMUM_VALID_NATIVE_BUDGET",
            "native_numeric_budget_cap_tokens": 10000,
            "native_numeric_budget_min_tokens": 1000,
            "native_visible_output_reserve_tokens": 24,
            "effort_minimum_output_tokens": 1024,
        },
    )
    await add("decimal_billed", [{**GOOD, "reported_cost_usd": 0.0000025}])
    await add(
        "estimated",
        [GOOD],
        {
            "pricing": {
                "pricing_manifest_sha256": "a" * 64,
                "uncached_input_micros_per_million": 1234567,
                "cached_input_micros_per_million": 7654321,
                "output_micros_per_million": 123,
                "reasoning_micros_per_million": 456,
            }
        },
    )
    await add("other_error", [{"error": "synthetic"}])
    await add("missing_usage", [{"text": "done"}])
    await add(
        "invalid_usage",
        [
            {
                "text": "done",
                "usage": {"input_tokens": 11, "output_tokens": 7, "reasoning_tokens": 8},
            }
        ],
    )
    path = Path(__file__).with_name("request_accounting_python.json")
    path.write_text(json.dumps(cases, sort_keys=True, indent=2) + "\n")
    print(f"wrote {len(cases)} offline request vectors")


if __name__ == "__main__":
    asyncio.run(generate())
