#!/usr/bin/env bash
# 本机 Langfuse 的初始化（obs.sh up 会调用，可重复执行）：
#   1. 逐项补齐 deploy/local/.env 中的随机密码与 API 密钥（已有的保持不变）；
#   2. 生成 SeaweedFS 的 S3 凭证文件 deploy/local/s3.json；
#   3. 把 Langfuse 地址、密钥和登录账号写入仓库根目录 .env 的 langfuse-local 区块。
# 不在终端输出任何密码。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="$ROOT_DIR/deploy/local"
LOCAL_ENV="$LOCAL_DIR/.env"
ROOT_ENV="$ROOT_DIR/.env"
umask 077

rand() { openssl rand -hex 24; }
ensure() {
  if ! grep -q "^$1=" "$LOCAL_ENV"; then
    printf '%s=%s\n' "$1" "$2" >> "$LOCAL_ENV"
    echo "langfuse-local-init: 新增 $1"
  fi
}
get() { grep -E "^$1=" "$LOCAL_ENV" | tail -1 | cut -d= -f2-; }

[ -f "$LOCAL_ENV" ] || echo "# 由 scripts/langfuse-local-init.sh 生成，请勿提交到仓库" > "$LOCAL_ENV"
chmod 600 "$LOCAL_ENV"
# 密码只用十六进制字符，避免连接串需要转义
ensure LANGFUSE_PORT 23100
ensure S3_PORT 23101
ensure POSTGRES_PASSWORD "$(rand)"
ensure REDIS_PASSWORD "$(rand)"
ensure CLICKHOUSE_PASSWORD "$(rand)"
ensure S3_ACCESS_KEY "langfuse$(openssl rand -hex 6)"
ensure S3_SECRET_KEY "$(rand)"
ensure LANGFUSE_SALT "$(rand)"
ensure LANGFUSE_ENCRYPTION_KEY "$(openssl rand -hex 32)"
ensure LANGFUSE_NEXTAUTH_SECRET "$(rand)"
ensure LANGFUSE_INIT_PUBLIC_KEY "pk-lf-$(openssl rand -hex 16)"
ensure LANGFUSE_INIT_SECRET_KEY "sk-lf-$(openssl rand -hex 16)"
ensure LANGFUSE_INIT_USER_EMAIL admin@interview.local
ensure LANGFUSE_INIT_USER_PASSWORD "$(rand)"

cat > "$LOCAL_DIR/s3.json" <<JSON
{
  "identities": [
    {
      "name": "langfuse",
      "credentials": [{"accessKey": "$(get S3_ACCESS_KEY)", "secretKey": "$(get S3_SECRET_KEY)"}],
      "actions": ["Admin", "Read", "List", "Tagging", "Write"]
    }
  ]
}
JSON
# SeaweedFS 容器以 uid 1000 运行，需要能读到挂载的凭证文件（只在本机、已被 .gitignore 排除）
chmod 644 "$LOCAL_DIR/s3.json"

block="$(cat <<BLOCK
# >>> langfuse-local（由 scripts/langfuse-local-init.sh 生成，请勿手改）
LANGFUSE_HOST=http://127.0.0.1:$(get LANGFUSE_PORT)
LANGFUSE_PUBLIC_KEY=$(get LANGFUSE_INIT_PUBLIC_KEY)
LANGFUSE_SECRET_KEY=$(get LANGFUSE_INIT_SECRET_KEY)
LANGFUSE_ADMIN_EMAIL=$(get LANGFUSE_INIT_USER_EMAIL)
LANGFUSE_ADMIN_PASSWORD=$(get LANGFUSE_INIT_USER_PASSWORD)
# <<< langfuse-local
BLOCK
)"
touch "$ROOT_ENV"
chmod 600 "$ROOT_ENV" 2>/dev/null || true
tmp="$(mktemp)"
awk '/^# >>> langfuse-local/{skip=1} !skip{print} /^# <<< langfuse-local/{skip=0}' "$ROOT_ENV" > "$tmp"
printf '%s\n' "$block" >> "$tmp"
mv "$tmp" "$ROOT_ENV"
echo "langfuse-local-init: 已更新 .env 中的 langfuse-local 区块（密码未显示）"
