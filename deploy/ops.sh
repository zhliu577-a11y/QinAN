#!/usr/bin/env bash
# 运维入口。跑在宿主机上，包住 docker compose 与 /internal/ 接口。
#
# 为什么重启和「别人的日志」不放进取网关的接口里：
#   容器要重启自己 / 读其他容器的 stdout，就得挂 docker socket。挂上 socket
#   等价于把宿主机 root 交出去（可以起特权容器挂 / ），为了几个运维按钮不值。
#   所以：进程内的东西走 /internal/（见 logs-api），容器层的操作走这个脚本。
#
# 用法：./ops.sh help
set -uo pipefail

cd "$(dirname "$0")" || exit 1
COMPOSE=(docker compose)
ENV_FILE=".env"

if [ ! -f "$ENV_FILE" ]; then
  echo "缺少 $ENV_FILE，先按 deploy/README.md 生成" >&2
  exit 1
fi
# ADMIN_TOKEN 只用于内部接口；不打印、不落日志
ADMIN_TOKEN="$(grep -E '^ADMIN_TOKEN=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
BASE_INTERNAL="http://127.0.0.1:8080"

die() { echo "错误: $*" >&2; exit 1; }

pretty() {
  # 宿主机上有啥用啥；都没有就原样输出 JSON
  if command -v python3 >/dev/null 2>&1; then
    python3 -m json.tool 2>/dev/null || cat
  elif command -v python >/dev/null 2>&1; then
    python -m json.tool 2>/dev/null || cat
  elif command -v jq >/dev/null 2>&1; then
    jq . 2>/dev/null || cat
  else
    cat
  fi
}

# 内部接口只在 internal 网络里可达（nginx 对 /internal/ 是 deny all），
# 所以必须从 gateway 容器内部打。
api() {
  local path="$1"
  "${COMPOSE[@]}" exec -T gateway curl -s -m 10 \
    -H "X-Admin-Token: $ADMIN_TOKEN" "${BASE_INTERNAL}${path}"
}

wait_healthy() {
  local timeout="${1:-60}" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if curl -sk -m 5 https://127.0.0.1/api/v1/health 2>/dev/null | grep -q '"status":"ok"'; then
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
    printf '.'
  done
  return 1
}

resolved_targets() {
  case "$1" in
    gateway)   echo "gateway" ;;
    nginx)     echo "nginx" ;;
    opencode)  opencode_services ;;
    all)       echo "gateway nginx $(opencode_services)" ;;
    *)         die "未知目标 '$1'（可选：gateway|nginx|opencode|all）" ;;
  esac
}

# 实例个数从 compose 里读，不写死：扩容到第 4 个实例之后，
# `restart opencode` / `restart all` 必须也能带上它，否则新实例改了配置却重启不到。
# sort -V 让 opencode-10 排在 opencode-9 之后。
opencode_services() {
  "${COMPOSE[@]}" config --services 2>/dev/null \
    | grep -E '^opencode-' | sort -V | tr '\n' ' ' | sed 's/ *$//'
}

cmd_status() {
  echo "== 容器 =="
  "${COMPOSE[@]}" ps --format '  {{.Name}}  {{.Status}}'
  echo
  echo "== 对外入口 =="
  curl -sk -m 5 https://127.0.0.1/api/v1/health | pretty
  echo
  echo "== 网关自检 =="
  api /internal/status | pretty
}

cmd_health() {
  # 给监控/定时任务用：退出码即状态
  local body
  body="$(api /internal/status)" || exit 2
  if printf '%s' "$body" | grep -q '"status":"ok"'; then
    echo "ok"
    exit 0
  fi
  echo "degraded"
  printf '%s' "$body" | pretty >&2
  exit 1
}

cmd_instances() { api /internal/instances | pretty; }
cmd_metrics()   { api /internal/metrics   | pretty; }
cmd_users()     { api /internal/users     | pretty; }

