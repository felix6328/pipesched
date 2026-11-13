#!/usr/bin/env bash
# 拉起官方 cgwire/cgwire:1.0.79（Kitsu + Zou + Postgres + Redis）。不修改镜像内容。
set -euo pipefail
export PATH="${HOME}/.local/bin:/usr/local/bin:/usr/bin:${PATH}"
# 本盒子上 Docker Engine 在 tcp://127.0.0.1:2375，没有 unix socket，也没有系统 docker CLI。
# 若你本机有正常的 Docker Desktop / docker CLI，不需要设 DOCKER_HOST。
if [[ -z "${DOCKER_HOST:-}" ]]; then
  if [[ ! -S /var/run/docker.sock ]] && curl -sS --max-time 1 http://127.0.0.1:2375/version >/dev/null 2>&1; then
    export DOCKER_HOST=tcp://127.0.0.1:2375
  fi
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "找不到 docker CLI。请安装 Docker，或把静态 docker 客户端放到 PATH。"
  echo "本仓库不修改、不捆绑 Kitsu 源码。"
  exit 1
fi
IMAGE="${PIPESCHED_KITSU_IMAGE:-cgwire/cgwire:1.0.79}"
NAME="${PIPESCHED_KITSU_CONTAINER:-cgwire}"
PORT="${PIPESCHED_KITSU_PORT:-8878}"
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "容器 $NAME 已存在，启动中…"
  docker start "$NAME" >/dev/null
else
  echo "拉取 $IMAGE …"
  docker pull "$IMAGE"
  docker run -d --name "$NAME" --memory=2560m --cpus=2 -p "${PORT}:80" --restart unless-stopped "$IMAGE"
fi
echo "等待 http://127.0.0.1:${PORT}/api …"
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/api" || true)
  if [[ "$code" == "200" || "$code" == "401" ]]; then
    echo "就绪。Kitsu UI: http://127.0.0.1:${PORT}/   API: http://127.0.0.1:${PORT}/api"
    echo "官方默认账号：admin@example.com / mysecretpassword（见 https://zou.cg-wire.com/ ）"
    curl -s "http://127.0.0.1:${PORT}/api"
    echo
    exit 0
  fi
  sleep 2
done
echo "超时。docker logs $NAME："
docker logs --tail 50 "$NAME" || true
exit 1
