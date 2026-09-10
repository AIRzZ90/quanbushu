#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${MINING_CONTROL_HOME:-$SCRIPT_DIR}"
ENV_FILE="${INSTALL_DIR}/.env"
MINING_DIR="${MINING_CONTROL_MINING_DIR:-${HOME}/quantus-mining}"
NODE_VERSION="${QUANTUS_NODE_VERSION:-v1.0.1}"
MINER_VERSION="${QUANTUS_MINER_VERSION:-v4.2.0}"
NODE_TARGET=""
MINER_ASSET=""

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

if [ ! -f "${INSTALL_DIR}/control.py" ]; then
  die "请在 quantus-mining-control 项目目录中执行此脚本。"
fi

if [ "$(id -u)" -ne 0 ]; then
  die "该控制器需要 root 运行，以便管理 Quantus 节点和矿工进程。"
fi

require_cmd curl
require_cmd python3
require_cmd tar
require_cmd sha256sum

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

MINING_DIR="${MINING_CONTROL_MINING_DIR:-$MINING_DIR}"
CONTROL_PORT="${MINING_CONTROL_PORT:-10200}"
CONTROL_TLS="${MINING_CONTROL_TLS:-1}"
NODE_VERSION="${QUANTUS_NODE_VERSION:-${NODE_VERSION}}"
MINER_VERSION="${QUANTUS_MINER_VERSION:-${MINER_VERSION}}"

detect_gpu_devices() {
  if [ -n "${MINING_CONTROL_GPU_DEVICES:-}" ]; then
    printf '%s' "$MINING_CONTROL_GPU_DEVICES"
    return
  fi

  if command -v nvidia-smi >/dev/null 2>&1; then
    local count
    if count="$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | awk 'NF { count++ } END { print count + 0 }')"; then
      if [ "$count" -gt 0 ] 2>/dev/null; then
        [ "$count" -le 16 ] || count=16
        printf '%s' "$count"
        return
      fi
    fi
  fi

  printf '1'
}

GPU_DEVICES="$(detect_gpu_devices)"

case "$(uname -m)" in
  x86_64|amd64)
    NODE_TARGET="x86_64-unknown-linux-gnu"
    MINER_ASSET="quantus-miner-linux-x86_64"
    ;;
  *)
    die "当前一键部署脚本只支持 Linux x86_64；Quantus 没有 Linux ARM64 miner 发行包。"
    ;;
esac

mkdir -p "$INSTALL_DIR" "$MINING_DIR/bin" "$MINING_DIR/logs"
chmod 700 "$INSTALL_DIR"

download_binaries() {
  local temp_dir node_asset node_url sums_url miner_url
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' RETURN

  node_asset="quantus-node-${NODE_VERSION}-${NODE_TARGET}.tar.gz"
  node_url="https://github.com/Quantus-Network/chain/releases/download/${NODE_VERSION}/${node_asset}"
  sums_url="https://github.com/Quantus-Network/chain/releases/download/${NODE_VERSION}/sha256sums-${NODE_VERSION}-${NODE_TARGET}.txt"
  miner_url="https://github.com/Quantus-Network/quantus-miner/releases/download/${MINER_VERSION}/${MINER_ASSET}"

  if [ ! -x "${MINING_DIR}/bin/quantus-node" ] || [ "${FORCE_DOWNLOAD:-0}" = "1" ]; then
    info "下载 quantus-node ${NODE_VERSION}..."
    curl -fsSL "$node_url" -o "${temp_dir}/${node_asset}"
    curl -fsSL "$sums_url" -o "${temp_dir}/checksums.txt"
    grep -F "$node_asset" "${temp_dir}/checksums.txt" > "${temp_dir}/node-checksum.txt" \
      || die "找不到 quantus-node 校验值。"
    (cd "$temp_dir" && sha256sum --check node-checksum.txt)
    tar -xzf "${temp_dir}/${node_asset}" -C "$temp_dir"
    [ -x "${temp_dir}/quantus-node" ] || die "节点压缩包中没有 quantus-node。"
    install -m 0755 "${temp_dir}/quantus-node" "${MINING_DIR}/bin/quantus-node"
  else
    info "使用已有 quantus-node: ${MINING_DIR}/bin/quantus-node"
  fi

  if [ ! -x "${MINING_DIR}/bin/quantus-miner" ] || [ "${FORCE_DOWNLOAD:-0}" = "1" ]; then
    info "下载 quantus-miner ${MINER_VERSION}..."
    curl -fsSL "$miner_url" -o "${temp_dir}/${MINER_ASSET}"
    install -m 0755 "${temp_dir}/${MINER_ASSET}" "${MINING_DIR}/bin/quantus-miner"
  else
    info "使用已有 quantus-miner: ${MINING_DIR}/bin/quantus-miner"
  fi
}

download_binaries

