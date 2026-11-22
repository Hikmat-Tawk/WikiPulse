#!/usr/bin/env bash
# Run the four firewall resilience tests (API deny/reject, BS4 deny, Selenium deny). Usage: bash phase4_auto.sh
# Emergency undo: sudo ufw delete deny out 443; sudo ufw delete reject out 443; sudo ufw disable
set -u
cd "$(dirname "$0")/../.."
PY="$HOME/.venvs/wikipulse/bin/python"
CAP=experiments/captures
mkdir -p "$CAP"
if [ "$(id -u)" -eq 0 ]; then echo "Run as your normal user, not with sudo."; exit 1; fi

echo "== sudo is needed only for ufw; enter your password once:"
sudo -v || exit 1
( while true; do sudo -n true 2>/dev/null; sleep 50; done ) &   # keep the sudo ticket alive
KEEP_PID=$!
CHILDREN=()
cleanup() {
  kill "$KEEP_PID" 2>/dev/null
  for p in "${CHILDREN[@]}"; do kill "$p" 2>/dev/null; done
  wait 2>/dev/null
  echo; echo "== final firewall state:"; sudo ufw status
}
trap cleanup EXIT

STAMP=$(date +%H%M)
IDS=()

run_test() {  # $1 method  $2 mode(deny|reject)  $3 extra runner args
  local method=$1 mode=$2 extra=$3
  local id="fw2_${mode}_${method}_${STAMP}"
  local csv="experiments/metrics/${id}.resilience.csv"
  IDS+=("$id")
  echo; echo "################ TEST: ${method} + ${mode}  (id ${id}) ################"
  "$PY" -u -m src.benchmarking.resilience_runner --method "$method" --id "$id" $extra 2>&1 | tee "$CAP/${id}.log" &
  local runner=$!
  CHILDREN+=("$runner")
  local waited=0 ok=0
  while [ "$waited" -lt 180 ]; do
    ok=$(grep -c ',ok,' "$csv" 2>/dev/null); ok=${ok:-0}
    [ "$ok" -ge 2 ] && break
    sleep 2; waited=$((waited + 2))
  done
  if [ "$ok" -lt 2 ]; then echo "!! fewer than 2 successful collections after 180 s - skipping the block for this test"; wait "$runner"; return; fi
  echo ">> 2 collections ok -> blocking outbound 443 (${mode}) for 60 s"
  ( for i in $(seq 1 30); do echo "== $(date +%T)"; ss -tanp | grep -E 'python|chrome'; sleep 2; done ) > "$CAP/ss_${id}.txt" &
  local sslog=$!
  CHILDREN+=("$sslog")
  sudo bash src/networking/firewall_block.sh "$mode" 60
  wait "$sslog" 2>/dev/null
  echo ">> block over; scraper keeps running to show recovery..."
  wait "$runner"
}

run_test api deny ""
run_test api reject ""
run_test bs4 deny ""
run_test selenium deny "--duration 300 --interval 30"

echo; echo "################ SUMMARY ################"
"$PY" -m src.benchmarking.resilience_summary "${IDS[@]}"
echo; echo "SYN-SENT seen in ss logs:"; grep -c "SYN-SENT" "$CAP"/ss_fw2_*_"${STAMP}".txt
