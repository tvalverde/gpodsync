#!/usr/bin/env bash
#
# The check on the checker.
#
# audit.sh has now had five bugs of one shape: it reported ok having examined
# nothing. A subshell swallowed its failure count; a filesystem check stood in for
# a tracked check; a missing .git made every tracked-file test vacuous; `git show`
# rendered binary blobs as a one-line summary; grep's error status was read as
# "no match". None of them were visible from a passing run.
#
# So every mode gets a case here, and each one plants something and demands a
# non-zero exit. A case that stops failing when its fix is reverted is the only
# evidence that the fix is load-bearing.
#
# Usage: audit_self_test.sh /path/to/audit.sh

set -uo pipefail

AUDIT="${1:?path to audit.sh required}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Work from the throwaway directory, never from the repository under test. A bug
# in this file once left a fixture path empty, and `git -C "" add -f -A` therefore
# ran against the real repository — staging the virtualenv and the untracked
# deployment file, and committing them. Standing here means the worst a future
# slip can do is damage a temporary directory.
cd "$TMP" || exit 1

FAILURES=0
SECRET="secret-host.example.net"

git_quiet_in() {
    local repo="$1"; shift
    require_repo "$repo"
    git -C "$repo" -c user.email=t@example.com -c user.name=Test "$@" >/dev/null 2>&1
}

new_repo() {
    local name path
    name="$1"
    path="$TMP/$name"
    mkdir -p "$path"
    git -c init.defaultBranch=main init -q "$path"
    printf '*\n' >"$path/.dockerignore"
    # A global gitignore may exclude .dockerignore; without this the fixture would
    # reproduce that problem instead of testing what it means to test.
    printf '!.dockerignore\n' >"$path/.gitignore"
    echo "$path"
}

# Every fixture path passes through here. An empty or non-repository path aborts
# the whole run rather than being handed to git, which would then operate on
# whatever directory happened to be current.
require_repo() {
    if [ -z "${1:-}" ] || [ ! -d "${1:-}/.git" ]; then
        echo "audit --self-test: ABORTED — a fixture repository could not be created."
        echo "                   Refusing to run git commands with no target."
        exit 1
    fi
}

run_audit() {
    local repo="$1" patterns="$2"; shift 2
    require_repo "$repo"
    (cd "$repo" && AUDIT_FORBIDDEN_STRINGS="$patterns" bash "$AUDIT" "$@" 2>&1)
}

git_in() {
    local repo="$1"; shift
    require_repo "$repo"
    git -C "$repo" "$@"
}

expect() {
    local description="$1" expected="$2" actual="$3" output="$4"
    if [ "$actual" -ne "$expected" ]; then
        echo "  FAILED  $description"
        echo "          exit $actual, expected $expected"
        echo "$output" | sed 's/^/          /'
        FAILURES=$((FAILURES + 1))
    fi
}

expect_says() {
    local description="$1" needle="$2" output="$3"
    if ! printf '%s' "$output" | grep -qF -- "$needle"; then
        echo "  FAILED  $description (no '$needle' in the output)"
        echo "$output" | sed 's/^/          /'
        FAILURES=$((FAILURES + 1))
    fi
}

# --- A planted secret in staged text -----------------------------------------

repo=$(new_repo staged-text)
printf 'deploy target: %s\n' "$SECRET" >"$repo/notes.md"
git_in "$repo" add -A
out=$(run_audit "$repo" "$SECRET" --staged); code=$?
expect "a staged secret is caught" 1 "$code" "$out"
expect_says "the matching pattern is named by index" "pattern #1" "$out"
if printf '%s' "$out" | grep -qF -- "$SECRET"; then
    echo "  FAILED  the secret itself was printed to the log"
    FAILURES=$((FAILURES + 1))
fi

# --- A clean tree really is clean ---------------------------------------------

repo=$(new_repo clean)
printf 'nothing to see\n' >"$repo/readme.md"
git_in "$repo" add -A
out=$(run_audit "$repo" "no-such-string-anywhere" --staged); code=$?
expect "a clean staged diff passes" 0 "$code" "$out"

# --- A secret inside a binary --------------------------------------------------

repo=$(new_repo staged-binary)
printf 'header\000%s\000trailer' "$SECRET" >"$repo/blob.bin"
git_in "$repo" add -A
out=$(run_audit "$repo" "$SECRET" --staged); code=$?
expect "a secret inside a staged binary is caught" 1 "$code" "$out"

