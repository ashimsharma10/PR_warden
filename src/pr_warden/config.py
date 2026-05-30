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

    # Cheap model by default; the summarizer will later be split into several
    # smaller agents that can each pick their own model.
    summarizer_model: str = "claude-3-5-haiku-latest"

    summarizer_max_cost_usd: float = 0.10
    daily_cost_limit_usd: float = 5.00

    # ── Tool-using review agent (off by default) ────────────────────────────
    # Comma-separated "owner/name" allowlist. Empty → the agent never runs, so
    # deploying this code changes nothing in prod until a repo is flipped on.
    agent_review_repos: str = ""
    agent_model: str = "claude-sonnet-4-6"
    # Wall-clock cap for a single agent run on the live path. The loop also
    # bounds tokens and tool calls internally; this guards against a slow run
    # holding up the webhook handler.
    agent_timeout_s: float = 90.0

    stats_bearer_token: str = ""
    bot_owner_username: str = ""

    def agent_enabled_for(self, repo: str) -> bool:
        """True if the review agent is allowlisted for `repo` ("owner/name")."""
        allowed = {r.strip() for r in self.agent_review_repos.split(",") if r.strip()}
        return repo in allowed

    def private_key(self) -> str:
        path = Path(self.github_app_private_key_path)
        if not path.exists():
            return ""
        return path.read_text()


settings = Settings()
