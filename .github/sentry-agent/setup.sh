#!/usr/bin/env bash
# open-wearables: runs before the developer agent. Mirrors the backend-test job in
# .github/workflows/ci.yml so the agent can run pytest, ruff and ty like CI does.
set -euo pipefail

# Same services as CI (tests would otherwise spin up testcontainers on every pytest run).
docker run -d --name ow-pg -p 5432:5432 \
  -e POSTGRES_USER=open-wearables -e POSTGRES_PASSWORD=open-wearables \
  -e POSTGRES_DB=open_wearables_test postgres:18 >/dev/null
docker run -d --name ow-redis -p 6379:6379 redis:7 >/dev/null

# uv (it also fetches the Python version pinned in backend/.python-version)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
{ echo "$HOME/.local/bin"; echo "$HOME/.cargo/bin"; } >> "$GITHUB_PATH"

(cd backend && uv sync --group dev --group code-quality --extra otel)

# Same test env as CI, exported to the agent's step
{
  echo "ENV=test"
  echo "TEST_DATABASE_URL=postgresql+psycopg://open-wearables:open-wearables@localhost:5432/open_wearables_test"
  echo "TEST_REDIS_URL=redis://localhost:6379/0"
  echo "SECRET_KEY=test-secret-key-for-ci"
  echo "MASTER_KEY=dGVzdC1tYXN0ZXIta2V5LWZvci10ZXN0aW5nLW9ubHk="
} >> "$GITHUB_ENV"

for _ in $(seq 1 30); do
  docker exec ow-pg pg_isready -U open-wearables >/dev/null 2>&1 && exit 0
  sleep 2
done
echo "::warning::Postgres not ready, the agent may not be able to run DB tests"
