#!/usr/bin/env bash
# 在本机启动 LiveKit Server（前台运行，Ctrl+C 结束）。
# 首次使用前先运行 scripts/fetch-livekit.sh；密钥来自 .env 的 LIVEKIT_API_KEY / LIVEKIT_API_SECRET，
# 不存在时自动生成并写入 .env（仅本机开发使用）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) BIN="$ROOT_DIR/.tools/livekit/livekit-server.exe" ;;
  *) BIN="$ROOT_DIR/.tools/livekit/livekit-server" ;;
esac

if [ ! -x "$BIN" ]; then
  echo "找不到 $BIN，请先运行：bash scripts/fetch-livekit.sh" >&2
  exit 1
fi

env_get() { [ -f "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

key="$(env_get LIVEKIT_API_KEY)"
secret="$(env_get LIVEKIT_API_SECRET)"
if [ -z "$key" ] || [ -z "$secret" ]; then
  key="dev$(openssl rand -hex 4)"
  secret="$(openssl rand -hex 32)"
  {
    echo "LIVEKIT_URL=ws://127.0.0.1:7880"
    echo "LIVEKIT_API_KEY=$key"
    echo "LIVEKIT_API_SECRET=$secret"
  } >> "$ENV_FILE"
  echo "已生成本机 LiveKit 密钥并写入 .env（未显示）"
fi

exec "$BIN" --config "$ROOT_DIR/deploy/local/livekit.yaml" --keys "$key: $secret"
