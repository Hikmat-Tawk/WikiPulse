#!/usr/bin/env bash
# Block outbound 443 with ufw for N seconds, then always restore. Usage: sudo bash firewall_block.sh deny|reject 60
# Manual undo: sudo ufw delete deny out 443; sudo ufw delete reject out 443; sudo ufw disable
set -u
MODE="${1:-deny}"
SECS="${2:-60}"
case "$MODE" in deny|reject) ;; *) echo "mode must be deny or reject"; exit 1 ;; esac
case "$SECS" in ''|*[!0-9]*) echo "seconds must be a whole number, got '$SECS' (paste one command per line)"; exit 1 ;; esac
if [ "$SECS" -lt 10 ] || [ "$SECS" -gt 300 ]; then echo "seconds must be between 10 and 300"; exit 1; fi
if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi

LOG="$(dirname "$0")/../../experiments/captures/firewall_events.log"
mkdir -p "$(dirname "$LOG")"
log() { echo "$(date +%s.%N | cut -c1-14) $1 ${2:-}" | tee -a "$LOG"; }

WAS_ACTIVE=0
ufw status | grep -q "Status: active" && WAS_ACTIVE=1

restore() {
  trap - EXIT INT TERM
  ufw delete "$MODE" out 443 >/dev/null 2>&1
  [ "$WAS_ACTIVE" -eq 0 ] && ufw --force disable >/dev/null 2>&1
  log RESTORE "rule '$MODE out 443' removed; ufw $( [ "$WAS_ACTIVE" -eq 0 ] && echo 'disabled again' || echo 'left active')"
  ufw status verbose
}
trap restore EXIT INT TERM

if [ "$WAS_ACTIVE" -eq 0 ]; then
  ufw --force enable >/dev/null
  log UFW_ENABLED "was inactive"
fi
ufw "$MODE" out 443 >/dev/null
log BLOCK "$MODE out 443 (tcp+udp) for ${SECS}s"
ufw status numbered
for ((i=SECS; i>0; i--)); do printf "\r  blocked, restoring in %3ds (Ctrl+C restores now)" "$i"; sleep 1; done
echo
