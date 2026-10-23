#!/bin/sh
# Requires Gitleaks 8.30.1 and Git. All output is redacted; no report is saved.
set -eu
umask 077

repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
binary=${GITLEAKS_BINARY:-gitleaks}
mode=${1:-history}
if [ "$#" -gt 1 ]; then
    printf '%s\n' 'Usage: scripts/check_secrets.sh [history|staged|self-test]' >&2
    exit 2
fi
case "$mode" in
    history|staged|self-test) ;;
    *) printf '%s\n' 'Usage: scripts/check_secrets.sh [history|staged|self-test]' >&2; exit 2 ;;
esac
if ! command -v "$binary" >/dev/null 2>&1; then
    printf '%s\n' 'Install Gitleaks 8.30.1, or set GITLEAKS_BINARY to its executable path.' >&2
    exit 2
fi
case "$("$binary" version)" in
    8.30.1|v8.30.1) ;;
    *) printf '%s\n' 'Gitleaks 8.30.1 is required to keep local and CI detection consistent.' >&2; exit 2 ;;
esac

scan_tmp=$(mktemp -d "${TMPDIR:-/tmp}/twilio-gitleaks.XXXXXX")
trap 'rm -rf -- "$scan_tmp"' EXIT HUP INT TERM
run_scan() {
    "$binary" "$@" --config "$repo/.gitleaks.toml" \
        --gitleaks-ignore-path "$scan_tmp" --ignore-gitleaks-allow \
        --redact=100 --no-banner --no-color --max-archive-depth=3
}

case "$mode" in
    history)
        if [ "$(git -C "$repo" rev-parse --is-shallow-repository)" != false ]; then
            printf '%s\n' 'Full-history scan requires an unshallowed clone. Fetch the available branches and tags first.' >&2
            exit 2
        fi
        run_scan git "$repo" --log-opts='--all --full-history -m'
        # Scan the complete current committed tree as well as historical diffs.
        git -C "$repo" archive HEAD > "$scan_tmp/head.tar"
        mkdir "$scan_tmp/head"
        tar -xf "$scan_tmp/head.tar" -C "$scan_tmp/head"
        run_scan dir "$scan_tmp/head"
        # Git diff scanning does not inspect commit messages.
        git -C "$repo" log --all --format=raw > "$scan_tmp/commit-metadata.txt"
        run_scan dir "$scan_tmp/commit-metadata.txt"
        # Include dangling/reflog-only blobs and commit/tag messages still in
        # this local object database. Temporary copies are mode 600 and removed
        # by the trap; no secret values or reports are retained.
        mkdir "$scan_tmp/objects"
        git -C "$repo" cat-file --batch-all-objects \
            --batch-check='%(objectname) %(objecttype)' > "$scan_tmp/object-list.txt"
        while read -r object_id object_type; do
            case "$object_type" in
                blob|commit|tag)
                    git -C "$repo" cat-file "$object_type" "$object_id" > "$scan_tmp/objects/$object_id"
                    ;;
            esac
        done < "$scan_tmp/object-list.txt"
        run_scan dir "$scan_tmp/objects"
        ;;
    staged)
        run_scan git "$repo" --pre-commit --staged
        ;;
    self-test)
        # Create deliberately synthetic credentials only inside a private temp
        # repo. Then delete them in a later commit to verify history detection.
        fixture="$scan_tmp/fixture"
        mkdir "$fixture"
        git -C "$fixture" init -q
        printf 'aws_access_key_id = "%s%s"\n' 'AKIA' 'Q7M2N3P4R6S2T3V5' > "$fixture/config.txt"
        printf 'twilio_auth_token = "%s%s"\n' '9f4c7a1e8b2d6c30' 'a5e1d8b7c2f9046a' >> "$fixture/config.txt"
        printf 'aws_secret_access_key = "%s%s"\n' 't8H3n5L7p2S9v4C6x1J0' 'q6B8m3D5f7K9r2W4z1A0' >> "$fixture/config.txt"
        git -C "$fixture" add config.txt
        git -C "$fixture" -c user.name='Scanner self-test' \
            -c user.email='scanner@example.invalid' -c commit.gpgsign=false \
            -c core.hooksPath=/dev/null commit -qm 'Synthetic test fixtures'
        git -C "$fixture" rm -q config.txt
        git -C "$fixture" -c user.name='Scanner self-test' \
            -c user.email='scanner@example.invalid' -c commit.gpgsign=false \
            -c core.hooksPath=/dev/null commit -qm 'Delete test fixtures'
        # Each rule must independently find its deleted synthetic credential.
        # A distinct detection code prevents config/runtime errors (exit 1)
        # from masquerading as successful positive-control detections.
        for rule in aws-access-token twilio-auth-token-assignment aws-secret-key-assignment; do
            result=0
            run_scan git "$fixture" --log-opts='--all --full-history -m' \
                --enable-rule "$rule" --exit-code=42 || result=$?
            if [ "$result" -ne 42 ]; then
                printf 'Self-test failed for rule %s: expected detection exit 42, got %s.\n' "$rule" "$result" >&2
                exit 2
            fi
        done
        printf '%s\n' 'Self-test passed: all three rules detect deleted synthetic credentials.'
        ;;
esac
