#!/usr/bin/env bash
# Idempotent installer for the gh REST-budget shim (harmonic-forge#650).
#
# Links ~/.local/bin/gh -> tools/gh/gh_shim (this repo), ahead of the real
# `gh` binary on PATH, so every `gh` invocation -- interactive or from
# inside a script -- passes through the scan/budget guard first.
#
# NEVER run against a real operator environment from an agent session --
# this is the operator's own action. This script's logic is covered by a
# unit test that points HOME/PATH at a throwaway temp dir.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIM_PATH="${SCRIPT_DIR}/gh_shim"
TARGET_DIR="${HOME}/.local/bin"
TARGET="${TARGET_DIR}/gh"

mkdir -p "${TARGET_DIR}"

if [ -e "${TARGET}" ] && [ ! -L "${TARGET}" ]; then
    echo "install_gh_shim: ${TARGET} already exists as a real file -- refusing to overwrite it." >&2
    exit 1
fi

if [ -L "${TARGET}" ]; then
    EXISTING_TARGET="$(readlink "${TARGET}")"
    if [ "${EXISTING_TARGET}" != "${SHIM_PATH}" ]; then
        # Resolve both to absolute paths before comparing, in case the
        # existing link uses a relative or differently-spelled path that
        # still happens to point at this same file.
        EXISTING_REAL="$(readlink -f "${TARGET}" 2>/dev/null || echo "${EXISTING_TARGET}")"
        SHIM_REAL="$(readlink -f "${SHIM_PATH}")"
        if [ "${EXISTING_REAL}" != "${SHIM_REAL}" ]; then
            echo "install_gh_shim: ${TARGET} is a symlink to ${EXISTING_TARGET}, not this repo's gh_shim -- refusing to overwrite it." >&2
            exit 1
        fi
    fi
fi

ln -sfn "${SHIM_PATH}" "${TARGET}"

RESOLVED="$(command -v gh || true)"
if [ "${RESOLVED}" != "${TARGET}" ]; then
    echo "install_gh_shim: installed the symlink but 'command -v gh' resolves to '${RESOLVED}', not ${TARGET} -- check PATH ordering." >&2
    exit 1
fi

echo "install_gh_shim: gh now resolves to ${TARGET} -> ${SHIM_PATH}"
