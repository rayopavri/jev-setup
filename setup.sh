#!/bin/sh
# One command for the whole Jev setup. See setup/jev_setup.py, or run with no arguments.
#   ./setup.sh install | check | sync | publish   [--dry-run]
exec python3 "$(dirname "$0")/setup/jev_setup.py" "$@"