NODE_KEY_FILE="${MINING_CONTROL_NODE_KEY_FILE:-${MINING_DIR}/node_key.p2p}"
if [ ! -f "$NODE_KEY_FILE" ]; then
  info "生成节点 P2P 身份..."
  "${MINING_DIR}/bin/quantus-node" key generate-node-key --file "$NODE_KEY_FILE"
  chmod 600 "$NODE_KEY_FILE"
fi

PASSWORD_HASH="${MINING_CONTROL_PASSWORD_HASH:-}"
if [ -z "$PASSWORD_HASH" ] && [ -f "$ENV_FILE" ]; then
  PASSWORD_HASH="$(sed -n 's/^MINING_CONTROL_PASSWORD_HASH=//p' "$ENV_FILE" | head -n 1 | sed 's/^"\(.*\)"$/\1/')"
fi

if [ -z "$PASSWORD_HASH" ]; then
  PASSWORD="${MINING_CONTROL_PASSWORD:-}"
  if [ -z "$PASSWORD" ]; then
    if [ ! -t 0 ]; then
      die "非交互部署必须设置 MINING_CONTROL_PASSWORD 环境变量。"
    fi
    read -r -s -p "设置前端访问密码: " PASSWORD
    printf '\n'
    read -r -s -p "再次输入访问密码: " PASSWORD_CONFIRM
    printf '\n'
    [ "$PASSWORD" = "$PASSWORD_CONFIRM" ] || die "两次密码不一致。"
  fi
  [ "${#PASSWORD}" -ge 8 ] || die "访问密码至少需要 8 个字符。"
  PASSWORD_HASH="$(
    printf '%s' "$PASSWORD" | python3 -c '
import base64
import hashlib
import secrets
import sys

password = sys.stdin.buffer.read()
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password, salt, 310000, dklen=32)
encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
print(f"pbkdf2_sha256$310000${encode(salt)}${encode(digest)}")
'
  )"
  unset PASSWORD PASSWORD_CONFIRM MINING_CONTROL_PASSWORD
fi

