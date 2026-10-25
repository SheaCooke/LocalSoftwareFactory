#!/usr/bin/env bash
# One-time setup for the software factory on WSL2 / Linux.
# Installs uv if missing, installs Python 3.12 (managed by uv), syncs .venv,
# creates .env, and checks git, Docker and the sandbox image.
#
#   ./scripts/setup.sh [--with-keyring]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*"; }

EXTRA=()
[[ "${1:-}" == "--with-keyring" ]] && EXTRA=(--extra keyring)

# --- Location -----------------------------------------------------------------
case "$ROOT" in
  /mnt/[a-z]/*)
    warn "the project is on a Windows drive ($ROOT). Git, file watching and Docker"
    warn "bind mounts are much slower and file modes are unreliable there; keep it under ~/."
    ;;
esac

# --- uv -----------------------------------------------------------------------
step "Checking uv"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "    uv not found; installing with pip --user"
  python3 -m pip install --user --quiet uv
fi
echo "    $(uv --version)"

# --- Python + dependencies ---------------------------------------------------
step "Installing Python 3.12 (uv-managed) and dependencies into .venv"
uv python install 3.12
uv sync "${EXTRA[@]}"
echo "    $(.venv/bin/python --version)"

# --- .env ---------------------------------------------------------------------
if [[ ! -f .env ]]; then
  step "Creating .env from .env.example (fill in the keys you need)"
  cp .env.example .env
fi
chmod 600 .env

# --- git ----------------------------------------------------------------------
step "Checking git"
command -v git >/dev/null 2>&1 || warn "git not found; it is required for worktree isolation (sudo apt install git)"

# --- Docker -------------------------------------------------------------------
step "Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
  warn "docker not found. The factory will use the restricted-subprocess sandbox (reduced isolation)."
elif ! docker info >/dev/null 2>&1; then
  if id -nG | grep -qw docker; then
    warn "the Docker daemon is not running. Start it with: sudo service docker start"
  else
    warn "cannot reach Docker; add yourself to the docker group: sudo usermod -aG docker \$USER (then restart WSL)"
  fi
else
  echo "    Docker $(docker info --format '{{.ServerVersion}}') is running; building the sandbox image."
  uv run factory sandbox build || warn "sandbox image build failed; reduced isolation until 'factory sandbox build' succeeds"
  if [[ "$(docker info --format '{{.SwapLimit}}')" != "true" ]]; then
    warn "Docker cannot limit swap here (common on WSL2 without systemd). The sandbox adds a"
    warn "per-process memory cap instead. For full cgroup limits, enable systemd in /etc/wsl.conf:"
    warn "  [boot]"
    warn "  systemd=true"
  fi
fi

step "Done. Try:  uv run factory prompt \"hello\" --model echo"
