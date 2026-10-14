#!/usr/bin/env bash
# 启停可观测性服务（compose 的 observability profile）：
#   本机：Langfuse（deploy/local/docker-compose.yml，自带 Postgres/Redis/S3/ClickHouse）
#   VPS：Prometheus、Grafana（deploy/vps/docker-compose.yml，需要先 make vps-deploy）
# 用法：obs.sh up | down。down 只停止容器，数据卷保留，不影响核心服务。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_COMPOSE=(docker compose -f "$ROOT_DIR/deploy/local/docker-compose.yml" --profile observability)
VPS_SERVICES="prometheus grafana"

vps() (
  source "$ROOT_DIR/scripts/_lib.sh"
  vps_ssh "cd '$VPS_DIR' && docker compose --profile observability $1"
)

case "${1:-}" in
  up)
    bash "$ROOT_DIR/scripts/langfuse-local-init.sh"
    "${LOCAL_COMPOSE[@]}" up -d
    vps "up -d $VPS_SERVICES"
    echo "Langfuse 首次启动需要 2–3 分钟完成数据库迁移：http://127.0.0.1:23100"
    ;;
  down)
    "${LOCAL_COMPOSE[@]}" stop
    vps "stop $VPS_SERVICES"
    ;;
  *) echo "用法：$0 up|down" >&2; exit 1 ;;
esac
