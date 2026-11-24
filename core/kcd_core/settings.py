"""All configuration comes from environment variables (12-factor). `Settings.validate_for_env` refuses unsafe
combinations at start-up, because a clinical service that boots in a degraded, silent mode is worse than one that
does not boot."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KCD_", extra="ignore")

    env: Literal["dev", "test", "staging", "prod"] = "dev"
    database_url: str = "postgresql://kcd:kcd_dev@localhost:5432/kcd"
    nats_url: str = "nats://localhost:4222"
    pg_pool_max: int = 10
    pg_options: str = ""  # extra libpq options, e.g. "-c search_path=kcd" (tests use a throwaway schema)
    migrations_dir: Path = Path("infra/migrations")
    migrate_on_start: bool = False

    catalog_path: Path = Path("contracts/policy_catalog.json")
    manifests_dir: Path = Path("seats/manifests")
    keys_dir: Path = Path("keys")

    bus_max_attempts: int = 5
    bus_replicas: int = 1  # JetStream stream replicas: 1 for a single node, 3 in a real cluster

    default_unit: str = "uro_oncology"
    escalation_interval_s: float = 2.0
    escalation_lease_s: float = 30.0

    pager: Literal["log", "webhook"] = "log"
    pager_url: str | None = None
    pager_secret: str | None = None

    http_host: str = "0.0.0.0"  # noqa: S104 - inside a container
    http_port: int = 8000

    # control plane API auth (OIDC). Unset => every control-plane call is refused (fail closed).
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    dev_token: str | None = Field(default=None, description="Static bearer token; honoured only when env=dev")

    llm_base_url: str | None = None  # OpenAI-compatible endpoint (vLLM/Ollama) for the Chief of Staff draft step
    llm_model: str | None = None
    llm_api_key: str | None = None

    def validate_for_env(self) -> None:
        problems: list[str] = []
        if self.pager == "webhook" and not (self.pager_url and self.pager_secret):
            problems.append("KCD_PAGER=webhook needs KCD_PAGER_URL and KCD_PAGER_SECRET")
        if self.env in ("staging", "prod"):
            if self.pager == "log":
                problems.append("KCD_PAGER=log reaches nobody: use webhook outside dev/test")
            if self.dev_token:
                problems.append("KCD_DEV_TOKEN must not be set outside dev/test")
            if "kcd_dev" in self.database_url:
                problems.append("the default dev database password is in use")
            if self.bus_replicas < 3 and self.env == "prod":
                problems.append("prod JetStream needs KCD_BUS_REPLICAS=3")
        if problems:
            raise SystemExit("refusing to start:\n  - " + "\n  - ".join(problems))
