#!/usr/bin/env bash
set -euo pipefail

APP_DIR_DEFAULT="/opt/ai-gamemaster"
APP_DIR="${APP_DIR:-$APP_DIR_DEFAULT}"
APP_USER="${APP_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:7b-instruct}"
DB_NAME="${DB_NAME:-aigm}"
DB_USER="${DB_USER:-aigm}"
DB_PASSWORD="${DB_PASSWORD:-aigm_password_change_me}"
STREAMLIT_PORT="${STREAMLIT_PORT:-9531}"
HEALTH_PORT="${HEALTH_PORT:-9540}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root (sudo)."
  exit 1
fi

if ! grep -qi "debian" /etc/os-release; then
  echo "This installer is for Debian. Detected:"
  cat /etc/os-release
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "App directory not found: $APP_DIR"
  echo "Clone/copy the repo there first."
  exit 1
fi

echo "Installing OS dependencies..."
apt-get update
apt-get install -y \
  curl \
  ca-certificates \
  git \
  build-essential \
  "$PYTHON_BIN" \
  python3-venv \
  postgresql \
  postgresql-contrib

echo "Installing Ollama..."
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi

echo "Configuring PostgreSQL..."
systemctl enable postgresql
systemctl restart postgresql
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

echo "Setting up Python environment..."
cd "$APP_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install --upgrade "setuptools>=78.1.1" wheel
"$VENV_DIR/bin/python" -m pip install -e ".[dev,ui]"

echo "Writing .env defaults..."
ENV_FILE="$APP_DIR/.env"
touch "$ENV_FILE"
set_env() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|g" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

set_env "AIGM_DATABASE_URL" "postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"
set_env "AIGM_DATABASE_SSLMODE" "require"
set_env "AIGM_DATABASE_CONNECT_TIMEOUT_S" "10"
set_env "AIGM_LLM_PROVIDER" "ollama"
set_env "AIGM_OLLAMA_URL" "http://127.0.0.1:11434"
set_env "AIGM_OLLAMA_MODEL" "$OLLAMA_MODEL"
set_env "AIGM_AUTH_ENFORCE" "false"
set_env "AIGM_STREAMLIT_PORT" "$STREAMLIT_PORT"
set_env "AIGM_HEALTHCHECK_PORT" "$HEALTH_PORT"
set_env "AIGM_HEALTHCHECK_URL" "http://127.0.0.1:${HEALTH_PORT}/health"
set_env "AIGM_LOG_DIR" "$LOG_DIR"
set_env "AIGM_LOG_FILE_MAX_BYTES" "10485760"
set_env "AIGM_LOG_FILE_BACKUP_COUNT" "5"
set_env "AIGM_LOG_DB_BATCH_SIZE" "50"
set_env "AIGM_LOG_DB_FLUSH_INTERVAL_S" "2"

echo "Initializing DB schema if needed..."
"$VENV_DIR/bin/python" -m aigm.db.bootstrap --required-table campaigns --required-table system_logs --required-table bot_configs

echo "Ensuring Ollama service + model..."
systemctl enable ollama
systemctl restart ollama
sleep 2
ollama pull "$OLLAMA_MODEL"

echo "Installing systemd supervisor service..."
mkdir -p "$LOG_DIR"
chown -R "$APP_USER":"$APP_USER" "$LOG_DIR" || true
cat >/etc/systemd/system/aigm-supervisor.service <<EOF
[Unit]
Description=AI GameMaster Unified Supervisor
After=network.target postgresql.service ollama.service

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV_DIR}/bin/python -m aigm.ops.supervisor --streamlit-port ${STREAMLIT_PORT} --health-port ${HEALTH_PORT} --log-dir ${LOG_DIR} --cwd ${APP_DIR}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable aigm-supervisor
systemctl restart aigm-supervisor

echo "Install complete."
