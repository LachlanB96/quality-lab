#!/usr/bin/env bash
# Leak check for this public repo. Blocks two kinds of leak:
#   - secrets (API keys, tokens, private keys), found by gitleaks
#   - personal data, found by matching a private blocklist
#
# Usage:
#   leak-check.sh --staged       the next commit's files, paths and author (pre-commit hook)
#   leak-check.sh --msg <file>   a commit message (commit-msg hook)
#   leak-check.sh --all          every commit's files, paths, message and author (pre-push hook, CI)
#
# The blocklist has one string per line, matched case-insensitively. Blank lines and lines
# starting with # are ignored. It is never stored in this repo:
#   locally: $QL_BLOCKLIST_FILE, or ~/.config/quality-lab/blocklist.txt
#   in CI:   the QL_BLOCKLIST repository secret, written to a temp file by the workflow
#
# A public commit identity can contain a blocklisted string, like the username in a GitHub
# noreply email. The optional allowed identities list names those identities, one
# "Name <email>" per line, each matching a whole identity case-insensitively. It exempts commit
# authors and committers only, never file contents, paths or messages. Same format rules, and
# also never stored in this repo:
#   locally: $QL_ALLOWED_IDENTITIES_FILE, or ~/.config/quality-lab/allowed-identities.txt
#   in CI:   the QL_ALLOWED_IDENTITIES repository secret
#
# Output names commits and files but never the matched text, because CI logs on a public
# repo are public too. Any error while matching counts as a match, so the check fails closed.

set -eu

mode="${1:-}"
blocklist="${QL_BLOCKLIST_FILE:-$HOME/.config/quality-lab/blocklist.txt}"
allowlist="${QL_ALLOWED_IDENTITIES_FILE:-$HOME/.config/quality-lab/allowed-identities.txt}"
failed=0

usage() {
  echo "usage: leak-check.sh --staged | --msg <file> | --all" >&2
  exit 2
}

fail() {
  echo "leak-check: $*" >&2
  failed=1
}

case "$mode" in
  --staged | --all) ;;
  --msg) [ -n "${2:-}" ] || usage ;;
  *) usage ;;
esac

# Copies a list's entries to a file, trimmed and lowercased (see is_personal), without blank
# lines or comments. A missing list gives an empty file.
load_list() {
  if [ -f "$1" ]; then
    tr -d '\r' <"$1" |
      sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' |
      grep -v -e '^$' -e '^#' |
      tr '[:upper:]' '[:lower:]' >"$2" || true
  fi
}

patterns="$(mktemp)"
identities="$(mktemp)"
trap 'rm -f "$patterns" "$identities"' EXIT
load_list "$blocklist" "$patterns"
load_list "$allowlist" "$identities"
if [ ! -s "$patterns" ]; then
  echo "leak-check: no blocklist entries in $blocklist" >&2
  echo "  Add one personal string per line: names, emails, usernames, machine names, employer." >&2
  exit 1
fi

# Succeeds if stdin contains a blocklisted string. Lowercases the input rather than using
# grep -i, which crashes in some Git for Windows builds. A grep error counts as a match.
is_personal() {
  local status=0
  tr '[:upper:]' '[:lower:]' | grep -q -F -f "$patterns" || status=$?
  if [ "$status" -gt 1 ]; then
    echo "leak-check: grep failed (exit $status), so this counts as a match." >&2
  fi
  [ "$status" != 1 ]
}

# Succeeds if stdin, one "Name <email>" per line, has a personal identity. An identity on the
# allowed identities list is skipped: an exact string comparison, not a grep.
is_personal_identity() {
  local ident entry allowed
  while IFS= read -r ident; do
    ident="$(printf '%s' "$ident" | tr '[:upper:]' '[:lower:]')"
    allowed=0
    while IFS= read -r entry; do
      if [ "$ident" = "$entry" ]; then
        allowed=1
        break
      fi
    done <"$identities"
    if [ "$allowed" = 0 ] && printf '%s\n' "$ident" | is_personal; then
      return 0
    fi
  done
  return 1
}

