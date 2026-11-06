"""
Provider-neutral LLM interface. Callers import only this module.

`LLM_PROVIDER` selects the adapter. Only the selected provider's settings are read.
"""

from __future__ import annotations

import os
import sys
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx2

from training_season.claude import (
    ANTHROPIC_API_KEY_ENV,
    ClaudeAdapter,
    ClaudeConfig,
    claude_config_from_environment,
    new_anthropic_client,
)
from training_season.cli_entry import run_cli
from training_season.llm_base import (
    ChatTurn,
    LLMAdapter,
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMJob,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMReply,
    LLMRequestRejectedError,
    LLMResponseError,
    LLMUnavailableError,
    LLMUsage,
    config_value_detail,
    environment_value,
)
from training_season.openai_compatible import (
    OPENAI_COMPATIBLE_API_KEY_ENV,
    OpenAICompatibleAdapter,
    OpenAICompatibleConfig,
    new_openai_compatible_client,
    openai_compatible_config_from_environment,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "LLM_PROVIDER_ENV",
    "ChatTurn",
    "LLMAdapter",
    "LLMAuthenticationError",
    "LLMConfig",
    "LLMConfigurationError",
    "LLMError",
    "LLMJob",
    "LLMProvider",
    "LLMRateLimitedError",
    "LLMRefusalError",
    "LLMReply",
    "LLMRequestRejectedError",
    "LLMResponseError",
    "LLMUnavailableError",
    "LLMUsage",
    "llm_config_from_environment",
    "main",
    "new_llm_adapter",
]

LLM_PROVIDER_ENV = "LLM_PROVIDER"


class LLMProvider(StrEnum):
    """Provider selected by `LLM_PROVIDER`."""

    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"


type LLMConfig = ClaudeConfig | OpenAICompatibleConfig


def llm_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> LLMConfig:
    """Read and validate the selected provider's settings without an API call."""
    environment = os.environ if environ is None else environ
    provider = environment_value(
        environment, LLM_PROVIDER_ENV, LLMProvider.OPENAI_COMPATIBLE
    )
    if provider == LLMProvider.ANTHROPIC:
        return claude_config_from_environment(environment)
    if provider == LLMProvider.OPENAI_COMPATIBLE:
        return openai_compatible_config_from_environment(environment)
    keys = (
        environment.get(ANTHROPIC_API_KEY_ENV, "").strip(),
        environment.get(OPENAI_COMPATIBLE_API_KEY_ENV, "").strip(),
    )
    raise LLMConfigurationError(
        "LLM_PROVIDER must be openai_compatible or anthropic. "
        f"{config_value_detail(provider, *keys)}"
    )


def new_llm_adapter(
    config: LLMConfig,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> LLMAdapter:
    """Build the adapter for a provider. Inject a transport only in tests."""
    if isinstance(config, ClaudeConfig):
        return ClaudeAdapter(
            config,
            factory=lambda claude_config: new_anthropic_client(
                claude_config,
                http_client=(
                    None if transport is None else httpx2.Client(transport=transport)
                ),
            ),
        )
    return OpenAICompatibleAdapter(
        config,
        factory=lambda openai_config: new_openai_compatible_client(
            openai_config, transport=transport
        ),
    )


def _print_llm_models(_arguments: tuple[str, ...]) -> int:
    try:
        config = llm_config_from_environment()
    except LLMConfigurationError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    for job in LLMJob:
        sys.stdout.write(f"{_llm_job_line(config, job)}\n")
    return 0


def _llm_job_line(config: LLMConfig, job: LLMJob) -> str:
    if isinstance(config, ClaudeConfig):
        job_config = config.for_job(job)
        effort = "none" if job_config.effort is None else job_config.effort
        return (
            f"{job} provider={LLMProvider.ANTHROPIC} "
            f"model={job_config.model} effort={effort}"
        )
    return (
        f"{job} provider={LLMProvider.OPENAI_COMPATIBLE} "
        f"model={config.for_job(job).model}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Check local LLM configuration without making an API call."""
    return run_cli(argv, "training-season-llm check", {("check",)}, _print_llm_models)


if __name__ == "__main__":
    raise SystemExit(main())
