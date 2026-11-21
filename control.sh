#!/usr/bin/env bash
# Operational controls for the cloud monitor (needs the GitHub CLI, `gh`).
#   ./control.sh check        run a stock check now
#   ./control.sh status       latest saved inventory status
#   ./control.sh history      recent workflow runs
#   ./control.sh logs         output of the most recent run
#   ./control.sh test         send a Telegram test notification
#   ./control.sh summary      send the daily health summary now
#   ./control.sh pause | resume
#   ./control.sh price 119.99 change the maximum price
#   ./control.sh disable      stop the schedule permanently (re-enable: gh workflow enable monitor.yml)
set -euo pipefail
REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
WF=monitor.yml
case "${1:-}" in
  check)   gh workflow run "$WF" -R "$REPO" -f mode=check   && echo "Check started. ./control.sh logs in ~1 min." ;;
  test)    gh workflow run "$WF" -R "$REPO" -f mode=test-notify && echo "Test notification requested." ;;
  summary) gh workflow run "$WF" -R "$REPO" -f mode=summary && echo "Summary requested." ;;
  status)
    gh api "repos/$REPO/contents/state.json?ref=state" -q .content | base64 --decode | python3 -c '
import json,sys
s=json.load(sys.stdin)
print("last run:", s.get("last_run"))
for k,v in s.get("retailers",{}).items():
    print()
    print(k + ":", v["state"], " price=%s  checked=%s  last_verified=%s" % (v.get("price"), v.get("last_checked"), v.get("last_healthy")))
    print("  ", v.get("reason"))
    for c in v.get("channels",[]):
        print("   %-9s%-14s local=%s  %s" % (c["method"], c["status"], c["verified"], c["where"]))
print("\nalerts sent:", s.get("stats",{}).get("alerts_sent",0))' ;;
  history) gh run list -R "$REPO" -w "$WF" -L "${2:-20}" ;;
  logs)    id=$(gh run list -R "$REPO" -w "$WF" -L 1 --json databaseId -q '.[0].databaseId'); gh run view "$id" -R "$REPO" --log | grep -E "monitor \(|\[Walmart\]|\[Sam's|    (SHIPPING|PICKUP|DELIVERY)|telegram|WARNING|ERROR" | sed 's/^.*Z //' ;;
  pause)   gh variable set MONITOR_PAUSED -R "$REPO" -b true  && echo "Paused (scheduled runs skip)." ;;
  resume)  gh variable set MONITOR_PAUSED -R "$REPO" -b false && echo "Resumed." ;;
  price)   [[ "${2:-}" =~ ^[0-9]+(\.[0-9]{1,2})?$ ]] || { echo "usage: $0 price 128"; exit 1; }
           gh variable set MAX_PRICE -R "$REPO" -b "$2" && echo "Max price set to \$$2 (applies from the next run)." ;;
  disable) gh workflow disable "$WF" -R "$REPO" && echo "Monitoring disabled. Delete the repo to remove everything." ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
