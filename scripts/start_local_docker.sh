#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker/local/docker-compose.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/env.local}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env.local. Copy .env.example to env.local and fill in the required values."
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

SITE_NAME="${SITE_NAME:-tap_lms.localhost}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-15}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
BUSINESS_THEME_REPO="${BUSINESS_THEME_REPO:-https://github.com/Midocean-Technologies/business_theme_v14.git}"

FRAPPE_PYTHON_VERSION="${FRAPPE_PYTHON_VERSION:-}"
if [[ -z "$FRAPPE_PYTHON_VERSION" ]]; then
  case "$FRAPPE_BRANCH" in
    v14*|version-14*)
      FRAPPE_PYTHON_VERSION="3.10.20"
      ;;
  esac
fi

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build postgres redis-cache redis-queue dev
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T -u root dev chown -R frappe:frappe /home/frappe/frappe-bench

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev bash -lc '
set -euo pipefail

SITE_NAME="${SITE_NAME:-tap_lms.localhost}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-15}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
BUSINESS_THEME_REPO="${BUSINESS_THEME_REPO:-https://github.com/Midocean-Technologies/business_theme_v14.git}"
FRAPPE_PYTHON_VERSION="${FRAPPE_PYTHON_VERSION:-}"

if [[ -n "$FRAPPE_PYTHON_VERSION" ]]; then
  pyenv install -s "$FRAPPE_PYTHON_VERSION"
  export PYENV_VERSION="$FRAPPE_PYTHON_VERSION"
fi

