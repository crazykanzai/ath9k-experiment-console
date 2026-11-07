#!/usr/bin/env bash
# DHCP for the isolated ath9k AP. Called by ath9k-edca after AP IPv4 is ready.
# Manual use: sudo ./start_dhcp.sh {start|stop|status} [IFACE] [AP_IP/CIDR]
set -euo pipefail

ACTION="${1:-}"
IFACE="${2:-${WIFI_IFACE:-}}"
AP_IP_CIDR="${3:-${AP_IP_CIDR:-192.168.50.1/24}}"

if [[ -z "${IFACE}" ]]; then
  IFACE="$(ip -4 -o addr show 2>/dev/null | awk -v cidr="${AP_IP_CIDR}" '$4 == cidr {print $2; exit}')"
  if [[ -z "${IFACE}" && "${ACTION}" != start ]]; then
    shopt -s nullglob
    pidfiles=(/run/ath9k-edca-dhcp-*.pid)
    shopt -u nullglob
    if (( ${#pidfiles[@]} == 1 )); then
      IFACE="${pidfiles[0]#/run/ath9k-edca-dhcp-}"
      IFACE="${IFACE%.pid}"
    fi
  fi
fi

if [[ ! "${IFACE}" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.:-]{0,14}$ ]]; then
  echo "无效的无线接口名：${IFACE}" >&2
  exit 2
fi

PID_FILE="/run/ath9k-edca-dhcp-${IFACE}.pid"
LEASE_FILE="/run/ath9k-edca-dhcp-${IFACE}.leases"
CIDR_FILE="/run/ath9k-edca-dhcp-${IFACE}.cidr"
LOG_FILE="/var/log/ath9k-dnsmasq-${IFACE}.log"

managed_pid() {
  [[ -r "${PID_FILE}" ]] || return 1
  local pid comm cmdline
  pid="$(cat "${PID_FILE}")"
  [[ "${pid}" =~ ^[0-9]+$ ]] && ((pid > 1)) || return 1
  [[ -r "/proc/${pid}/comm" && -r "/proc/${pid}/cmdline" ]] || return 1
  comm="$(cat "/proc/${pid}/comm")"
  cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline")"
  [[ "${comm}" == dnsmasq && "${cmdline}" == *"--pid-file=${PID_FILE}"* &&
     "${cmdline}" == *"--interface=${IFACE}"* ]]
}

require_root() {
  if (( EUID != 0 )); then
    echo "DHCP 管理需要 root；请用 sudo 运行 ath9k-edca。" >&2
    exit 1
  fi
}

stop_dhcp() {
  require_root
  if managed_pid; then
    local pid waited
    pid="$(cat "${PID_FILE}")"
    kill "${pid}" 2>/dev/null || true
    for ((waited=0; waited<30; waited++)); do
      managed_pid || break
      sleep 0.1
    done
    if managed_pid; then
      kill -KILL "${pid}" 2>/dev/null || true
      sleep 0.1
      if managed_pid; then
        echo "DHCP 进程 ${pid} 未退出；保留 PID 文件供排查。" >&2
        exit 1
      fi
    fi
    echo "已停止 ${IFACE} 的 DHCP (PID ${pid})"
  fi
  rm -f -- "${PID_FILE}" "${CIDR_FILE}"
}

pool_for_ap() {
  python3 - "${AP_IP_CIDR}" <<'PY'
import ipaddress
import sys

try:
    ap = ipaddress.IPv4Interface(sys.argv[1])
    net = ap.network
    first = int(net.network_address) + 1
    last = int(net.broadcast_address) - 1
    address = int(ap.ip)
    if not first <= address <= last or last - first < 1:
        raise ValueError("AP 子网至少需要两个可用主机地址")
    # Preserve the original .100-.200 pool when it fits and excludes the AP.
    preferred = (int(net.network_address) + 100,
                 int(net.network_address) + 200)
    if preferred[1] <= last and not preferred[0] <= address <= preferred[1]:
        start, end = preferred
    else:
        before = (first, address - 1)
        after = (address + 1, last)
        segment = max((before, after), key=lambda pair: pair[1] - pair[0])
        start, end = segment[0], min(segment[1], segment[0] + 100)
    print(f"{ipaddress.IPv4Address(start)},{ipaddress.IPv4Address(end)},{net.netmask},12h")
except ValueError as exc:
    sys.exit(f"无效的 AP IPv4/CIDR 或 DHCP 地址池：{exc}")
PY
}

start_dhcp() {
  require_root
  if ! command -v dnsmasq >/dev/null 2>&1; then
    echo "缺少 dnsmasq；请先在 AP 安装 dnsmasq-base。" >&2
    exit 1
  fi
  if ! ip link show dev "${IFACE}" >/dev/null 2>&1 ||
     ! ip -4 -o addr show dev "${IFACE}" | grep -Fq "inet ${AP_IP_CIDR} "; then
    echo "${IFACE} 尚未配置 ${AP_IP_CIDR}，拒绝启动 DHCP。" >&2
    exit 1
  fi

  local range output
  range="$(pool_for_ap)" || exit 1
  if managed_pid; then
    if [[ -r "${CIDR_FILE}" ]] && [[ "$(cat "${CIDR_FILE}")" == "${AP_IP_CIDR}" ]]; then
      echo "${IFACE} 的 DHCP 已运行 (PID $(cat "${PID_FILE}"))"
      return 0
    fi
    stop_dhcp
  else
    rm -f -- "${PID_FILE}" "${CIDR_FILE}"
  fi

  # Never stop a service that this project did not start. A foreign DHCP
  # listener must be investigated rather than silently replaced.
  if ss -H -lun 2>/dev/null | grep -Eq '(^|[[:space:]]|:)67([[:space:]]|$)'; then
    echo "UDP 67 已被占用；请用 sudo ss -lunp 检查现有 DHCP 服务。" >&2
    exit 1
  fi

  if ! output="$(dnsmasq --conf-file=/dev/null --port=0 \
      --interface="${IFACE}" --bind-interfaces \
      --dhcp-range="${range}" --dhcp-option=3 --dhcp-option=6 \
      --dhcp-leasefile="${LEASE_FILE}" --log-dhcp \
      --log-facility="${LOG_FILE}" --pid-file="${PID_FILE}" 2>&1)"; then
    echo "dnsmasq 启动失败：${output}" >&2
    exit 1
  fi
  local waited
  for ((waited=0; waited<20; waited++)); do
    managed_pid && break
    sleep 0.1
  done
  if ! managed_pid; then
    echo "dnsmasq 已退出或 PID 不属于 ${IFACE}；日志：${LOG_FILE}" >&2
    exit 1
  fi
  printf '%s\n' "${AP_IP_CIDR}" > "${CIDR_FILE}"
  echo "${IFACE} DHCP 已启动：${range}；日志 ${LOG_FILE}"
}

case "${ACTION}" in
  start) start_dhcp ;;
  stop) stop_dhcp ;;
  status)
    if managed_pid; then
      echo "${IFACE} DHCP 运行中 (PID $(cat "${PID_FILE}"))"
    else
      echo "${IFACE} DHCP 未运行"
      exit 1
    fi
    ;;
  *)
    echo "用法：$0 {start|stop|status} [无线接口] [AP_IP/CIDR]" >&2
    exit 2
    ;;
esac
