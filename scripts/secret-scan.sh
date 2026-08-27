#!/usr/bin/env bash
#
# A generic secret scanner, next to the project-specific one.
#
# scripts/audit.sh knows which strings are private to this deployment. This knows
# what a credential looks like in general — an AWS key, a private key block, a
# token shaped like a token — and so covers what nobody thought to put on a list.
# The two are complementary and neither replaces the other.
#
# The binary is fetched by version and verified against a pinned checksum rather
# than installed by a third-party action: one fewer thing with write access to a
# release pipeline, and a download that is checked instead of trusted.
#
# Usage: secret-scan.sh [--staged]

set -uo pipefail

VERSION="8.30.1"
CHECKSUM="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
ARCHIVE="gitleaks_${VERSION}_linux_x64.tar.gz"
URL="https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${ARCHIVE}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/gpodsync"

MODE=history
[ "${1:-}" = "--staged" ] && MODE=staged

WORK=$(mktemp -d)
# Not `exec`: exec replaces this shell, so the trap never runs and every
# invocation left twenty-nine megabytes behind in the temporary directory.
trap 'rm -rf "$WORK"' EXIT

resolve_binary() {
    # A gitleaks already on PATH is used only when it is the pinned version.
    # Accepting whatever is installed made the pinning decorative: anything
    # named gitleaks that exits 0 would have passed the scan.
    if command -v gitleaks >/dev/null 2>&1 &&
       gitleaks version 2>/dev/null | grep -qx "$VERSION"; then
        command -v gitleaks
        return
    fi

    mkdir -p "$CACHE"
    local archive="$CACHE/$ARCHIVE"
    if [ ! -f "$archive" ]; then
        curl -sSfL "$URL" -o "$archive" || return 1
    fi

    # Verified on every run, not only on download: a cache is a directory anybody
    # on the machine can write to.
    if ! echo "$CHECKSUM  $archive" | sha256sum -c - >/dev/null 2>&1; then
        rm -f "$archive"
        echo "secret-scan: the gitleaks download does not match its pinned checksum." >&2
        return 1
    fi

    tar -xzf "$archive" -C "$WORK" gitleaks || return 1
    echo "$WORK/gitleaks"
}

GITLEAKS=$(resolve_binary) || {
    echo "secret-scan: could not obtain gitleaks $VERSION." >&2
    exit 1
}

# --redact so a finding is reported without copying the secret into a log that,
# on a public repository, anybody can read. The same rule the other scanner
# follows.
if [ "$MODE" = "staged" ]; then
    "$GITLEAKS" git --staged --no-banner --redact --exit-code 1
    exit $?
fi

# Warn rather than pass silently: `gitleaks git` reads history, so a shallow
# clone gives it one commit to look at and it reports no leaks — a clean result
# that examined almost nothing.
if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
    echo "secret-scan: FAIL — this is a shallow clone." >&2
    echo "             gitleaks reads history, so it would scan one commit and" >&2
    echo "             report no leaks. Check out with fetch-depth: 0." >&2
    exit 1
fi

"$GITLEAKS" git --no-banner --redact --exit-code 1
exit $?
