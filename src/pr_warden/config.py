from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    github_app_id: int = 0
    github_app_private_key_path: str = "./pr_warden.pem"
    github_webhook_secret: str = ""

    database_url: str = "postgresql+asyncpg://pr_warden:localdev@localhost:5432/pr_warden"

    anthropic_api_key: str = ""

    daily_cost_limit_usd: float = 5.00

    stats_bearer_token: str = ""
    bot_owner_username: str = ""

    def private_key(self) -> str:
        path = Path(self.github_app_private_key_path)
        if not path.exists():
            return ""
        return path.read_text()


settings = Settings()