# Prints a path, unless the path itself is personal.
show_path() {
  if printf '%s\n' "$1" | is_personal; then
    echo "  (a file whose path contains a blocklisted string)" >&2
  else
    echo "  $1" >&2
  fi
}

run_gitleaks() {
  if ! command -v gitleaks >/dev/null 2>&1; then
    fail "gitleaks is not installed, so secrets were not checked. See https://github.com/gitleaks/gitleaks#installing"
    return
  fi
  gitleaks "$@" --redact --no-banner || fail "gitleaks found a possible secret (details above, redacted)."
}

check_identity() {
  # Each ident is "Name <email> 1700000000 +1000". Drop the timestamp before matching.
  if { git var GIT_AUTHOR_IDENT; git var GIT_COMMITTER_IDENT; } 2>/dev/null |
    sed 's/ [0-9]* [-+][0-9]*$//' | is_personal_identity; then
    fail "the commit author or committer name or email is personal. Set a public identity for this repo:"
    echo '    git config user.name "<github-username>"' >&2
    echo '    git config user.email "<id>+<github-username>@users.noreply.github.com"' >&2
    echo '  Your noreply address is listed at https://github.com/settings/emails' >&2
    echo "  If a noreply identity is blocked only because its username is blocklisted, add it as" >&2
    echo "  \"Name <email>\" to $allowlist" >&2
  fi
}

check_staged() {
  local hits path status=0
  # Contents as they will be committed: the index, not the working tree.
  # git grep has its own matcher, so -i is safe here. Exit 1 means no match.
  hits="$(git grep --cached -I -l -i -F -f "$patterns")" || status=$?
  if [ "$status" -gt 1 ]; then
    fail "git grep failed (exit $status), so staged file contents were not checked."
  elif [ -n "$hits" ]; then
    fail "staged files contain blocklisted strings. To see the lines: git grep --cached -n -i -F -f \"$blocklist\""
    while IFS= read -r path; do show_path "$path"; done <<<"$hits"
  fi
  if git ls-files --cached | is_personal; then
    fail "a staged file path contains a blocklisted string."
  fi
  check_identity
  run_gitleaks git --pre-commit --staged
}

check_msg() {
  # Ignore git's comment lines, and the diff below the scissors line that `commit -v` adds.
  if sed -e '/^# -* >8 -*$/,$d' -e '/^#/d' "$1" | is_personal; then
    fail "the commit message contains a blocklisted string."
  fi
}

check_all() {
  local rev short hits path status count=0
  for rev in $(git rev-list --all); do
    short="$(git rev-parse --short "$rev")"
    count=$((count + 1))
    if git log -1 --format='%an <%ae>%n%cn <%ce>' "$rev" | is_personal_identity; then
      fail "commit $short: the author or committer name or email is personal."
    fi
    if git log -1 --format=%B "$rev" | is_personal; then
      fail "commit $short: the message contains a blocklisted string."
    fi
    if git ls-tree -r --name-only "$rev" | is_personal; then
      fail "commit $short: a file path contains a blocklisted string."
    fi
    status=0
    hits="$(git grep -I -l -i -F -f "$patterns" "$rev" --)" || status=$?
    if [ "$status" -gt 1 ]; then
      fail "commit $short: git grep failed (exit $status), so file contents were not checked."
    elif [ -n "$hits" ]; then
      fail "commit $short: files contain blocklisted strings:"
      while IFS= read -r path; do show_path "${path#"$rev":}"; done <<<"$hits"
    fi
  done
  echo "leak-check: checked $count commit(s) against the blocklist."
  run_gitleaks git
}

case "$mode" in
  --staged) check_staged ;;
  --msg) check_msg "$2" ;;
  --all) check_all ;;
esac

if [ "$failed" != 0 ]; then
  echo "leak-check: blocked. Remove the secret or personal data, then try again." >&2
  exit 1
fi
