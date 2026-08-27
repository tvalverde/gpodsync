#!/usr/bin/env bash
#
# Fail if anything private would reach a public artefact.
#
# The repository and the published image are public and git history is permanent,
# so this runs from the first commit rather than as a review before release.
#
# Two kinds of check:
#
#   Structural  — always run, need no configuration. Catches the categories of
#                 mistake that are recognisable by shape: a committed .env, a
#                 database, a private key.
#   Substring   — needs a list of strings that are private to this deployment:
#                 the server's address, its hostnames, its paths. That list is
#                 itself private, so it is never stored in the repository.
#
# The list comes from AUDIT_FORBIDDEN_STRINGS, one entry per line, supplied by a
# CI secret or by the untracked .env.deploy. Without it only the structural checks
# run — which is right for a fork or an outside contributor, who have no such
# strings to leak — but --require-list makes its absence a hard failure, so the
# release workflow cannot silently degrade into a no-op.
#
# A match is reported by location and by the index of the pattern that matched,
# never by the matched text. Printing it would copy the private string into a CI
# log that, on a public repository, anyone can read.
#
# EVERY check here fails closed. That is not a style preference: this script has
# had five separate bugs whose shape was "reports ok having examined nothing",
# and a gate that cannot fail is indistinguishable from one that works until the
# day it matters. --self-test exists to keep proving otherwise.
#
# Usage: audit.sh [--staged | --tree | --history] [--image REF] [--require-list]

set -uo pipefail

MODE=tree
IMAGE=""
REQUIRE_LIST=0
FAILURES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --staged)       MODE=staged ;;
        --tree)         MODE=tree ;;
        --history)      MODE=history ;;
        --image)        IMAGE="${2:?--image needs an image reference}"; shift ;;
        --require-list) REQUIRE_LIST=1 ;;
        --self-test)    MODE=self-test ;;
        -h|--help)      sed -n '2,34p' "$0"; exit 0 ;;
        *)              echo "audit: unknown argument '$1'" >&2; exit 2 ;;
    esac
    shift
done

if [ "$MODE" = "self-test" ]; then
    exec bash "$(dirname "$0")/audit_self_test.sh" "$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
fi

# Fail closed if git is unusable. Without this, `git ls-files` returns nothing and
# every "is anything private tracked?" check reports ok having examined zero files
# — a vacuous pass. Not hypothetical: act's checkout hands the job a directory
# with no .git in it, and this script cheerfully approved it.
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "audit: FAIL — not inside a git working tree."
    echo "       Every tracked-file check would pass without examining anything."
    exit 1
fi

cd "$(git rev-parse --show-toplevel)" || exit 2

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fail() { echo "  FAIL  $*"; FAILURES=$((FAILURES + 1)); }
pass() { echo "  ok    $*"; }

# --- The private-string list -------------------------------------------------

if [ -z "${AUDIT_FORBIDDEN_STRINGS:-}" ] && [ -f .env.deploy ]; then
    # shellcheck disable=SC1091
    . ./.env.deploy
fi

PATTERN_FILE="$WORK/patterns"
: >"$PATTERN_FILE"
if [ -n "${AUDIT_FORBIDDEN_STRINGS:-}" ]; then
    # A trailing carriage return matches nothing, ever. The list is pasted into a
    # GitHub secrets box or edited on whatever machine is to hand, so one CRLF
    # would leave the audit green and completely blind.
    printf '%s\n' "$AUDIT_FORBIDDEN_STRINGS" \
        | tr -d '\r' \
        | grep -v '^[[:space:]]*$' >"$PATTERN_FILE"
fi
PATTERN_COUNT=$(wc -l <"$PATTERN_FILE" | tr -d ' ')

echo "audit: mode=$MODE patterns=$PATTERN_COUNT${IMAGE:+ image=$IMAGE}"

if [ "$PATTERN_COUNT" -eq 0 ]; then
    if [ "$REQUIRE_LIST" -eq 1 ]; then
        echo "  FAIL  no forbidden-string list, and --require-list was given"
        echo "        Set the AUDIT_FORBIDDEN_STRINGS secret. Refusing to publish"
        echo "        with the substring check silently disabled."
        exit 1
    fi
    echo "  note  no forbidden-string list; running structural checks only"
fi

# --- Structural checks -------------------------------------------------------

leaky_env=$(git ls-files | grep -E '(^|/|\.)[^/]*\.env(\.|$)|(^|/)\.env(\.|$)' \
    | grep -v '\.env\.example$' || true)
if [ -n "$leaky_env" ]; then
    fail "environment files are tracked:"; echo "$leaky_env" | sed 's/^/          /'
else
    pass "no environment file is tracked"
fi

db_files=$(git ls-files | grep -E '\.sqlite3?(-wal|-shm)?$|\.db$' || true)
if [ -n "$db_files" ]; then
    fail "a database is tracked (it holds password hashes and session keys):"
    echo "$db_files" | sed 's/^/          /'
else
    pass "no database is tracked"
fi

key_files=$(git ls-files \
    | grep -E '(^|/)(id_(rsa|dsa|ecdsa|ed25519))$|\.(pem|key|p12|pfx|ppk|jks|keystore)$' || true)
if [ -n "$key_files" ]; then
    fail "key material is tracked:"; echo "$key_files" | sed 's/^/          /'
else
    pass "no key material is tracked"
fi

