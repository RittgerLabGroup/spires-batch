#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 [--merged] [--verify-only]"
    echo
    echo "  --merged       Expect merged component work on each repository's main branch."
    echo "  --verify-only  Check branches and imports without changing the environment."
}

mode="development"
verify_only="false"
while (($#)); do
    case "$1" in
        --merged)
            mode="merged"
            ;;
        --verify-only)
            verify_only="true"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "${CONDA_DEFAULT_ENV:-}" != "spipy14" ]]; then
    echo "This bootstrap must run inside the spipy14 mamba environment." >&2
    echo "Example: mamba run -n spipy14 bash scripts/bootstrap_local_phase_d.sh" >&2
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
batch_root="$(cd "${script_dir}/.." && pwd)"
organization_root="$(cd "${batch_root}/.." && pwd)"

repositories=(
    "spires-contract"
    "spires-io"
    "spires-r0"
    "spires-inversion"
    "spires-postprocess"
    "spires-batch"
)
development_branches=(
    "batch-support"
    "batch-support"
    "ross-dev"
    "main"
    "main"
    "main"
)

repo_paths=()
for index in "${!repositories[@]}"; do
    repository="${repositories[$index]}"
    repo_path="${organization_root}/${repository}"
    repo_paths+=("${repo_path}")

    if [[ ! -d "${repo_path}/.git" ]]; then
        echo "Missing sibling Git repository: ${repo_path}" >&2
        exit 2
    fi

    expected_branch="main"
    if [[ "${mode}" == "development" ]]; then
        expected_branch="${development_branches[$index]}"
    fi
    actual_branch="$(git -C "${repo_path}" branch --show-current)"
    if [[ "${actual_branch}" != "${expected_branch}" ]]; then
        echo "${repository}: expected branch ${expected_branch}, found ${actual_branch}" >&2
        exit 2
    fi
    echo "${repository}: ${actual_branch}"
done

phase_d_python="${SPIRES_PHASE_D_PYTHON:-python}"
if [[ "${verify_only}" != "true" ]]; then
    "${phase_d_python}" -m pip install --no-deps --no-build-isolation \
        --editable "${repo_paths[0]}" \
        --editable "${repo_paths[1]}" \
        --editable "${repo_paths[2]}" \
        --editable "${repo_paths[4]}" \
        --editable "${repo_paths[5]}"

    phase_d_cc="${SPIRES_PHASE_D_CC:-/usr/bin/gcc}"
    phase_d_cxx="${SPIRES_PHASE_D_CXX:-/usr/bin/g++}"
    if [[ ! -x "${phase_d_cc}" || ! -x "${phase_d_cxx}" ]]; then
        echo "Missing inversion compiler: CC=${phase_d_cc}, CXX=${phase_d_cxx}" >&2
        exit 2
    fi
    CC="${phase_d_cc}" CXX="${phase_d_cxx}" \
        "${phase_d_python}" -m pip install --no-deps --no-build-isolation \
        --editable "${repo_paths[3]}"
fi

SPIRES_PHASE_D_ROOT="${organization_root}" "${phase_d_python}" - <<'PY'
import importlib
import os
from pathlib import Path

organization_root = Path(os.environ["SPIRES_PHASE_D_ROOT"]).resolve()
expected_modules = {
    "spires_contract": organization_root / "spires-contract",
    "spires_io": organization_root / "spires-io",
    "spires_r0": organization_root / "spires-r0",
    "spires_inversion": organization_root / "spires-inversion",
    "spires_postprocess": organization_root / "spires-postprocess",
    "spires_batch": organization_root / "spires-batch",
}

for module_name, expected_root in expected_modules.items():
    module = importlib.import_module(module_name)
    module_path = Path(module.__file__).resolve()
    if not module_path.is_relative_to(expected_root.resolve()):
        raise RuntimeError(
            f"{module_name} resolved to {module_path}, expected {expected_root}"
        )
    print(f"{module_name}: {module_path}")
PY

echo "Phase D local scientific stack is ready in ${mode} mode."
