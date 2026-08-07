#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  submit_from_config.sh CONFIG [options]

Plan and inspect only (default):
  submit_from_config.sh examples/viirs_water_year_request.json

Plan, stage configured inputs, and submit:
  submit_from_config.sh examples/viirs_water_year_request.json \
      --submit \
      --state-root /persistent/shared/spires-batch-state

Options:
  --submit             Perform the live Slurm submission. Without this flag,
                       the script validates, plans, and stops.
  --state-root PATH    Persistent reservation-state directory. Required with
                       --submit. All runs targeting the same product namespace
                       should share this directory.
  --run-root PATH      Parent for timestamped run artifacts.
                       Default: /scratch/alpine/$USER/spires-batch-runs
  --env-prefix PATH    Mamba environment prefix containing spires-batch.
                       May also be set with SPIRES_BATCH_ENV_PREFIX.
  -h, --help           Show this help.

The request controls sensor/platform, inputs, science, output paths, staging,
resource profile, concurrency, and automatic retry count. --submit is the only
switch in this wrapper that mutates Slurm state.
EOF
}

submit="false"
state_root="${SPIRES_BATCH_STATE_ROOT:-}"
run_root="${SPIRES_BATCH_RUN_ROOT:-/scratch/alpine/${USER:?USER must be set}/spires-batch-runs}"
env_prefix="${SPIRES_BATCH_ENV_PREFIX:-}"
config_path=""

while (($#)); do
    case "$1" in
        --submit)
            submit="true"
            shift
            ;;
        --state-root)
            [[ $# -ge 2 ]] || {
                echo "--state-root requires a path" >&2
                exit 2
            }
            state_root="$2"
            shift 2
            ;;
        --run-root)
            [[ $# -ge 2 ]] || {
                echo "--run-root requires a path" >&2
                exit 2
            }
            run_root="$2"
            shift 2
            ;;
        --env-prefix)
            [[ $# -ge 2 ]] || {
                echo "--env-prefix requires a path" >&2
                exit 2
            }
            env_prefix="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [[ -n "${config_path}" ]]; then
                echo "Only one CONFIG path may be supplied" >&2
                usage >&2
                exit 2
            fi
            config_path="$1"
            shift
            ;;
    esac
done

if [[ -z "${config_path}" ]]; then
    echo "CONFIG is required" >&2
    usage >&2
    exit 2
fi
if [[ ! -f "${config_path}" ]]; then
    echo "Config file does not exist: ${config_path}" >&2
    exit 2
fi
if [[ "${submit}" == "true" && -z "${state_root}" ]]; then
    echo "--state-root is required with --submit" >&2
    exit 2
fi

batch_command=()
if [[ -n "${env_prefix}" ]]; then
    if ! command -v mamba >/dev/null 2>&1; then
        echo "mamba is required when --env-prefix is used" >&2
        exit 2
    fi
    if [[ ! -d "${env_prefix}" ]]; then
        echo "Mamba environment prefix does not exist: ${env_prefix}" >&2
        exit 2
    fi
    batch_command=(mamba run -p "${env_prefix}" spires-batch)
elif command -v spires-batch >/dev/null 2>&1; then
    batch_command=(spires-batch)
else
    echo "spires-batch is not on PATH; activate spipy14 or use --env-prefix" >&2
    exit 2
fi

run_batch() {
    "${batch_command[@]}" "$@"
}

config_name="$(basename "${config_path}")"
config_stem="${config_name%.json}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${run_root}"
run_directory="${run_root}/${config_stem}-${timestamp}"
mkdir "${run_directory}"

request_snapshot="${run_directory}/request.json"
manifest="${run_directory}/resolved-plan.json"
operation_directory="${run_directory}/operation"

cp "${config_path}" "${request_snapshot}"

echo "Validating ${config_path}"
run_batch validate "${config_path}" | tee "${run_directory}/validation.txt"

echo "Writing immutable plan ${manifest}"
run_batch plan "${config_path}" --output "${manifest}" \
    | tee "${run_directory}/planning.txt"

echo "Writing task and path preview ${run_directory}/dry-run.txt"
run_batch dry-run "${manifest}" > "${run_directory}/dry-run.txt"

echo "Writing staging preview ${run_directory}/staging-plan.txt"
run_batch stage "${manifest}" > "${run_directory}/staging-plan.txt"

echo
echo "Run artifacts: ${run_directory}"
echo "Resolved plan: ${manifest}"
echo "Task preview: ${run_directory}/dry-run.txt"
echo "Staging preview: ${run_directory}/staging-plan.txt"

if [[ "${submit}" != "true" ]]; then
    echo
    echo "Plan-only mode complete. Review the previews, then rerun with"
    echo "--submit --state-root /persistent/shared/spires-batch-state"
    exit 0
fi

mkdir -p "${state_root}"

echo "Executing configured staging before submission"
run_batch stage "${manifest}" --execute \
    | tee "${run_directory}/staging-execution.txt"

if type module >/dev/null 2>&1; then
    module try-load slurm/blanca >/dev/null 2>&1 || true
fi
if ! command -v sbatch >/dev/null 2>&1; then
    echo "sbatch is unavailable after attempting to load slurm/blanca" >&2
    exit 2
fi

echo "Starting live stage-gated Slurm execution"
run_batch submission start-operational "${manifest}" \
    --state-root "${state_root}" \
    --output-dir "${operation_directory}" \
    | tee "${run_directory}/submission.txt"

echo
echo "Submission record: ${run_directory}/submission.txt"
echo "Operation record: ${operation_directory}/operation.json"
echo "Terminal result: ${operation_directory}/operational-result.json"
echo "Human-readable summary: ${operation_directory}/summaries/run-summary.txt"
