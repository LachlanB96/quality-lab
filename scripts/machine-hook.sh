#!/usr/bin/env bash
# Runs the machine's own git hook of the same name, for a clone that asks for it.
#
# Usage: machine-hook.sh <hook> [hook args...]   (stdin is passed through, for pre-push)
#
# This repo sets core.hooksPath to .githooks, which replaces any hooks folder the machine sets in
# its global git config, so those hooks would never run here. A clone with namemask.mode set in its
# git config needs them: each hook in .githooks calls this first, and the machine's hook runs before
# the leak check. If that hook can't be found, the commit or push is blocked. A clone without
# namemask.mode (CI, anyone else's) runs the leak check alone.

set -eu

hook="${1:?usage: machine-hook.sh <hook> [args...]}"
shift

[ -n "$(git config --get namemask.mode || true)" ] || exit 0

dir="$(git config --global --includes --get core.hooksPath || true)"
if [ -z "$dir" ]; then
  echo "$hook: namemask.mode is set but the global git config has no core.hooksPath. Blocked." >&2
  exit 1
fi
if [ ! -f "$dir/$hook" ]; then
  echo "$hook: namemask.mode is set but the machine's $hook hook is missing from $dir. Blocked." >&2
  exit 1
fi
exec sh "$dir/$hook" "$@"