# Tracked, not merely present. Checking the filesystem was an earlier mistake: a
# global gitignore excluded .dockerignore, so the file existed locally, the check
# passed, and the thing that keeps the image's build context fail-closed would
# never have reached the repository or CI.
for required in .gitignore .dockerignore; do
    if ! [ -f "$required" ]; then
        fail "$required is missing — the build context is unbounded without it"
    elif git ls-files --error-unmatch "$required" >/dev/null 2>&1; then
        pass "$required is tracked"
    else
        fail "$required exists but is not tracked (check your global gitignore)"
    fi
done

if [ -f .dockerignore ]; then
    if [ "$(grep -vE '^\s*(#|$)' .dockerignore | head -1)" = "*" ]; then
        pass ".dockerignore is still an allowlist"
    else
        fail ".dockerignore no longer starts with a bare '*' — it is a blocklist now"
    fi
fi

# --- Substring checks --------------------------------------------------------

# Takes a file, never a pipe: as the right-hand side of a pipeline this ran in a
# subshell, so its FAILURES increments were discarded and the script printed its
# findings and then exited 0.
scan_file() {
    local blob="$1" label="$2" empty_is_fine="${3:-0}"
    [ "$PATTERN_COUNT" -eq 0 ] && return 0

    # An empty blob usually means the scan did not work — a failed export, an
    # unreadable file — and calling that clean is the mistake this whole script
    # keeps making. The exception is a staged diff, where having nothing to scan
    # is an ordinary state: amending a message stages no changes.
    if ! [ -s "$blob" ]; then
        if [ "$empty_is_fine" = "1" ]; then
            pass "nothing staged to scan"
        else
            fail "nothing to scan for $label — refusing to call it clean"
        fi
        return 0
    fi

    grep -qiFf "$PATTERN_FILE" "$blob"
    case $? in
        1) pass "no forbidden string in $label"; return 0 ;;
        0) : ;;
        # grep's third exit status is an error, and `! grep` treated it as "no
        # match" — a missing file used to report a pass.
        *) fail "the scan of $label errored — refusing to call it clean"; return 0 ;;
    esac

    local index=0 hits
    while IFS= read -r pattern; do
        index=$((index + 1))
        hits=$(grep -ciF -- "$pattern" "$blob" 2>/dev/null || true)
        if [ "${hits:-0}" -gt 0 ]; then
            fail "$label contains pattern #$index (${hits} matching line(s))"
        fi
    done <"$PATTERN_FILE"
    return 0
}

if [ "$PATTERN_COUNT" -gt 0 ]; then
    case "$MODE" in
        staged)
            # --text so a binary blob is scanned rather than summarised as
            # "Binary files differ", and --no-ext-diff so a configured external
            # differ cannot quietly change what the hook examines.
            git diff --cached --no-ext-diff --text >"$WORK/blob"
            scan_file "$WORK/blob" "the staged diff" 1
            ;;
        tree)
            # File names as well as contents: a file called
            # backup-of-<private-host>.txt leaks in its name alone, and the
            # contents-only scan passed it.
            if [ "$(git ls-files | wc -l)" -eq 0 ]; then
                fail "no tracked files to scan — refusing to report a clean tree"
            else
                git ls-files >"$WORK/blob"
                if ! git ls-files -z | xargs -0 cat >>"$WORK/blob" 2>/dev/null; then
                    fail "could not read every tracked file — the scan is incomplete"
                fi
                scan_file "$WORK/blob" "the working tree"
            fi
            ;;
        history)
            # Every object on every ref, read raw. `git show` renders diffs, so a
            # binary blob appeared as "Binary files differ" and its contents were
            # never examined — which is precisely the committed-then-deleted
            # database this sweep exists to catch before the repo goes public.
            # Raw objects also carry commit messages and path names.
            if [ -z "$(git rev-list --all 2>/dev/null)" ]; then
                fail "no commits to scan — refusing to report a clean history"
            else
                {
                    git rev-list --all --objects
                    git rev-list --all --objects | awk '{print $1}' | git cat-file --batch
                } >"$WORK/blob" 2>/dev/null
                scan_file "$WORK/blob" "the history"
            fi
            ;;
    esac
fi

if [ -n "$IMAGE" ]; then
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        fail "image $IMAGE is not present locally"
    else
        docker image inspect "$IMAGE" >"$WORK/image-config"
        scan_file "$WORK/image-config" "the image configuration"

        mkdir -p "$WORK/image"
        if ! docker save "$IMAGE" | tar -x -C "$WORK/image"; then
            fail "could not export $IMAGE for scanning"
        else
            # zcat -f passes uncompressed data through and inflates gzip. Layers
            # arrive compressed under some storage drivers, and grep over the raw
            # bytes of a gzip blob finds nothing at all.
            find "$WORK/image" -type f -print0 | xargs -0 zcat -f 2>/dev/null \
                >"$WORK/image-layers"
            scan_file "$WORK/image-layers" "the image layers"
        fi

        env_leak=$(docker image inspect "$IMAGE" --format '{{json .Config.Env}}' \
            | grep -oiE '(secret|password|token|key)[A-Z_]*=[^,"]{8,}' || true)
        if [ -n "$env_leak" ]; then
            fail "the image bakes in something that looks like a credential"
        else
            pass "no credential-shaped value baked into the image environment"
        fi
    fi
fi

# --- Verdict -----------------------------------------------------------------

echo
if [ "$FAILURES" -gt 0 ]; then
    echo "audit: $FAILURES check(s) failed"
    exit 1
fi
echo "audit: clean"
