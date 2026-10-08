#!/bin/sh
# Verify that ClinePass models can actually use pi's tools before they carry work.
#
#   check-tools.sh [model ...]     (default: the four lane models)
#   check-tools.sh --backup [cline|openrouter]   (pay-as-you-go models; roughly 30 cents)
#
# Each model gets a scratch folder and must read a file, run a command, and write
# the combined answer to disk. PASS only if the file on disk is exactly right.

for d in "$HOME"/.nvm/versions/node/*/bin; do
  [ -x "$d/pi" ] && PATH="$d:$PATH"
done
export PATH

if [ "${1:-}" = "--backup" ]; then
  set -- $(python3 "$(dirname "$0")/cline_pause.py" backup-models "${2:-}")
fi
MODELS="${*:-cline-pass/qwen3.8-max cline-pass/qwen3.7-max cline-pass/deepseek-v4-pro cline-pass/kimi-k3}"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/cline-toolcheck.XXXXXX")"

for m in $MODELS; do
  dir="$ROOT/$(echo "$m" | tr '/' '_')"
  mkdir -p "$dir"
  uuidgen | cut -c1-8 > "$dir/input.txt"
  # A ClinePass model goes through the clinepass provider; a backup one names its own.
  case "$m" in cline-pass/*) pm="clinepass/$m" ;; *) pm="$m" ;; esac
  ( cd "$dir" && perl -e 'alarm shift; exec @ARGV' 240 \
      pi --model "$pm" --thinking low -p "Use your tools. 1) Read the file input.txt. 2) Run the shell command: echo tool-ok. 3) Write a file out.txt whose single line is the contents of input.txt followed by a space and the output of that command. Then reply DONE." \
      > pi.log 2>&1 ) &
done
wait

fail=0
for m in $MODELS; do
  dir="$ROOT/$(echo "$m" | tr '/' '_')"
  want="$(cat "$dir/input.txt") tool-ok"
  got="$(cat "$dir/out.txt" 2>/dev/null)"
  if [ "$got" = "$want" ]; then echo "PASS  $m"; else echo "FAIL  $m  (log: $dir/pi.log)"; fail=1; fi
done
exit $fail
