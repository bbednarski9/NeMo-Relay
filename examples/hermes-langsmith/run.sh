#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

example_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$example_root/../.." && pwd)"
mode="${1:-local}"
hermes_source="${HERMES_SOURCE:-}"

if [[ "$mode" != "local" && "$mode" != "langsmith" ]]; then
    echo "usage: $0 [local|langsmith]" >&2
    exit 2
fi

for command_name in node python3; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "required command not found: $command_name" >&2
        exit 2
    fi
done

hermes_launcher=""
hermes_python="${HERMES_PYTHON:-}"
hermes_python_env=()
if [[ -n "$hermes_source" ]]; then
    if [[ ! -d "$hermes_source" ]]; then
        echo "HERMES_SOURCE is not a directory: $hermes_source" >&2
        exit 2
    fi
    hermes_source="$(cd "$hermes_source" && pwd)"
    if [[ ! -f "$hermes_source/cli.py" ]]; then
        echo "HERMES_SOURCE does not contain cli.py: $hermes_source" >&2
        exit 2
    fi
    if [[ -z "$hermes_python" && -x "$hermes_source/.venv/bin/python" ]]; then
        hermes_python="$hermes_source/.venv/bin/python"
    fi
    hermes_python_env+=("PYTHONPATH=$hermes_source${PYTHONPATH:+:$PYTHONPATH}")
