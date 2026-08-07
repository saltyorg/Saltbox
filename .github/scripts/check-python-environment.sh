#!/usr/bin/env bash

set -euo pipefail

python_version=$(tr -d '[:space:]' < .python-version)
uv_version=$(tr -d '[:space:]' < .uv-version)
renovate_uv=$(jq -r '.constraints.uv' .github/renovate.json)
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
export UV_PYTHON_INSTALL_DIR="$workdir/python"

if [[ "$renovate_uv" != "$uv_version" ]]; then
    echo "Renovate uv constraint $renovate_uv does not match .uv-version $uv_version" >&2
    exit 1
fi

if [[ "$(uv --version)" != "uv $uv_version"* ]]; then
    echo "Expected uv $uv_version, got $(uv --version)" >&2
    exit 1
fi

if [[ ! "$python_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid exact Python version: $python_version" >&2
    exit 1
fi

uv python install --managed-python --no-bin "$python_version"
lock_sha_before=$(sha256sum requirements/requirements-saltbox.txt | awk '{print $1}')
uv pip compile \
    --python-version "$python_version" \
    --generate-hashes \
    --output-file requirements/requirements-saltbox.txt \
    requirements/requirements-saltbox.in
lock_sha_after=$(sha256sum requirements/requirements-saltbox.txt | awk '{print $1}')
if [[ "$lock_sha_before" != "$lock_sha_after" ]]; then
    echo "requirements-saltbox.txt is not synchronized with requirements-saltbox.in" >&2
    git diff -- requirements/requirements-saltbox.txt
    exit 1
fi

runtime_venv="$workdir/runtime-venv"
uv venv --python "$python_version" --managed-python --no-project "$runtime_venv"
uv pip sync \
    --python "$runtime_venv/bin/python" \
    --require-hashes \
    requirements/requirements-saltbox.txt
uv pip check --python "$runtime_venv/bin/python"

"$runtime_venv/bin/python" --version
"$runtime_venv/bin/ansible" --version
"$runtime_venv/bin/certbot" --version
"$runtime_venv/bin/apprise" --version

if uv pip list --python "$runtime_venv/bin/python" --format freeze | grep -Eiq '^(pip|setuptools|uv|wheel)=='; then
    echo "The Saltbox runtime unexpectedly contains pip, setuptools, uv, or wheel" >&2
    exit 1
fi

cffi_version=$(sed -n 's/^cffi==\([^[:space:]\\]*\).*/\1/p' requirements/requirements-saltbox.txt | head -n 1)
if [[ -z "$cffi_version" ]]; then
    echo "Unable to find the locked cffi version" >&2
    exit 1
fi

source_build_venv="$workdir/source-build-venv"
uv venv --python "$python_version" --managed-python --no-project "$source_build_venv"
uv pip install \
    --python "$source_build_venv/bin/python" \
    --no-binary cffi \
    "cffi==$cffi_version"

if uv pip list --python "$source_build_venv/bin/python" --format freeze | grep -Eiq '^(pip|setuptools|wheel)=='; then
    echo "The source-build target unexpectedly contains pip, setuptools, or wheel" >&2
    exit 1
fi
