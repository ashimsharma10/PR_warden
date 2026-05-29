FROM python:3.12-slim

WORKDIR /app

# Install gitleaks (pinned version for reproducibility)
ARG GITLEAKS_VERSION=8.18.4
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -sSfL \
       "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
       | tar -xz -C /usr/local/bin gitleaks \
    && gitleaks version \
    && apt-get purge -y curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml .
RUN uv pip install --system -e .

COPY . .

CMD ["uvicorn", "pr_warden.main:app", "--host", "0.0.0.0", "--port", "8000"]
