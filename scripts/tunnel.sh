#!/usr/bin/env bash
# 本机开发时保持运行：
#   正向（-L）：把 VPS 上只监听 127.0.0.1 的服务映射到本机同端口
#   反向（-R）：把本机 API / 语音 worker / 评测进程的指标端口暴露给 VPS 上的 Prometheus 抓取
# Ctrl+C 结束。端口需与 deploy/vps/init-env.sh、deploy/vps/prometheus.yml 保持一致。
source "$(dirname "$0")/_lib.sh"

API_METRICS_LOCAL_PORT="$(env_get API_METRICS_PORT)"
WORKER_METRICS_LOCAL_PORT="$(env_get WORKER_METRICS_PORT)"
EVAL_METRICS_LOCAL_PORT="$(env_get EVAL_METRICS_PORT)"
API_METRICS_LOCAL_PORT="${API_METRICS_LOCAL_PORT:-8000}"
WORKER_METRICS_LOCAL_PORT="${WORKER_METRICS_LOCAL_PORT:-9465}"
EVAL_METRICS_LOCAL_PORT="${EVAL_METRICS_LOCAL_PORT:-9466}"

echo "隧道已建立后：Postgres 127.0.0.1:25432  Redis :26379  S3 :29000（文件浏览 http://127.0.0.1:29001）"
echo "             Prometheus http://127.0.0.1:29090  Grafana http://127.0.0.1:23000"
exec ssh "${SSH_OPTS[@]}" -N \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 25432:127.0.0.1:25432 \
  -L 26379:127.0.0.1:26379 \
  -L 29000:127.0.0.1:29000 \
  -L 29001:127.0.0.1:29001 \
  -L 29090:127.0.0.1:29090 \
  -L 23000:127.0.0.1:23000 \
  -R "127.0.0.1:29464:127.0.0.1:${API_METRICS_LOCAL_PORT}" \
  -R "127.0.0.1:29465:127.0.0.1:${WORKER_METRICS_LOCAL_PORT}" \
  -R "127.0.0.1:29466:127.0.0.1:${EVAL_METRICS_LOCAL_PORT}" \
  "$SSH_TARGET"
