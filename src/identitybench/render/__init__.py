"""Prompt rendering: prompt variants, the context-mode case message and few-shot selection."""

from identitybench.render.fewshot import FewShotBudgetError, select_fewshot
from identitybench.render.prompt import (
    PROMPT_VARIANTS,
    TEMPLATE_VERSION,
    build_request,
    build_system,
    prompt_sha256,
)
from identitybench.render.variants import (
    PromptVariant,
    VariantError,
    default_prompts_dir,
    load_variant,
)
from identitybench.render.view import parse_view, render_view

__all__ = [
    "PROMPT_VARIANTS",
    "TEMPLATE_VERSION",
    "FewShotBudgetError",
    "PromptVariant",
    "VariantError",
    "build_request",
    "build_system",
    "default_prompts_dir",
    "load_variant",
    "parse_view",
    "prompt_sha256",
    "render_view",
    "select_fewshot",
]
