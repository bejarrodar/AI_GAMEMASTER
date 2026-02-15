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
COMPONENTS="${COMPONENTS:-all}"                 # all | bot | web | llm | db | comma-list e.g. bot,web
INSTALL_LOCAL_POSTGRES="${INSTALL_LOCAL_POSTGRES:-true}"
INSTALL_LOCAL_OLLAMA="${INSTALL_LOCAL_OLLAMA:-true}"
RUN_DB_BOOTSTRAP="${RUN_DB_BOOTSTRAP:-true}"
INSTALL_SERVICE="${INSTALL_SERVICE:-true}"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root (sudo) so system packages/services can be installed."
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "App directory not found: $APP_DIR"
  echo "Clone/copy the repo there first."
  exit 1
fi

source /etc/os-release
DISTRO_ID="${ID:-unknown}"

has_component() {
  local needle="$1"
  local normalized
  normalized="$(echo "$COMPONENTS" | tr '[:upper:]' '[:lower:]')"
  if [[ "$normalized" == "all" ]]; then
    return 0
  fi
  [[ ",$normalized," == *",$needle,"* ]]
}

need_python_stack=false
if has_component "bot" || has_component "web"; then
  need_python_stack=true
fi

echo "Installer config:"
echo "  DISTRO=$DISTRO_ID"
echo "  COMPONENTS=$COMPONENTS"
echo "  INSTALL_LOCAL_POSTGRES=$INSTALL_LOCAL_POSTGRES"
echo "  INSTALL_LOCAL_OLLAMA=$INSTALL_LOCAL_OLLAMA"
echo "  RUN_DB_BOOTSTRAP=$RUN_DB_BOOTSTRAP"
echo "  INSTALL_SERVICE=$INSTALL_SERVICE"

install_base_packages() {
  if [[ "$DISTRO_ID" == "ubuntu" || "$DISTRO_ID" == "debian" ]]; then
    apt-get update
    apt-get install -y curl ca-certificates git build-essential "$PYTHON_BIN" python3-venv
  elif [[ "$DISTRO_ID" == "amzn" ]]; then
    dnf -y update
    dnf -y install curl ca-certificates git gcc gcc-c++ make "$PYTHON_BIN" python3-pip
  else
    echo "Unsupported distro: $DISTRO_ID"
    exit 1
  fi
}

install_postgres_packages() {
  if [[ "$DISTRO_ID" == "ubuntu" || "$DISTRO_ID" == "debian" ]]; then
    apt-get install -y postgresql postgresql-contrib
  elif [[ "$DISTRO_ID" == "amzn" ]]; then
    dnf -y install postgresql15 postgresql15-server postgresql15-contrib
  fi
}

start_postgres() {
  if [[ "$DISTRO_ID" == "amzn" ]]; then
    if [[ ! -d /var/lib/pgsql/data/base && ! -d /var/lib/pgsql/15/data/base ]]; then
      if command -v postgresql-setup >/dev/null 2>&1; then
        postgresql-setup --initdb || true
      fi
      if command -v /usr/pgsql-15/bin/postgresql-15-setup >/dev/null 2>&1; then
        /usr/pgsql-15/bin/postgresql-15-setup initdb || true
      fi
    fi
    systemctl enable postgresql || systemctl enable postgresql-15 || true
    systemctl restart postgresql || systemctl restart postgresql-15
  else
    systemctl enable postgresql || true
    systemctl restart postgresql
  fi
}

echo "Installing base dependencies..."
install_base_packages

if [[ "$INSTALL_LOCAL_POSTGRES" == "true" ]]; then
  echo "Installing and configuring local PostgreSQL..."
  install_postgres_packages
  start_postgres
  sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';"
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
else
  echo "Skipping local PostgreSQL install/config (INSTALL_LOCAL_POSTGRES=false)."
fi

if [[ "$INSTALL_LOCAL_OLLAMA" == "true" ]]; then
  echo "Installing/validating local Ollama..."
  if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  systemctl enable ollama || true
  systemctl restart ollama
  sleep 2
  ollama pull "$OLLAMA_MODEL"
