#!/usr/bin/env bash
# Threads 1 vs 4 on the real API while logging python's connections with ss.
set -u
cd "$(dirname "$0")/../.."
PY="$HOME/.venvs/wikipulse/bin/python"
mkdir -p experiments/captures
SS_LOG=experiments/captures/ss_threads_api.txt
( while true; do echo "== $(date +%s.%N | cut -c1-14) $(date +%T)"; ss -tanp | grep python; sleep 0.5; done ) > "$SS_LOG" &
SS_PID=$!
trap 'kill $SS_PID 2>/dev/null' EXIT
"$PY" -u -m src.benchmarking.threads_experiment --target api --workers 1,4 --windows 12 --repeats 2
echo "ss log -> $SS_LOG"
