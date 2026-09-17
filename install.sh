#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${BRETTER_REPO_URL:-https://github.com/csufpsudocromis/Bretter-IMG.git}"
BRANCH="${BRETTER_BRANCH:-main}"
INSTALL_DIR="${BRETTER_INSTALL_DIR:-${HOME}/bretter-img}"
INSTALL_SYSTEM_DEPS=1
START_SERVICES=1
ADMIN_USER="${BRETTER_ADMIN_USER:-}"
ADMIN_PASSWORD="${BRETTER_ADMIN_PASSWORD:-}"

usage() {
  cat <<'EOF'
Bretter-IMG installer

Usage:
  ./install.sh [options]
  curl -fsSL https://raw.githubusercontent.com/csufpsudocromis/Bretter-IMG/main/install.sh | bash

Options:
  --dir PATH             Install or update the repo at PATH.
  --repo URL             Git repository URL to clone.
  --branch NAME          Git branch to install.
  --admin-user NAME      Create an initial admin user.
  --admin-password PASS  Password for the initial admin user.
  --skip-system-deps     Do not install missing OS packages.
  --no-start             Install dependencies but do not start services.
  -h, --help             Show this help.

Environment:
  BRETTER_INSTALL_DIR, BRETTER_REPO_URL, BRETTER_BRANCH
  BRETTER_ADMIN_USER, BRETTER_ADMIN_PASSWORD
EOF
}

log() {
  printf '[bretter-img] %s\n' "$*" >&2
}

fail() {
  printf '[bretter-img] ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)
      INSTALL_DIR="${2:-}"
      [[ -n "${INSTALL_DIR}" ]] || fail "--dir requires a path"
      shift 2
      ;;
    --repo)
      REPO_URL="${2:-}"
      [[ -n "${REPO_URL}" ]] || fail "--repo requires a URL"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      [[ -n "${BRANCH}" ]] || fail "--branch requires a branch name"
      shift 2
      ;;
    --admin-user)
      ADMIN_USER="${2:-}"
      [[ -n "${ADMIN_USER}" ]] || fail "--admin-user requires a username"
      shift 2
      ;;
    --admin-password)
      ADMIN_PASSWORD="${2:-}"
      [[ -n "${ADMIN_PASSWORD}" ]] || fail "--admin-password requires a password"
      shift 2
      ;;
    --skip-system-deps)
      INSTALL_SYSTEM_DEPS=0
      shift
      ;;
    --no-start)
      START_SERVICES=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

python_venv_works() {
  have_cmd python3 || return 1
  local probe_dir
  probe_dir="$(mktemp -d)"
  if python3 -m venv "${probe_dir}/venv" >/dev/null 2>&1; then
    rm -rf "${probe_dir}"
    return 0
  fi
  rm -rf "${probe_dir}"
  return 1
}

sudo_if_needed() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif have_cmd sudo; then
    sudo "$@"
  else
    fail "sudo is required to install missing system packages"
  fi
}