else
  echo "Skipping local Ollama install/config (INSTALL_LOCAL_OLLAMA=false)."
fi

if [[ "$need_python_stack" == "true" ]]; then
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

  if [[ "$INSTALL_LOCAL_POSTGRES" == "true" ]]; then
    set_env "AIGM_DATABASE_URL" "postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"
  fi
  set_env "AIGM_DATABASE_SSLMODE" "require"
  set_env "AIGM_DATABASE_CONNECT_TIMEOUT_S" "10"
  if [[ "$INSTALL_LOCAL_OLLAMA" == "true" ]]; then
    set_env "AIGM_LLM_PROVIDER" "ollama"
    set_env "AIGM_OLLAMA_URL" "http://127.0.0.1:11434"
    set_env "AIGM_OLLAMA_MODEL" "$OLLAMA_MODEL"
  fi
  set_env "AIGM_AUTH_ENFORCE" "false"
  set_env "AIGM_STREAMLIT_PORT" "$STREAMLIT_PORT"
  set_env "AIGM_HEALTHCHECK_PORT" "$HEALTH_PORT"
  set_env "AIGM_HEALTHCHECK_URL" "http://127.0.0.1:${HEALTH_PORT}/health"
  set_env "AIGM_LOG_DIR" "$LOG_DIR"
  set_env "AIGM_LOG_FILE_MAX_BYTES" "10485760"
  set_env "AIGM_LOG_FILE_BACKUP_COUNT" "5"
  set_env "AIGM_LOG_DB_BATCH_SIZE" "50"
  set_env "AIGM_LOG_DB_FLUSH_INTERVAL_S" "2"

  if [[ "$RUN_DB_BOOTSTRAP" == "true" ]]; then
    echo "Running DB bootstrap/migration checks..."
    "$VENV_DIR/bin/python" -m aigm.db.bootstrap
    "$VENV_DIR/bin/python" - <<'PY'
from aigm.adapters.llm import LLMAdapter
from aigm.db.session import SessionLocal
from aigm.services.game_service import GameService

service = GameService(LLMAdapter())
with SessionLocal() as db:
    service.seed_default_auth(db)
    service.seed_default_agency_rules(db)
    service.seed_default_gameplay_knowledge(db)
print("[install] backend defaults validated")
PY
  fi

  mkdir -p "$LOG_DIR"
  chown -R "$APP_USER":"$APP_USER" "$LOG_DIR" || true
fi

if [[ "$INSTALL_SERVICE" == "true" && "$need_python_stack" == "true" ]]; then
  echo "Installing systemd service(s) for components: $COMPONENTS"
  if [[ "$COMPONENTS" == "all" ]]; then
    cat >/etc/systemd/system/aigm-supervisor.service <<EOF
[Unit]
Description=AI GameMaster Unified Supervisor
After=network.target

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
  else
    if has_component "bot"; then
      cat >/etc/systemd/system/aigm-bot-manager.service <<EOF
[Unit]
Description=AI GameMaster Bot Manager
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV_DIR}/bin/python -m aigm.ops.bot_manager --cwd ${APP_DIR}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
      systemctl daemon-reload
      systemctl enable aigm-bot-manager
      systemctl restart aigm-bot-manager
    fi
    if has_component "web"; then
      cat >/etc/systemd/system/aigm-web.service <<EOF
[Unit]
Description=AI GameMaster Streamlit Web
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV_DIR}/bin/python -m streamlit run streamlit_app.py --server.port ${STREAMLIT_PORT} --server.headless true
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
      systemctl daemon-reload
      systemctl enable aigm-web
      systemctl restart aigm-web
    fi
  fi
fi

echo "Install complete."
if [[ "$COMPONENTS" == "all" ]]; then
  echo "Check status: systemctl status aigm-supervisor"
elif has_component "bot"; then
  echo "Check status: systemctl status aigm-bot-manager"
fi
if has_component "web"; then
  echo "Check status: systemctl status aigm-web"
fi
if [[ "$INSTALL_LOCAL_OLLAMA" == "true" ]]; then
  echo "Check Ollama: curl http://127.0.0.1:11434/api/tags"
fi
