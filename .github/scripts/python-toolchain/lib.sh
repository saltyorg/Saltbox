#!/usr/bin/env bash
# Helpers for update.sh. Source this file; it defines functions only.

toolchain_fail() {
    echo "python-toolchain: $*" >&2
    exit 1
}

toolchain_log() {
    echo "python-toolchain: $*"
}

# Read a pin file that must hold one exact X.Y.Z version.
read_exact_version() {
    local file=$1 label=$2 value
    [[ -f $file ]] || toolchain_fail "$label pin $file is missing"
    value=$(tr -d '[:space:]' < "$file")
    is_exact_version "$value" || toolchain_fail "$label pin is not one exact X.Y.Z version: '$value'"
    printf '%s\n' "$value"
}

is_exact_version() {
    [[ $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# Print the higher of two exact versions.
version_max() {
    printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1
}

# Succeed when $1 is strictly lower than $2.
version_lt() {
    [[ $1 != "$2" && $(version_max "$1" "$2") == "$2" ]]
}

# Check a file against a lowercase hex SHA-256.
verify_sha256() {
    local file=$1 expected=$2 label=$3 actual
    [[ $expected =~ ^[0-9a-f]{64}$ ]] || toolchain_fail "$label: no valid SHA-256 to check against"
    actual=$(sha256sum -- "$file" | awk '{print $1}')
    if [[ $actual != "$expected" ]]; then
        toolchain_fail "$label: SHA-256 mismatch (expected $expected, got $actual)"
    fi
}

# Run a uv, Python or venv entrypoint with no user or system uv config and
# with a throwaway HOME, cache, Python install dir and TMPDIR under the
# workspace. Usage: isolated <workspace> <uv|python|entrypoint> <dir> <cmd> [args]
isolated() (
    set -euo pipefail
    local workspace=$1 kind=$2 command_dir=$3 command=$4
    shift 4
    local variable scratch
    [[ $workspace == /* && $command_dir == /* && $command == /* ]] ||
        toolchain_fail "isolated commands need absolute paths"
    while IFS= read -r variable; do
        case "$variable" in
            UV_SYSTEM_CERTS | UV_NATIVE_TLS) ;;
            UV | UV_* | PIP_* | PYTHON* | VIRTUAL_ENV* | CONDA_* | _CONDA_* | __PYVENV_LAUNCHER__ | ANSIBLE_* | XDG_*)
                unset "$variable"
                ;;
        esac
    done < <(compgen -e)
    mkdir -p -- "$workspace/cache" "$workspace/python" "$workspace/tmp"
    scratch=$(mktemp -d "$workspace/tmp/command.XXXXXXXXXX")
    trap 'rm -rf -- "$scratch"' EXIT
    mkdir -p -- "$scratch/home"
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    export HOME="$scratch/home"
    export UV_CACHE_DIR="$workspace/cache" UV_PYTHON_INSTALL_DIR="$workspace/python"
    case "$kind" in
        # UV_NO_CONFIG rather than --no-config keeps uv's lock header equal
        # to the command CI runs.
        uv) export UV_NO_CONFIG=1 ;;
        python) set -- -I "$@" ;;
        entrypoint)
            export PATH="${command%/*}:$PATH" PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1
            : > "$scratch/ansible.cfg"
            export ANSIBLE_CONFIG="$scratch/ansible.cfg"
            ;;
        *) toolchain_fail "unknown isolated command kind: $kind" ;;
    esac
    cd -- "$command_dir"
    TMPDIR="$scratch" TMP="$scratch" TEMP="$scratch" "$command" "$@"
)
