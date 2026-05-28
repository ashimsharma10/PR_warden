import os

# Set required env vars before any pr_warden import so Settings loads without errors
os.environ.setdefault("GITHUB_APP_ID", "99999")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret-32chars-xxxxxxxxxxxx")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://pr_warden:localdev@localhost:5432/pr_warden")
os.environ.setdefault("STATS_BEARER_TOKEN", "test-stats-token")
os.environ.setdefault("BOT_OWNER_USERNAME", "testowner")
