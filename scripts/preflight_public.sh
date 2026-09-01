#!/usr/bin/env bash
# Check that nothing personal is about to be published.
#
#   ./scripts/preflight_public.sh
#
# Run this before making the repository public, and before any push that touched
# config.yaml or the docs. Exits non-zero if anything looks like a real contact
# detail or credential in a file git would upload.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0
note() { printf '  %s\n' "$1"; }

echo "== Files git would publish =="
if git rev-parse --git-dir >/dev/null 2>&1; then
  files=$(git ls-files --cached --others --exclude-standard)
else
  echo "  (not a git repo yet; scanning the working tree)"
  files=$(find . -type f -not -path './.git/*' -not -path './data/*' \
          -not -path '*/__pycache__/*' -not -path './.pytest_cache/*' | sed 's|^\./||')
fi

echo
echo "== .env must never be published =="
if printf '%s\n' "$files" | grep -qx ".env"; then
  note "FAIL: .env is not ignored"; fail=1
else
  note "ok: .env is absent or ignored"
fi

echo
echo "== Email addresses =="
hits=$(printf '%s\n' "$files" | xargs -r grep -InE \
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' 2>/dev/null \
  | grep -vE 'example\.com|example\.org|you@gmail\.com|users\.noreply\.github\.com|\$\{' || true)
if [[ -n "$hits" ]]; then
  note "FAIL: real-looking email addresses found:"; printf '%s\n' "$hits" | sed 's/^/    /'; fail=1
else
  note "ok: only placeholder addresses"
fi

echo
echo "== Phone numbers =="
hits=$(printf '%s\n' "$files" | xargs -r grep -InE '\+[0-9]{7,15}' 2>/dev/null \
  | grep -vE '\+49X|\+15551234567|\+491700000000|\+4900|\$\{' || true)
if [[ -n "$hits" ]]; then
  note "FAIL: real-looking phone numbers found:"; printf '%s\n' "$hits" | sed 's/^/    /'; fail=1
else
  note "ok: only placeholder numbers"
fi

echo
echo "== Credentials =="
hits=$(printf '%s\n' "$files" | xargs -r grep -InE \
  'AC[0-9a-f]{32}|SK[0-9a-f]{32}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|xox[baprs]-' 2>/dev/null || true)
if [[ -n "$hits" ]]; then
  note "FAIL: possible live credential:"; printf '%s\n' "$hits" | sed 's/^/    /'; fail=1
else
  note "ok: no credential patterns"
fi

echo
echo "== Git history =="
if git rev-parse --git-dir >/dev/null 2>&1 && git log -1 >/dev/null 2>&1; then
  if git log -p --all 2>/dev/null | grep -qE '^\+.*(GMAIL_APP_PASSWORD=.+[A-Za-z]|AC[0-9a-f]{32})'; then
    note "FAIL: a secret appears in git history. Making the repo public would expose it"
    note "      even after deletion. Rewrite history or start a fresh repository."
    fail=1
  else
    note "ok: no secrets found in history"
  fi
else
  note "ok: no commits yet, nothing in history"
fi

echo
if [[ $fail -eq 0 ]]; then
  echo "PASS - safe to publish."
else
  echo "BLOCKED - fix the items above before making this repository public."
fi
exit $fail
