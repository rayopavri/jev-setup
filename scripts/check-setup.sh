#!/bin/sh
# check-setup.sh -- is Jev routing + ClinePass ready in this session? Plain-English checks.
#   sh check-setup.sh
set -u
S="$(cd "$(dirname "$0")" && pwd)"
LOG="${JEV_ROUTING_LOG:-$HOME/.config/typesafe/jev-routing.jsonl}"
ok()  { printf '  ✓ %s\n' "$1"; }
bad() { printf '  ✗ %s\n' "$1"; FAILED=1; }
FAILED=0
for d in "$HOME"/.nvm/versions/node/*/bin; do [ -x "$d/pi" ] && PATH="$d:$PATH"; done

echo "Jev routing health check"
[ -n "${TYPESAFE_API_KEY:-}" ] && ok "Jev key is available to this session" || bad "Jev key missing (TYPESAFE_API_KEY in ~/.claude/settings.json env)"
{ command -v jev >/dev/null || [ -x "$HOME/.local/bin/jev" ]; } && ok "Jev tool installed" || ok "Jev tool not installed (direct connection is used instead)"
command -v pi >/dev/null && ok "ClinePass runner (pi) installed" || bad "pi not found under ~/.nvm"
if command -v pi >/dev/null && pi --list-models cline-pass 2>/dev/null | grep -q "qwen3.8-max"; then
  ok "ClinePass models reachable"
else
  bad "ClinePass models not listed (login? run: pi /login)"
fi
if msg=$(python3 "$S/cline_pause.py" status); then bad "$msg"; else ok "ClinePass not paused"; fi
while IFS= read -r line; do
  case "$line" in *stopped*|*"no key"*) bad "Pay-as-you-go backup -- $line" ;; *) ok "Pay-as-you-go backup -- $line" ;; esac
done <<EOF
$(python3 "$S/cline_pause.py" backup | sed '1d; s/^ *//')
EOF
grep -q '"statusLine"' "$HOME/.claude/settings.json" && ok "Status bar configured (terminal only)" || ok "Status bar not configured (optional, not needed for Jev)"
if [ -f "$LOG" ]; then
  last=$(tail -n 50 "$LOG" | grep '"event": "route"' | tail -1 | sed -n 's/.*"ts": "\([^"]*\)".*/\1/p')
  [ -n "$last" ] && ok "Jev last judged a prompt at $last (UTC)" || bad "No recent Jev decisions in the log"
else
  bad "No routing log yet"
fi
grep -q "Jev routing (all projects)" "$HOME/.claude/CLAUDE.md" 2>/dev/null && ok "Global rule to follow Jev is in CLAUDE.md" || bad "Global Jev rule missing from ~/.claude/CLAUDE.md"
[ "$FAILED" -eq 0 ] && echo "All good." || echo "Something needs attention (see ✗ above)."
exit "$FAILED"