cmd_user() {
  [ $# -ge 1 ] || die "用法: ops.sh user <用户名|用户ID>"
  local key="$1" id
  case "$key" in
    *[!0-9]*)  # 含非数字 → 当用户名，先查 id
      id="$(api /internal/users?limit=200 \
        | tr '}' '\n' | grep -F "\"username\":\"$key\"" \
        | grep -o '"id":[0-9]*' | head -n1 | cut -d: -f2)"
      [ -n "$id" ] || die "找不到用户 '$key'"
      ;;
    *) id="$key" ;;
  esac
  echo "用户 ID: $id"
  api "/internal/users/$id" | pretty
}

cmd_logs() {
  [ $# -ge 1 ] || die "用法: ops.sh logs <服务名> [-f] [-n 行数]（服务名见 ops.sh status）"
  local service="$1"; shift
  local follow=() tail_args=(--tail=200)
  while [ $# -gt 0 ]; do
    case "$1" in
      -f|--follow) follow=(-f) ;;
      -n|--tail)   shift; tail_args=("--tail=$1") ;;
      *)           die "未知参数 '$1'" ;;
    esac
    shift
  done
  "${COMPOSE[@]}" logs "${tail_args[@]}" "${follow[@]}" "$service"
}

cmd_logs_api() {
  # 网关进程自己的日志（环形缓冲）。容器 stdout 里的权威副本仍然靠 logs gateway。
  local query="?limit=200"
  while [ $# -gt 0 ]; do
    case "$1" in
      --level)  shift; query="${query}&level=$1" ;;
      --task)   shift; query="${query}&task_id=$1" ;;
      --logger) shift; query="${query}&logger_name=$1" ;;
      --limit)  shift; query="?limit=$1" ;;
      --after)  shift; query="${query}&after_seq=$1" ;;
      *)        die "未知参数 '$1'" ;;
    esac
    shift
  done
  api "/internal/logs${query}" | pretty
}

