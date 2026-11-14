"""Settings from environment variables (or .env). See README, "Configuration"."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_", extra="ignore")

    # Models: a cheap one for bulk reading, a strong one for answers.
    answer_model: str = "claude-opus-5-5"
    summary_model: str = "claude-haiku-5-5"
    answer_effort: Effort = "medium"
    summary_effort: Effort = "low"

    # Spend caps (USD). The run cap applies per `ask` or `index --summarize`; the day cap across all runs.
    run_cap_usd: Decimal = Field(default=Decimal("0.25"), gt=0, le=100)
    day_cap_usd: Decimal = Field(default=Decimal("2.00"), gt=0, le=1_000)

    # Loop limits
    max_steps: int = Field(default=10, ge=1, le=50)
    max_output_tokens: int = Field(default=8_000, ge=512, le=64_000)
    min_output_tokens: int = Field(default=512, ge=64, le=8_000)
    summary_concurrency: int = Field(default=4, ge=1, le=32)

    # Storage and indexing
    db_path: str = "data/agent.db"
    max_file_bytes: int = Field(default=200_000, ge=1_000, le=5_000_000)
    chunk_lines: int = Field(default=60, ge=10, le=500)
    repo_map_max_files: int = Field(default=400, ge=10, le=5_000)

    # Anthropic client
    request_timeout_seconds: float = Field(default=120.0, ge=5, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)