# --- A secret in a file name, with innocent contents ---------------------------

repo=$(new_repo tree-filename)
printf 'entirely innocuous\n' >"$repo/backup-of-$SECRET.txt"
git_in "$repo" add -A
out=$(run_audit "$repo" "$SECRET" --tree); code=$?
expect "a secret in a file name is caught" 1 "$code" "$out"

# --- A secret committed and then deleted ---------------------------------------

repo=$(new_repo history-deleted)
printf 'x\000%s\000y' "$SECRET" >"$repo/dump.bin"
git_quiet_in "$repo" add -A
git_quiet_in "$repo" commit -m "add a dump"
git_quiet_in "$repo" rm -q "dump.bin"
git_quiet_in "$repo" commit -m "remove it again"
out=$(run_audit "$repo" "$SECRET" --history); code=$?
expect "a secret deleted in a later commit is still found in history" 1 "$code" "$out"

# --- A secret in a commit message ----------------------------------------------

repo=$(new_repo history-message)
printf 'ordinary\n' >"$repo/file.txt"
git_quiet_in "$repo" add -A
git_quiet_in "$repo" commit -m "deploy to $SECRET"
out=$(run_audit "$repo" "$SECRET" --history); code=$?
expect "a secret in a commit message is caught" 1 "$code" "$out"

# --- Structural: a tracked environment file -------------------------------------

repo=$(new_repo tracked-env)
printf 'TOKEN=abc\n' >"$repo/.env"
git_in "$repo" add -f -A
out=$(run_audit "$repo" "irrelevant" --staged); code=$?
expect "a tracked .env fails even with no matching pattern" 1 "$code" "$out"

# --- Structural: .dockerignore that stopped being an allowlist -------------------

repo=$(new_repo blocklist)
printf '# a comment\nnode_modules\n*\n' >"$repo/.dockerignore"
printf 'ok\n' >"$repo/file.txt"
git_in "$repo" add -A
out=$(run_audit "$repo" "irrelevant" --staged); code=$?
expect "a .dockerignore whose first pattern is not '*' fails" 1 "$code" "$out"

# --- The git guard ---------------------------------------------------------------

mkdir -p "$TMP/not-a-repo"
out=$(cd "$TMP/not-a-repo" && AUDIT_FORBIDDEN_STRINGS="$SECRET" bash "$AUDIT" --tree 2>&1)
code=$?
expect "running outside a git work tree fails" 1 "$code" "$out"

# --- --require-list ---------------------------------------------------------------

repo=$(new_repo no-list)
printf 'ok\n' >"$repo/file.txt"
git_in "$repo" add -A
out=$(run_audit "$repo" "" --tree --require-list); code=$?
expect "--require-list with no list fails" 1 "$code" "$out"
out=$(run_audit "$repo" "" --tree); code=$?
expect "no list without --require-list still runs the structural checks" 0 "$code" "$out"

# --- Pattern numbering beyond the first --------------------------------------------

repo=$(new_repo third-pattern)
printf 'mentions %s here\n' "$SECRET" >"$repo/notes.md"
git_in "$repo" add -A
out=$(run_audit "$repo" "$(printf 'alpha\nbeta\n%s\ndelta' "$SECRET")" --staged); code=$?
expect "a match on the third pattern is caught" 1 "$code" "$out"
expect_says "the index reported is the third, not the first" "pattern #3" "$out"

# --- A carriage return must not blind the match --------------------------------------

repo=$(new_repo crlf)
printf 'deploy target: %s\n' "$SECRET" >"$repo/notes.md"
git_in "$repo" add -A
out=$(run_audit "$repo" "$(printf '%s\r' "$SECRET")" --staged); code=$?
expect "a pattern list with CRLF line endings still matches" 1 "$code" "$out"

# --- An empty staged diff is an ordinary state, not a broken scan ----------------

repo=$(new_repo empty-staged)
printf 'ok\n' >"$repo/file.txt"
git_in "$repo" add -A >/dev/null 2>&1
git_quiet_in "$repo" commit -m "initial"
out=$(run_audit "$repo" "$SECRET" --staged); code=$?
expect "staging nothing passes rather than failing" 0 "$code" "$out"

# --- Verdict ---------------------------------------------------------------------

if [ "$FAILURES" -gt 0 ]; then
    echo "audit --self-test: $FAILURES case(s) FAILED"
    exit 1
fi
echo "audit --self-test: ok (14 cases: every mode detects, exits non-zero, and never echoes the secret)"
