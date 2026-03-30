from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.exists() else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # MongoDB
    mongodb_uri: str = "mongodb://localhost:27017/cogniq?replicaSet=rs0"
    mongodb_db_name: str = "cogniq"

    # Auth
    jwt_secret: str = "change-me"
    jwt_expiry_minutes: int = 60
    jwt_refresh_expiry_days: int = 30
    invite_codes: str = "COGNIQ01"

    # Claude
    anthropic_api_key: str = ""

    # Linear
    linear_api_key: str = ""
    linear_webhook_secret: str = ""

    # GitHub
    github_token: str = ""
    github_webhook_secret: str = ""

    # Slack
    slack_bot_token: str = ""
    slack_channel: str = "#cogniq-updates"

    # Agent limits
    plan_timeout_minutes: int = 10
    build_timeout_minutes: int = 30
    build_max_cost_usd: float = 5.00
    build_max_turns: int = 50

    # Worker
    worker_id: str = ""
    worker_poll_interval_seconds: float = 2.0
    worker_heartbeat_interval_seconds: float = 30.0
    worker_stale_timeout_seconds: int = 120

    # Workspace
    work_base_dir: str = "/app/workspaces"
    workspace_auto_clone: bool = True

    # App
    log_level: str = "INFO"
    environment: str = "development"

    @property
    def invite_code_list(self) -> list[str]:
        return [c.strip() for c in self.invite_codes.split(",") if c.strip()]


settings = Settings()