elif command -v hermes >/dev/null 2>&1; then
    hermes_launcher="$(command -v hermes)"
    if [[ -z "$hermes_python" ]]; then
        hermes_python="$(sed -n "s/^'''exec' '\\([^']*\\)'.*/\\1/p" "$hermes_launcher" | head -1)"
    fi
else
    echo "required command not found: hermes (or set HERMES_SOURCE and HERMES_PYTHON)" >&2
    exit 2
fi
if [[ -z "$hermes_python" || ! -x "$hermes_python" ]]; then
    echo "could not detect the Python interpreter used by Hermes; set HERMES_PYTHON" >&2
    exit 2
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
artifact_directory="${HERMES_LANGSMITH_ARTIFACT_DIR:-$repo_root/artifacts/hermes-langsmith-$timestamp}"
project="${LANGSMITH_PROJECT:-hermes-nemo-relay-smoke}"
mkdir -p \
    "$artifact_directory/hermes-home" \
    "$artifact_directory/provider-barrier" \
    "$artifact_directory/workspace"

provider_pid=""
collector_pid=""
cleanup() {
    if [[ -n "$collector_pid" ]]; then
        kill "$collector_pid" 2>/dev/null || true
        wait "$collector_pid" 2>/dev/null || true
    fi
    if [[ -n "$provider_pid" ]]; then
        kill "$provider_pid" 2>/dev/null || true
        wait "$provider_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT

provider_ready="$artifact_directory/provider-ready.json"
python3 "$repo_root/scripts/test-support/codex_mock_provider.py" \
    --ready-file "$provider_ready" \
    --log-file "$artifact_directory/provider-requests.jsonl" \
    --barrier-dir "$artifact_directory/provider-barrier" \
    >"$artifact_directory/provider.stdout" \
    2>"$artifact_directory/provider.stderr" &
provider_pid=$!

for _ in $(seq 1 100); do
    [[ -s "$provider_ready" ]] && break
    sleep 0.05
done
if [[ ! -s "$provider_ready" ]]; then
    echo "synthetic provider did not become ready" >&2
    sed -n '1,120p' "$artifact_directory/provider.stderr" >&2
    exit 1
fi
provider_address="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["address"])' "$provider_ready")"

if [[ "$mode" == "local" ]]; then
    export LANGSMITH_API_KEY="local-fixture-key"
    node "$repo_root/scripts/test-support/otel_collector.mjs" \
        >"$artifact_directory/otel-capture.jsonl" \
        2>"$artifact_directory/otel-collector.stderr" &
    collector_pid=$!
    for _ in $(seq 1 100); do
        [[ -s "$artifact_directory/otel-capture.jsonl" ]] && break
        sleep 0.05
    done
    if [[ ! -s "$artifact_directory/otel-capture.jsonl" ]]; then
        echo "local OTLP collector did not become ready" >&2
        sed -n '1,120p' "$artifact_directory/otel-collector.stderr" >&2
        exit 1
    fi
    otlp_traces_endpoint="$(
        python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).readline())["endpoint"])' \
            "$artifact_directory/otel-capture.jsonl"
    )"
else
    if [[ -z "${LANGSMITH_API_KEY:-}" ]]; then
        echo "set LANGSMITH_API_KEY for the LangSmith deployment" >&2
        exit 2
    fi
    if [[ -n "${LANGSMITH_OTLP_ENDPOINT:-}" ]]; then
        otlp_traces_endpoint="$LANGSMITH_OTLP_ENDPOINT"
    elif [[ -n "${LANGSMITH_ENDPOINT:-}" ]]; then
        otlp_traces_endpoint="${LANGSMITH_ENDPOINT%/}/otel/v1/traces"
    else
        echo "set LANGSMITH_ENDPOINT or LANGSMITH_OTLP_ENDPOINT" >&2
        exit 2
    fi
fi

plugins_toml="$artifact_directory/plugins.toml"
python3 "$example_root/render_config.py" \
    --template "$example_root/plugins.toml.template" \
    --output "$plugins_toml" \
    --otlp-traces-endpoint "$otlp_traces_endpoint"

if ! env "${hermes_python_env[@]}" "$hermes_python" -c \
    'import importlib.metadata,json; from agent import relay_runtime; version=importlib.metadata.version("nemo-relay"); expected="HERMES_NEMO_RELAY_PLUGINS_TOML"; actual=getattr(relay_runtime,"RELAY_PLUGINS_CONFIG_ENV",None); result={"hermes_native_plugin_env":actual,"nemo_relay_version":version}; print(json.dumps(result,indent=2)); raise SystemExit(0 if actual==expected and version.split(".")[:2]==["0","6"] else 1)' \
    >"$artifact_directory/runtime-preflight.json"; then
    echo "Hermes must provide native Relay plugin initialization and use nemo-relay 0.6.x:" >&2
    sed -n '1,200p' "$artifact_directory/runtime-preflight.json" >&2
    echo "use Hermes feat/relay-native-plugin-init with a nemo-relay 0.6.x environment" >&2
    echo "artifacts: $artifact_directory" >&2
    exit 1
fi

if ! env "${hermes_python_env[@]}" "$hermes_python" -c \
    'import json,sys,tomllib; from nemo_relay import plugin; config=tomllib.load(open(sys.argv[1],"rb")); report=plugin.validate(config); blocking=[item for item in report["diagnostics"] if item["level"] in {"warning","error"}]; print(json.dumps(report,indent=2)); raise SystemExit(bool(blocking))' \
    "$plugins_toml" \
    >"$artifact_directory/plugin-validation.json"; then
    echo "NeMo Relay rejected or warned about the rendered Relay 0.6 plugins.toml:" >&2
    sed -n '1,200p' "$artifact_directory/plugin-validation.json" >&2
    echo "artifacts: $artifact_directory" >&2
    exit 1
fi

otel_headers="x-api-key=${LANGSMITH_API_KEY},Langsmith-Project=${project}"
if [[ -n "$hermes_source" ]]; then
    hermes_command=(
        "$hermes_python"
        "$hermes_source/cli.py"
        --query "Reply with exactly pong."
        --provider auto
        --model gpt-4o-mini
        --base_url "http://$provider_address/v1"
        --api_key "hermes-langsmith-fixture-key"
        --max_turns 2
        --quiet
    )
else
    hermes_command=(
        "$hermes_launcher"
        chat
        --query "Reply with exactly pong."
        --provider openai-api
        --model gpt-4o-mini
        --max-turns 2
        --quiet
    )
fi

if ! (
    cd "$artifact_directory/workspace"
    env \
        "${hermes_python_env[@]}" \
        HERMES_HOME="$artifact_directory/hermes-home" \
        HERMES_IGNORE_RULES=1 \
        HERMES_IGNORE_USER_CONFIG=1 \
        HERMES_NEMO_RELAY_PLUGINS_TOML="$plugins_toml" \
        OTEL_EXPORTER_OTLP_HEADERS="$otel_headers" \
        OPENAI_API_KEY="hermes-langsmith-fixture-key" \
        OPENAI_BASE_URL="http://$provider_address/v1" \
        DISABLE_AUTOUPDATER=1 \
        "${hermes_command[@]}"
) >"$artifact_directory/hermes.stdout" 2>"$artifact_directory/hermes.stderr"; then
    echo "Hermes synthetic run failed:" >&2
    sed -n '1,240p' "$artifact_directory/hermes.stderr" >&2
    echo "artifacts: $artifact_directory" >&2
    exit 1
fi

verify_args=("$artifact_directory" "--project" "$project")
if [[ "$mode" == "langsmith" ]]; then
    verify_args+=("--skip-otlp-capture")
fi
python3 "$example_root/verify_export.py" "${verify_args[@]}"

echo "mode: $mode"
echo "project: $project"
echo "OTLP traces endpoint: $otlp_traces_endpoint"
echo "artifacts: $artifact_directory"