cmd_restart() {
  [ $# -ge 1 ] || die "用法: ops.sh restart <gateway|nginx|opencode|all>"
  local targets
  targets="$(resolved_targets "$1")" || exit 1
  # 空目标会让 `up -d --force-recreate` 变成「重建所有服务」，比报错危险得多
  [ -n "$targets" ] || die "没解析出要重建的服务，检查 docker-compose.yml"
  echo "重建: $targets"
  # 一律用 --force-recreate 而不是 restart：
  #   * nginx.conf 是单文件 bind mount，restart 只会重载「容器创建时那个 inode」，
  #     改过的配置看不到，必须重建容器；
  #   * .env / compose 的改动同样只有重建才生效。
  # 频率很低（改配置、修故障），慢一点换「永远是对的」。
  # 注意 gateway 是单 worker、状态全在进程内，重建会打断在飞任务 ——
  # 启动时 _recover_orphans 会把它们放回队列重试一次，不会永久卡在 running。
  # shellcheck disable=SC2086
  "${COMPOSE[@]}" up -d --force-recreate $targets || die "重建失败"
  echo -n "等待健康"
  if wait_healthy 60; then
    echo " 已恢复"
    cmd_status
  else
    echo " 超时"
    echo "看日志: ./ops.sh logs gateway -n 100" >&2
    exit 1
  fi
}

cmd_heal() {
  # 只重建不健康的容器：适合排查「某一个 opencode 挂了」
  local bad
  bad="$("${COMPOSE[@]}" ps --format '{{.Name}} {{.State}}' \
    | awk '$2 != "running" {print $1}' \
    | sed 's/^agent-//' | tr '\n' ' ')"
  if [ -z "${bad// /}" ]; then
    echo "没有不健康的容器"
    return 0
  fi
  echo "重建不健康的容器: $bad"
  # shellcheck disable=SC2086
  "${COMPOSE[@]}" up -d --force-recreate $bad || die "重建失败"
  wait_healthy 60 && echo "已恢复"
}

cmd_shell() {
  [ $# -ge 1 ] || die "用法: ops.sh shell <服务名>"
  "${COMPOSE[@]}" exec "$1" sh
}

cmd_backup() {
  # 卷的真实名字带 compose 项目名前缀（这里是 agent-gateway_*），而项目名可能
  # 被改掉。所以不去猜命名规则，直接从容器挂载点反查 —— 那才是权威。
  local stamp target="backup"
  stamp="$(date +%F-%H%M)"
  mkdir -p "$target" || die "无法创建 $target"

  local data_vol ws_vol
  data_vol="$(volume_of agent-gateway /srv/agent/data)"
  ws_vol="$(volume_of agent-opencode-1 /srv/agent/workspace)"
  [ -n "$data_vol" ] || die "查不到 gateway 的数据卷（容器在跑吗？）"
  [ -n "$ws_vol" ] || die "查不到 workspace 卷（opencode-1 在跑吗？）"

  # SQLite 在写入过程中被复制会拿到半截状态，先停写。两个卷一起备、只停一次。
  "${COMPOSE[@]}" stop gateway >/dev/null 2>&1
  local rc=0
  for pair in "$data_vol:$target/gateway-data-${stamp}.tgz" \
              "$ws_vol:$target/workspace-${stamp}.tgz"; do
    local vol="${pair%%:*}" out="${pair#*:}"
    if docker run --rm -v "${vol}:/data:ro" -v "$PWD/$target:/backup" \
        alpine tar czf "/backup/$(basename "$out")" -C /data .; then
      echo "已备份 $out  (卷 $vol)"
    else
      echo "备份 $vol 失败" >&2
      rc=1
    fi
  done
  "${COMPOSE[@]}" up -d gateway >/dev/null 2>&1
  echo -n "等待网关恢复"
  wait_healthy 60 && echo " 已恢复" || echo " 未恢复，看 ./ops.sh logs gateway"
  ls -lh "$target" | tail -n +2
  return $rc
}

volume_of() {
  docker inspect -f \
    "{{range .Mounts}}{{if eq .Destination \"$2\"}}{{.Name}}{{end}}{{end}}" "$1" 2>/dev/null
}

cmd_help() {
  cat <<'USAGE'
网关运维入口

  status                      容器 + 对外健康 + 网关自检（先看这个）
  health                      只输出 ok/degraded，退出码即状态，给监控用
  instances                   后端 opencode 进程明细（谁在忙、忙多久、失败几次）
  logs-api [选项]             网关进程自己的近期日志
        --level error|warning|info|debug   只看某级别以上
        --task <task_id>                   只看某个任务
        --logger <前缀>                     只看某个模块，如 app.services
        --limit <N>                         条数（默认 200）
        --after <seq>                       增量：只取比这个 seq 新的
  logs <服务> [-f] [-n N]     容器 stdout 日志（nginx/gateway/opencode-1…）
  user <用户名|用户ID>        单个用户详情：配额用量、任务分布、设备、实例绑定
  users                       用户列表
  metrics                     池与队列指标
  restart <目标>              重建：gateway|nginx|opencode|all
  heal                        只重建不健康的容器
  shell <服务>                进容器
  backup                      备份 gateway-data 与 workspace 卷

本机自测时若 8443 不可用，把 status/health 里的 127.0.0.1 换成实际入口地址。
USAGE
}

main() {
  local cmd="${1:-help}"
  shift || true
  case "$cmd" in
    status)    cmd_status "$@" ;;
    health)    cmd_health "$@" ;;
    instances) cmd_instances "$@" ;;
    logs)      cmd_logs "$@" ;;
    logs-api)  cmd_logs_api "$@" ;;
    user)      cmd_user "$@" ;;
    users)     cmd_users "$@" ;;
    metrics)   cmd_metrics "$@" ;;
    restart)   cmd_restart "$@" ;;
    heal)      cmd_heal "$@" ;;
    shell)     cmd_shell "$@" ;;
    backup)    cmd_backup "$@" ;;
    help|-h|--help) cmd_help ;;
    *) die "未知命令 '$cmd'（试试 ./ops.sh help）" ;;
  esac
}

main "$@"
