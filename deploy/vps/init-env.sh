#!/usr/bin/env sh
# 在 VPS 上运行（vps-deploy.sh 会调用）：
#   1. 首次部署时生成 .env（随机密码），已存在则不覆盖；
#   2. 每次都根据 .env 重新生成 SeaweedFS 的 S3 凭证文件 s3.json。
# 密码只保存在服务器上（权限 600），不会输出到终端。
set -eu

cd "$(dirname "$0")"
umask 077

rand() { openssl rand -hex 24; }

if [ -f .env ]; then
  echo "init-env: .env 已存在，保持不变"
else
  cat > .env <<EOF
# 由 init-env.sh 生成于 $(date -u +%Y-%m-%dT%H:%M:%SZ)，请勿提交到仓库
POSTGRES_USER=interview
POSTGRES_PASSWORD=$(rand)
POSTGRES_DB=interview
POSTGRES_PORT=25432

REDIS_PASSWORD=$(rand)
REDIS_PORT=26379

S3_ACCESS_KEY=interview$(openssl rand -hex 6)
S3_SECRET_KEY=$(rand)
S3_PORT=29000
S3_BROWSER_PORT=29001

PROMETHEUS_PORT=29090

GRAFANA_PORT=23000
GRAFANA_ADMIN_PASSWORD=$(rand)
EOF
  chmod 600 .env
  echo "init-env: 已生成 .env（密码未显示）"
fi

# shellcheck disable=SC1091
. ./.env
cat > s3.json <<EOF
{
  "identities": [
    {
      "name": "interview",
      "credentials": [{"accessKey": "${S3_ACCESS_KEY}", "secretKey": "${S3_SECRET_KEY}"}],
      "actions": ["Admin", "Read", "List", "Tagging", "Write"]
    }
  ]
}
EOF
# SeaweedFS 镜像以 seaweed 用户（uid/gid 1000）运行，凭证文件交给它，仍保持 600
chown 1000:1000 s3.json
chmod 600 s3.json
echo "init-env: 已生成 s3.json"
