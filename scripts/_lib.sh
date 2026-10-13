#!/usr/bin/env bash
# 脚本公共部分：读取仓库根目录 .env 中的 VPS 连接信息。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

env_get() {
  # 从 .env 读取单个变量（不 source 整个文件，避免执行其中内容）
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true
}

VPS_HOST="$(env_get VPS_HOST)"
VPS_PORT="$(env_get VPS_PORT)"
VPS_USER="$(env_get VPS_USER)"
VPS_DIR="$(env_get VPS_DIR)"
VPS_PORT="${VPS_PORT:-22}"
VPS_USER="${VPS_USER:-root}"
VPS_DIR="${VPS_DIR:-/opt/interview-agent}"

if [ -z "$VPS_HOST" ]; then
  echo "缺少 VPS_HOST：请在 .env 中填写 VPS_HOST / VPS_PORT / VPS_USER（参考 .env.example）" >&2
  exit 1
fi

SSH_OPTS=(-p "$VPS_PORT" -o BatchMode=yes -o ConnectTimeout=15)
SSH_TARGET="$VPS_USER@$VPS_HOST"

vps_ssh() { ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"; }
vps_scp() { scp -P "$VPS_PORT" -o BatchMode=yes -o ConnectTimeout=15 "$@"; }
