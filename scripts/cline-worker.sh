#!/bin/sh
# Hand one self-contained coding task to an open-model lane: ClinePass or Codex, the
# other free lane when one is down, and pay-as-you-go (OpenRouter) only when both are.
#
#   cline-worker.sh <light|standard> "<task>" [model] [--escalate]
#
# The ladder lives in lane-worker.py; this wrapper keeps the command Jev and
# delegate.py hand out. Exit 0 done, 2 usage, 4 attempts failed, 75 nothing could take it.
exec python3 "$(dirname "$0")/lane-worker.py" "$@" < /dev/null
