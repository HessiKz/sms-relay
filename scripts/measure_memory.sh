#!/usr/bin/env bash
# Report memory usage of the relay stack and the machine.
#
# Usage: sudo scripts/measure_memory.sh
set -euo pipefail

SERVICE=sms-relay-api.service

echo "=== system ==="
free -h
echo
echo "=== swap ==="
swapon --show || true
echo
echo "=== top memory consumers ==="
ps aux --sort=-%mem | head -10
echo
echo "=== relay service ==="
if systemctl is-active --quiet "$SERVICE"; then
  pid=$(systemctl show -p MainPID --value "$SERVICE")
  echo "main pid: $pid"
  ps -o pid,rss,vsz,etime,cmd -p "$pid" || true
  rss_kb=$(ps -o rss= -p "$pid" | tr -d ' ')
  if [ -n "$rss_kb" ]; then
    echo "RSS: $((rss_kb / 1024)) MB"
  fi
  systemctl show "$SERVICE" -p MemoryCurrent -p MemoryHigh -p MemoryMax -p MemoryPeak || true
else
  echo "service not running"
fi
echo
echo "=== caddy ==="
cpid=$(pgrep -x caddy | head -1 || true)
if [ -n "$cpid" ]; then
  ps -o pid,rss,etime,cmd -p "$cpid" || true
fi
echo
echo "=== recent kernel OOM events ==="
journalctl -k --since "24 hours ago" 2>/dev/null | grep -i -e oom -e "killed process" || echo "(none)"