install_system_deps() {
  local missing=()
  for cmd in git python3 npm node openssl curl; do
    have_cmd "${cmd}" || missing+=("${cmd}")
  done
  if have_cmd python3 && ! python_venv_works; then
    missing+=("python3-venv")
  fi

  if [[ ${#missing[@]} -eq 0 ]]; then
    return
  fi

  if [[ "${INSTALL_SYSTEM_DEPS}" -eq 0 ]]; then
    fail "Missing required commands: ${missing[*]}"
  fi

  log "Installing missing system packages: ${missing[*]}"
  if have_cmd apt-get; then
    sudo_if_needed apt-get update
    sudo_if_needed apt-get install -y git python3 python3-venv python3-pip nodejs npm openssl curl ca-certificates
  elif have_cmd dnf; then
    sudo_if_needed dnf install -y git python3 python3-pip nodejs npm openssl curl ca-certificates
  elif have_cmd yum; then
    sudo_if_needed yum install -y git python3 python3-pip nodejs npm openssl curl ca-certificates
  elif have_cmd zypper; then
    sudo_if_needed zypper install -y git python3 python3-pip nodejs npm openssl curl ca-certificates
  elif have_cmd pacman; then
    sudo_if_needed pacman -Sy --needed --noconfirm git python python-pip nodejs npm openssl curl ca-certificates
  elif have_cmd brew; then
    brew install git python node openssl curl
  else
    fail "No supported package manager found. Install these commands and rerun: ${missing[*]}"
  fi
}

assert_node_version() {
  local major
  major="$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)"
  if [[ "${major}" -lt 18 ]]; then
    fail "Node.js 18 or newer is required. Found: $(node --version 2>/dev/null || echo unknown)"
  fi
}

current_checkout_dir() {
  local dir
  dir="$(pwd -P)"
  if [[ -d "${dir}/.git" && -f "${dir}/frontend/package.json" && -f "${dir}/server/requirements.txt" ]]; then
    local remote
    remote="$(git -C "${dir}" remote get-url origin 2>/dev/null || true)"
    if [[ "${remote}" == *"Bretter-IMG"* || "${remote}" == "${REPO_URL}" ]]; then
      printf '%s\n' "${dir}"
      return 0
    fi
  fi
  return 1
}

prepare_repo() {
  local app_dir
  if app_dir="$(current_checkout_dir)"; then
    log "Using existing checkout: ${app_dir}"
    git -C "${app_dir}" fetch origin --prune >&2
    git -C "${app_dir}" checkout "${BRANCH}" >&2
    git -C "${app_dir}" pull --ff-only origin "${BRANCH}" >&2
    printf '%s\n' "${app_dir}"
    return
  fi

  app_dir="${INSTALL_DIR}"
  if [[ -d "${app_dir}/.git" ]]; then
    log "Updating existing install: ${app_dir}"
    git -C "${app_dir}" fetch origin --prune >&2
    git -C "${app_dir}" checkout "${BRANCH}" >&2
    git -C "${app_dir}" pull --ff-only origin "${BRANCH}" >&2
  else
    if [[ -e "${app_dir}" && -n "$(find "${app_dir}" -mindepth 1 -maxdepth 1 2>/dev/null)" ]]; then
      fail "${app_dir} exists and is not empty"
    fi
    log "Cloning ${REPO_URL} into ${app_dir}"
    git clone --branch "${BRANCH}" "${REPO_URL}" "${app_dir}" >&2
  fi
  printf '%s\n' "${app_dir}"
}

host_ip() {
  hostname -I 2>/dev/null | awk '{print $1}' || true
}

write_server_env() {
  local app_dir="$1"
  local env_file="${app_dir}/server/.env"
  if [[ -f "${env_file}" ]]; then
    log "Keeping existing server/.env"
    return
  fi

  local secret
  secret="$(openssl rand -hex 32)"
  log "Creating server/.env"
  cat >"${env_file}" <<EOF
SECRET_KEY=${secret}
DATABASE_URL=sqlite:///./bretter.db
IMAGE_STORE_PATH=${app_dir}/images-store
WINPE_STORE_PATH=${app_dir}/winpe-store
JOB_PAYLOAD_STORE_PATH=${app_dir}/job-payloads
SAMBA_AUTO_PROVISION_USERS=false
EOF
}

generate_certs() {
  local app_dir="$1"
  local cert_dir="${app_dir}/.certs"
  local cert="${cert_dir}/bretter-img.crt"
  local key="${cert_dir}/bretter-img.key"
  if [[ -f "${cert}" && -f "${key}" ]]; then
    log "Keeping existing local TLS certificate"
    return
  fi

  mkdir -p "${cert_dir}"
  local ip
  ip="$(host_ip)"
  [[ -n "${ip}" ]] || ip="127.0.0.1"

  local conf="${cert_dir}/openssl.cnf"
  cat >"${conf}" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3_req
prompt = no

[dn]
CN = bretter-img.local

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
DNS.2 = bretter-img.local
IP.1 = 127.0.0.1
IP.2 = ${ip}
EOF

  log "Generating local TLS certificate"
  openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
    -keyout "${key}" \
    -out "${cert}" \
    -config "${conf}" >/dev/null 2>&1
}

install_python_deps() {
  local app_dir="$1"
  log "Installing backend dependencies"
  python3 -m venv "${app_dir}/server/lib"
  "${app_dir}/server/lib/bin/python" -m pip install --upgrade pip
  "${app_dir}/server/lib/bin/python" -m pip install -r "${app_dir}/server/requirements.txt"
}

install_frontend_deps() {
  local app_dir="$1"
  log "Installing frontend dependencies"
  if [[ -f "${app_dir}/frontend/package-lock.json" ]]; then
    npm --prefix "${app_dir}/frontend" ci
  else
    npm --prefix "${app_dir}/frontend" install
  fi
}

assert_python_venv() {
  if ! python_venv_works; then
    fail "Python venv support is missing. Install python3-venv, or rerun without --skip-system-deps."
  fi
}

create_admin_user() {
  local app_dir="$1"
  if [[ -z "${ADMIN_USER}" && -z "${ADMIN_PASSWORD}" ]]; then
    return
  fi
  [[ -n "${ADMIN_USER}" && -n "${ADMIN_PASSWORD}" ]] || fail "Both --admin-user and --admin-password are required"

  log "Creating initial admin user '${ADMIN_USER}' if it does not already exist"
  if ! "${app_dir}/server/lib/bin/python" "${app_dir}/scripts/create_admin.py" "${ADMIN_USER}" "${ADMIN_PASSWORD}"; then
    log "Admin creation was skipped or failed. Check whether the user already exists."
  fi
}

stop_existing_services() {
  local app_dir="$1"
  local run_dir="${app_dir}/.run"
  mkdir -p "${run_dir}"

  for name in server frontend; do
    local pid_file="${run_dir}/${name}.pid"
    if [[ -f "${pid_file}" ]]; then
      local pid
      pid="$(cat "${pid_file}")"
      if [[ -n "${pid}" && -d "/proc/${pid}" ]]; then
        log "Stopping existing ${name} process (${pid})"
        kill "${pid}" || true
      fi
      rm -f "${pid_file}"
    fi
  done
}

wait_for_url() {
  local label="$1"
  local url="$2"
  local log_file="$3"
  local attempt
  for attempt in $(seq 1 40); do
    if curl -kfsS "${url}" >/dev/null 2>&1; then
      log "${label} is ready: ${url}"
      return
    fi
    sleep 1
  done
  log "${label} did not become ready. Recent log output:"
  tail -n 40 "${log_file}" 2>/dev/null || true
  fail "${label} failed to start"
}

start_services() {
  local app_dir="$1"
  local run_dir="${app_dir}/.run"
  local ip
  ip="$(host_ip)"
  [[ -n "${ip}" ]] || ip="127.0.0.1"

  stop_existing_services "${app_dir}"

  log "Starting backend"
  (
    cd "${app_dir}"
    nohup ./start-server.sh >"${app_dir}/server.log" 2>&1 &
    printf '%s\n' "$!" >"${run_dir}/server.pid"
  )
  wait_for_url "Backend" "https://127.0.0.1:8000/health" "${app_dir}/server.log"

  log "Starting frontend"
  (
    cd "${app_dir}"
    nohup ./start-frontend.sh >"${app_dir}/frontend.log" 2>&1 &
    printf '%s\n' "$!" >"${run_dir}/frontend.pid"
  )
  wait_for_url "Frontend" "https://127.0.0.1:3000" "${app_dir}/frontend.log"

  cat <<EOF

Bretter-IMG is running.
Console: https://${ip}:3000
API docs: https://${ip}:8000/docs
Logs:    ${app_dir}/frontend.log
         ${app_dir}/server.log

Stop later with:
  kill \$(cat "${run_dir}/frontend.pid") \$(cat "${run_dir}/server.pid")
EOF
}

main() {
  install_system_deps
  assert_node_version
  assert_python_venv

  local app_dir
  app_dir="$(prepare_repo)"

  mkdir -p "${app_dir}/images-store" "${app_dir}/winpe-store" "${app_dir}/job-payloads"
  write_server_env "${app_dir}"
  generate_certs "${app_dir}"
  install_python_deps "${app_dir}"
  install_frontend_deps "${app_dir}"
  create_admin_user "${app_dir}"

  if [[ "${START_SERVICES}" -eq 1 ]]; then
    start_services "${app_dir}"
  else
    log "Install complete. Start later from ${app_dir} with ./install.sh"
  fi
}

main