if [[ ! -d /home/frappe/frappe-bench/apps/frappe ]]; then
  if [[ -n "$(find /home/frappe/frappe-bench -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "/home/frappe/frappe-bench is not empty but Frappe is missing."
    echo "If this is a broken local setup, reset it with: docker compose --env-file .env -f docker/local/docker-compose.yml down -v"
    exit 1
  fi
  cd /home/frappe
  bench init \
    --frappe-branch "$FRAPPE_BRANCH" \
    --skip-redis-config-generation \
    --ignore-exist \
    frappe-bench
fi

cd /home/frappe/frappe-bench

if [[ "$FRAPPE_BRANCH" == v14* || "$FRAPPE_BRANCH" == version-14* ]]; then
  python - <<\PY
from pathlib import Path

path = Path("apps/frappe/frappe/database/postgres/setup_db.py")
text = path.read_text()
quote = chr(39)

main_create_db = "root_conn.sql(f\"CREATE DATABASE `{frappe.conf.db_name}`\")"
main_create_user = (
    "root_conn.sql(f\"CREATE user {frappe.conf.db_name} password "
    + quote
    + "{frappe.conf.db_password}"
    + quote
    + "\")"
)
main_replacement = (
    main_create_user
    + "\n\t"
    + "root_conn.sql(f\"CREATE DATABASE `{frappe.conf.db_name}` OWNER {frappe.conf.db_name}\")"
)

help_create_db = "root_conn.sql(f\"CREATE DATABASE `{help_db_name}`\")"
help_create_user = (
    "root_conn.sql(f\"CREATE user {help_db_name} password "
    + quote
    + "{help_db_name}"
    + quote
    + "\")"
)
help_replacement = (
    help_create_user
    + "\n\t"
    + "root_conn.sql(f\"CREATE DATABASE `{help_db_name}` OWNER {help_db_name}\")"
)

updated = text.replace(
    main_create_db + "\n\t" + main_create_user,
    main_replacement,
).replace(
    help_create_db + "\n\t" + help_create_user,
    help_replacement,
)

if updated != text:
    path.write_text(updated)
PY
fi

bench set-config -g db_host postgres
bench set-config -g db_port 5432
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
bench set-config -g socketio_port 9000

if [[ ! -e apps/tap_lms ]]; then
  ln -s /workspace/tap_lms apps/tap_lms
fi

if [[ ! -L apps/tap_lms ]]; then
  echo "apps/tap_lms exists but is not a symlink to /workspace/tap_lms."
  echo "Move or remove it before rerunning setup if you want live local code mounted."
  exit 1
fi

./env/bin/python -m pip install -q "setuptools<81"

mkdir -p sites
if [[ ! -f sites/apps.txt ]]; then
  printf "frappe\n" > sites/apps.txt
fi

grep -vx "frappetap_lms" sites/apps.txt > sites/apps.txt.tmp || true
mv sites/apps.txt.tmp sites/apps.txt

if ! grep -qx "frappe" sites/apps.txt; then
  printf "frappe\n%s" "$(cat sites/apps.txt)" > sites/apps.txt.tmp
  mv sites/apps.txt.tmp sites/apps.txt
fi

if ! grep -qx "tap_lms" sites/apps.txt; then
  printf "tap_lms\n" >> sites/apps.txt
fi

./env/bin/python -m pip install -q -e apps/tap_lms

if [[ ! -d apps/business_theme_v14 ]]; then
  bench get-app "$BUSINESS_THEME_REPO"
fi

grep -vx "frappetap_lms" sites/apps.txt > sites/apps.txt.tmp || true
mv sites/apps.txt.tmp sites/apps.txt

if [[ ! -d "sites/$SITE_NAME" ]]; then
  bench new-site "$SITE_NAME" \
    --db-type postgres \
    --db-host postgres \
    --db-port 5432 \
    --db-root-username "$POSTGRES_USER" \
    --db-root-password "$POSTGRES_PASSWORD" \
    --admin-password "$ADMIN_PASSWORD" \
    --install-app tap_lms
else
  bench --site "$SITE_NAME" migrate
fi

bench build --app tap_lms

if ! bench --site "$SITE_NAME" list-apps | grep -qx "business_theme_v14"; then
  bench --site "$SITE_NAME" install-app business_theme_v14
fi

bench --site "$SITE_NAME" set-config developer_mode 1
bench --site "$SITE_NAME" set-config host_name "http://tap_lms.localhost:${WEB_PORT:-8000}"

set_single_value() {
  local doctype="$1"
  local field="$2"
  local value="${3:-}"
  local args
  args="$(python -c "import json,sys; print(json.dumps([sys.argv[1], sys.argv[2], sys.argv[3]]))" "$doctype" "$field" "$value")"
  bench --site "$SITE_NAME" execute frappe.db.set_single_value --args "$args"
}

if [[ -n "${RABBITMQ_HOST:-}" ]]; then
  set_single_value "RabbitMQ Settings" host "${RABBITMQ_HOST:-}"
  set_single_value "RabbitMQ Settings" port "${RABBITMQ_PORT:-5671}"
  set_single_value "RabbitMQ Settings" virtual_host "${RABBITMQ_VIRTUAL_HOST:-}"
  set_single_value "RabbitMQ Settings" username "${RABBITMQ_USERNAME:-}"
  set_single_value "RabbitMQ Settings" password "${RABBITMQ_PASSWORD:-}"
  set_single_value "RabbitMQ Settings" submission_queue "${RABBITMQ_SUBMISSION_QUEUE:-}"
  set_single_value "RabbitMQ Settings" plagiarism_results_queue "${RABBITMQ_PLAGIARISM_RESULTS_QUEUE:-}"
  set_single_value "RabbitMQ Settings" feedback_results_queue "${RABBITMQ_FEEDBACK_RESULTS_QUEUE:-}"
fi

set_single_value "GCS Settings" enabled "${GCS_ENABLED:-0}"
set_single_value "GCS Settings" bucket_name "${GCS_BUCKET_NAME:-}"
set_single_value "GCS Settings" project_id "${GCS_PROJECT_ID:-}"
set_single_value "GCS Settings" credentials_json "${GCS_CREDENTIALS_JSON:-{}}"

set_single_value "ElevenLabs Settings" enabled "${ELEVENLABS_ENABLED:-0}"
set_single_value "ElevenLabs Settings" api_key "${ELEVENLABS_API_KEY:-disabled-local-placeholder}"

set_single_value "VoiceAgentSettings" enabled "${VOICE_AGENT_ENABLED:-0}"
set_single_value "VoiceAgentSettings" service_url "${VOICE_AGENT_SERVICE_URL:-}"
set_single_value "VoiceAgentSettings" client_id "${VOICE_AGENT_CLIENT_ID:-}"
set_single_value "VoiceAgentSettings" client_secret "${VOICE_AGENT_CLIENT_SECRET:-}"
set_single_value "VoiceAgentSettings" default_contact_group_id "${VOICE_AGENT_DEFAULT_CONTACT_GROUP_ID:-}"
set_single_value "VoiceAgentSettings" agent_id "${VOICE_AGENT_AGENT_ID:-}"
set_single_value "VoiceAgentSettings" auth_token_cache_ttl "${VOICE_AGENT_AUTH_TOKEN_CACHE_TTL:-3600}"

bench --site "$SITE_NAME" clear-cache
'

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T dev bash -lc '
set -euo pipefail

cd /home/frappe/frappe-bench

if ! pgrep -f "frappe.utils.bench_helper frappe serve --port 8000" >/dev/null; then
  nohup bench start > logs/local-bench-start.log 2>&1 </dev/null &
  sleep 2
fi
'

cat <<EOF

Local tap_lms setup is ready and running.

URL: http://tap_lms.localhost:${WEB_PORT:-8000}
Admin user: Administrator
Admin password: ${ADMIN_PASSWORD}

Restart after backend code changes:
  ./scripts/restart_local_docker.sh

Rebuild assets first when JS/CSS changes:
  ./scripts/restart_local_docker.sh --build

View runtime logs:
  docker compose --env-file env.local -f docker/local/docker-compose.yml exec dev bash -lc "tail -f /home/frappe/frappe-bench/logs/local-bench-start.log"

EOF
