#!/usr/bin/env bash
# 把 deploy/vps/ 下的依赖部署到 VPS（可重复执行）：
# 上传配置 → 首次生成随机密码 → docker compose up -d → 显示状态。
# 只启动核心服务；可观测性服务（observability profile）用 make obs-up 单独启动，
# 若它们已在运行，这里会带上该 profile 一起更新，避免被 --remove-orphans 当成孤儿删除。
# 只操作 interview-agent 这个 compose 项目，不影响服务器上的其它容器。
source "$(dirname "$0")/_lib.sh"

echo "部署到 $SSH_TARGET:$VPS_DIR"
vps_ssh "mkdir -p '$VPS_DIR/grafana/provisioning/datasources'"
vps_scp "$ROOT_DIR/deploy/vps/docker-compose.yml" \
        "$ROOT_DIR/deploy/vps/prometheus.yml" \
        "$ROOT_DIR/deploy/vps/init-env.sh" \
        "$SSH_TARGET:$VPS_DIR/"
vps_scp "$ROOT_DIR/deploy/vps/grafana/provisioning/datasources/prometheus.yml" \
        "$SSH_TARGET:$VPS_DIR/grafana/provisioning/datasources/"

vps_ssh "cd '$VPS_DIR' && sh init-env.sh && \
  if [ -n \"\$(docker compose --profile observability ps -q prometheus)\" ]; then \
    export COMPOSE_PROFILES=observability; fi && \
  docker compose up -d --remove-orphans && docker compose ps"