TLS_CERT="${MINING_CONTROL_TLS_CERT:-${INSTALL_DIR}/tls/cert.pem}"
TLS_KEY="${MINING_CONTROL_TLS_KEY:-${INSTALL_DIR}/tls/key.pem}"
if [ "$CONTROL_TLS" != "0" ] && { [ ! -f "$TLS_CERT" ] || [ ! -f "$TLS_KEY" ]; }; then
  require_cmd openssl
  mkdir -p "$(dirname "$TLS_CERT")"
  mkdir -p "$(dirname "$TLS_KEY")"
  public_host="${MINING_CONTROL_PUBLIC_HOST:-${PUBLIC_IPADDR:-localhost}}"
  if [[ "$public_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    san="IP:${public_host},DNS:localhost"
  else
    san="DNS:${public_host},DNS:localhost"
  fi
  info "生成控制面板自签名 HTTPS 证书..."
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "$TLS_KEY" -out "$TLS_CERT" \
    -subj "/CN=${public_host}" -addext "subjectAltName=${san}" \
    >/dev/null 2>&1
  chmod 600 "$TLS_KEY"
  chmod 644 "$TLS_CERT"
fi

umask 077
cat > "$ENV_FILE" <<EOF
MINING_CONTROL_PASSWORD_HASH='${PASSWORD_HASH}'
MINING_CONTROL_BIND="${MINING_CONTROL_BIND:-0.0.0.0}"
MINING_CONTROL_PORT="${CONTROL_PORT}"
MINING_CONTROL_TLS="${CONTROL_TLS}"
MINING_CONTROL_REQUIRE_HTTPS="${MINING_CONTROL_REQUIRE_HTTPS:-1}"
MINING_CONTROL_NODE_NAME="${MINING_CONTROL_NODE_NAME:-quantus-miner}"
MINING_CONTROL_CPU_WORKERS="${MINING_CONTROL_CPU_WORKERS:-8}"
MINING_CONTROL_GPU_DEVICES="${GPU_DEVICES}"
MINING_CONTROL_CUDA_GPU="${MINING_CONTROL_CUDA_GPU:-1}"
MINING_CONTROL_GPU_BATCH_SIZE="${MINING_CONTROL_GPU_BATCH_SIZE:-16777216}"
MINING_CONTROL_GPU_THROTTLE_MS="${MINING_CONTROL_GPU_THROTTLE_MS:-0}"
MINING_CONTROL_MINING_DIR="${MINING_DIR}"
MINING_CONTROL_NODE_BIN="${MINING_CONTROL_NODE_BIN:-${MINING_DIR}/bin/quantus-node}"
MINING_CONTROL_MINER_BIN="${MINING_CONTROL_MINER_BIN:-${MINING_DIR}/bin/quantus-miner}"
MINING_CONTROL_NODE_KEY_FILE="${NODE_KEY_FILE}"
MINING_CONTROL_NODE_BASE_PATH="${MINING_CONTROL_NODE_BASE_PATH:-/root/.local/share/quantus-node}"
MINING_CONTROL_NODE_METRICS_PORT="${MINING_CONTROL_NODE_METRICS_PORT:-9615}"
MINING_CONTROL_NODE_RPC_PORT="${MINING_CONTROL_NODE_RPC_PORT:-9944}"
MINING_CONTROL_MINER_LISTEN_PORT="${MINING_CONTROL_MINER_LISTEN_PORT:-9833}"
MINING_CONTROL_MINER_METRICS_PORT="${MINING_CONTROL_MINER_METRICS_PORT:-9900}"
MINING_CONTROL_CHAIN="${MINING_CONTROL_CHAIN:-mainnet}"
MINING_CONTROL_PUBLIC_HOST="${MINING_CONTROL_PUBLIC_HOST:-}"
MINING_CONTROL_PUBLIC_PORT="${MINING_CONTROL_PUBLIC_PORT:-}"
MINING_CONTROL_PUBLIC_URL="${MINING_CONTROL_PUBLIC_URL:-}"
MINING_CONTROL_TLS_CERT="${TLS_CERT}"
MINING_CONTROL_TLS_KEY="${TLS_KEY}"
EOF
chmod 600 "$ENV_FILE"

if command -v supervisorctl >/dev/null 2>&1 && [ -d /etc/supervisor/conf.d ]; then
  install -m 0755 "${INSTALL_DIR}/run-control.sh" /opt/supervisor-scripts/quantus-mining-control.sh
  sed "s#^environment=.*#environment=PROC_NAME=\"%(program_name)s\",MINING_CONTROL_HOME=\"${INSTALL_DIR}\"#; s#^directory=.*#directory=${INSTALL_DIR}#" \
    "${INSTALL_DIR}/supervisor-quantus-mining-control.conf" \
    > /etc/supervisor/conf.d/quantus-mining-control.conf
  supervisorctl reread >/dev/null
  supervisorctl update >/dev/null
  supervisorctl restart quantus-mining-control >/dev/null
  info "已由 supervisor 启动控制面板。"
elif command -v systemctl >/dev/null 2>&1; then
  cat > /etc/systemd/system/quantus-mining-control.service <<EOF
[Unit]
Description=Quantus Mining Control
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
Environment=MINING_CONTROL_HOME=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/run-control.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 /etc/systemd/system/quantus-mining-control.service
  systemctl daemon-reload
  systemctl enable --now quantus-mining-control.service >/dev/null
  info "已由 systemd 启动控制面板。"
else
  die "未找到 supervisor 或 systemd，无法注册常驻控制服务。"
fi

wait_for_control() {
  local health_url
  health_url="http://127.0.0.1:${CONTROL_PORT}/healthz"
  if [ "$CONTROL_TLS" != "0" ]; then
    health_url="https://127.0.0.1:${CONTROL_PORT}/healthz"
  fi
  for ((attempt = 1; attempt <= 30; attempt++)); do
    if [ "$CONTROL_TLS" != "0" ]; then
      if curl -kfsS --max-time 2 "$health_url" >/dev/null 2>&1; then
        return 0
      fi
    elif curl -fsS --max-time 2 "$health_url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

login_url() {
  local scheme public_url public_host public_port port_variable url_host
  scheme="http"
  [ "$CONTROL_TLS" != "0" ] && scheme="https"
  public_url="${MINING_CONTROL_PUBLIC_URL:-}"

  if [ -z "$public_url" ]; then
    public_host="${MINING_CONTROL_PUBLIC_HOST:-${PUBLIC_IPADDR:-}}"
    public_port="${MINING_CONTROL_PUBLIC_PORT:-}"
    if [ -z "$public_port" ]; then
      port_variable="VAST_TCP_PORT_${CONTROL_PORT}"
      public_port="${!port_variable:-}"
    fi
    [ -n "$public_host" ] || public_host="localhost"
    if [[ "$public_host" == *:* && "$public_host" != \[*\] ]]; then
      url_host="[${public_host}]"
    else
      url_host="$public_host"
    fi
    if [ -n "$public_port" ]; then
      public_url="${scheme}://${url_host}:${public_port}/"
    else
      public_url="${scheme}://${url_host}:${CONTROL_PORT}/"
    fi
  elif [[ "$public_url" != */ ]]; then
    public_url="${public_url}/"
  fi

  printf '%s' "$public_url"
}

info "部署完成。"
info "控制面板内部端口: ${CONTROL_PORT}"
if [ "$CONTROL_TLS" != "0" ]; then
  info "控制面板协议: HTTPS（自签名证书，首次打开需要在浏览器中确认证书）"
fi
if wait_for_control; then
  info "控制面板健康检查: 正常"
else
  info "控制面板健康检查: 超时，请检查 supervisor 或 systemd 日志。"
fi
info "前端登录地址: $(login_url)"
