import os

# Set required env vars before any pr_warden import so Settings loads without errors
os.environ.setdefault("GITHUB_APP_ID", "99999")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret-32chars-xxxxxxxxxxxx")
# sqlite (aiosqlite is always installed) so importing pr_warden.main — which
# eagerly builds the async engine — works without a Postgres driver in tests.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("STATS_BEARER_TOKEN", "test-stats-token")
