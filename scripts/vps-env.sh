#!/usr/bin/env bash
# 从 VPS 读取生成的密码，写入本机 .env 中由本脚本管理的区块（通过隧道访问的连接串）。
# 不在终端输出任何密码。可重复执行：每次整体替换管理区块，其它内容保持不变。
source "$(dirname "$0")/_lib.sh"

remote_env="$(vps_ssh "cat '$VPS_DIR/.env'")"
get() { printf '%s\n' "$remote_env" | grep -E "^$1=" | tail -1 | cut -d= -f2-; }

block="$(cat <<EOF
# >>> vps-env（由 scripts/vps-env.sh 生成，请勿手改）
DATABASE_URL=postgresql+asyncpg://$(get POSTGRES_USER):$(get POSTGRES_PASSWORD)@127.0.0.1:$(get POSTGRES_PORT)/$(get POSTGRES_DB)
REDIS_URL=redis://:$(get REDIS_PASSWORD)@127.0.0.1:$(get REDIS_PORT)/0
S3_ENDPOINT=http://127.0.0.1:$(get S3_PORT)
S3_ACCESS_KEY=$(get S3_ACCESS_KEY)
S3_SECRET_KEY=$(get S3_SECRET_KEY)
S3_REGION=us-east-1
S3_BUCKET_RECORDINGS=recordings
S3_BUCKET_RESUMES=resumes
GRAFANA_URL=http://127.0.0.1:$(get GRAFANA_PORT)
GRAFANA_ADMIN_PASSWORD=$(get GRAFANA_ADMIN_PASSWORD)
PROMETHEUS_URL=http://127.0.0.1:$(get PROMETHEUS_PORT)
# <<< vps-env
EOF
)"

touch "$ENV_FILE"
chmod 600 "$ENV_FILE" 2>/dev/null || true
# 删除旧的管理区块，再追加新的
tmp="$(mktemp)"
awk '/^# >>> vps-env/{skip=1} !skip{print} /^# <<< vps-env/{skip=0}' "$ENV_FILE" > "$tmp"
printf '%s\n' "$block" >> "$tmp"
mv "$tmp" "$ENV_FILE"
echo "已更新 .env 中的 vps-env 区块（密码未显示）"
