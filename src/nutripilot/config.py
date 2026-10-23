"""Environment-backed application settings with credential-free defaults."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    app_mode: Literal["demo", "docker", "production", "test"] = "demo"
    model_provider: Literal["fake", "openai"] = "fake"
    model_name: str = "qwen-plus"
    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MODEL_API_KEY", "DASHSCOPE_API_KEY"),
    )
    model_fallback_name: str | None = None
    model_timeout_seconds: float = Field(default=25, gt=0, le=120)
    model_max_retries: int = Field(default=2, ge=0, le=5)
    embedding_provider: Literal["hashing", "bge"] = "hashing"
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str | None = None
    rerank_provider: Literal["lexical", "bge"] = "lexical"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_device: str | None = None
    agent_max_steps: int = Field(default=8, ge=1, le=20)
    agent_deadline_seconds: float = Field(default=90, gt=0, le=600)
    agent_token_budget: int = Field(default=24000, ge=1, le=1_000_000)
    run_store_path: str | None = None
    api_access_tokens: dict[str, int] = Field(default_factory=dict)
    api_rate_limit_per_minute: int = Field(default=60, ge=1, le=10000)
    database_url: str = "postgresql+asyncpg://nutripilot:nutripilot@localhost:5432/nutripilot"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "meal_search"
    mcp_enabled: bool = True
    mcp_server_url: str = "http://localhost:8010/mcp"
    mcp_internal_token: str | None = None
    mcp_timeout_seconds: float = Field(default=8, gt=0, le=300)
    web_origin: str = "http://localhost:5173,http://127.0.0.1:5173"

    @model_validator(mode="after")
    def validate_model_credentials(self) -> Settings:
        if self.model_provider == "openai" and not self.model_api_key:
            raise ValueError("MODEL_API_KEY is required when MODEL_PROVIDER=openai")
        if any(len(token) < 32 or user_id < 1 for token, user_id in self.api_access_tokens.items()):
            raise ValueError(
                "API_ACCESS_TOKENS requires 32+ character tokens and positive user IDs"
            )
        if self.app_mode == "production" and not self.api_access_tokens:
            raise ValueError("API_ACCESS_TOKENS is required in production")
        return self

    @property
    def uses_fake_model(self) -> bool:
        return self.model_provider == "fake"

    @property
    def uses_external_infrastructure(self) -> bool:
        return self.app_mode in {"docker", "production"}

    @property
    def web_origins(self) -> list[str]:
        return [origin.strip() for origin in self.web_origin.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
