#!/usr/bin/env bash
#
# 生成自签证书，**仅用于本地 / 内网演练**，不要用于生产。
# 生产请用云厂商签发的正式证书，或 ACME 自动续期。
#
# nginx.conf 里写死了两个文件名，本脚本按同样的名字输出到 deploy/certs/：
#   certs/fullchain.pem   certs/privkey.pem
#
# 用法：
#   ./gen-self-signed-cert.sh 192.168.1.50
#   ./gen-self-signed-cert.sh agent.example.com
#   ./gen-self-signed-cert.sh 192.168.1.50 agent.example.com   # 多个都写进 SAN
#
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "用法: $0 <域名或IP> [更多域名或IP...]" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="${SCRIPT_DIR}/certs"
DAYS=825

# 组装 SAN：IPv4 走 IP:，其余按 DNS 处理。
# 手机端普遍要求证书带 SAN，只写 CN 会直接被拒。
SAN=""
for name in "$@"; do
  if [[ "${name}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN="${SAN}IP:${name},"
  else
    SAN="${SAN}DNS:${name},"
  fi
done
SAN="${SAN%,}"

mkdir -p "${CERT_DIR}"

openssl req -x509 -nodes -newkey rsa:2048 -days "${DAYS}" \
  -keyout "${CERT_DIR}/privkey.pem" \
  -out "${CERT_DIR}/fullchain.pem" \
  -subj "/CN=$1" \
  -addext "subjectAltName=${SAN}" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
  -addext "extendedKeyUsage=serverAuth"

chmod 644 "${CERT_DIR}/fullchain.pem"
chmod 600 "${CERT_DIR}/privkey.pem"

echo "已生成自签证书："
echo "  证书   ${CERT_DIR}/fullchain.pem"
echo "  私钥   ${CERT_DIR}/privkey.pem"
echo "  含 SAN ${SAN}"
echo "  有效期 ${DAYS} 天"
echo
echo "提醒：自签证书不受系统信任，手机首次访问需手动信任；生产环境请换正式证书。"
