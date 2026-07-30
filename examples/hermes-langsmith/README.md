<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Export Native Hermes Agent Telemetry to LangSmith

This example exercises Hermes Agent's native NeMo Relay plugin initialization.
Hermes reads `HERMES_NEMO_RELAY_PLUGINS_TOML` and calls
`nemo_relay.plugin.initialize()` before it opens its first Relay session scope.
It does not enable or load Hermes's optional `observability/nemo_relay` plugin,
and it does not create a Hermes `config.yaml`.

The example intentionally targets `nemo-relay` 0.6.x. Its minimal
[`plugins.toml.template`](./plugins.toml.template) uses the Relay 0.6
`openinference` component and sends OTLP/HTTP protobuf to either a local capture
server or LangSmith Cloud. The runner rejects other Relay minor versions and
any configuration validation warning or error.

## Why OpenInference over OTLP

OTLP/HTTP protobuf is the transport. OpenInference is the semantic projection
on those spans. This example deliberately omits ATOF and ATIF exporters so the
configuration proves only the LangSmith trace path.

LangSmith accepts OTLP and recognizes OpenInference attributes such as
`openinference.span.kind`, `input.value`, `output.value`, `llm.model_name`, and
`tool.name`. A generic OpenTelemetry export would preserve more Relay-private
fields, but OpenInference is the better default for LangSmith's trace UI and
dataset workflows.

## Prerequisites

Use the Hermes
[`feat/relay-native-plugin-init`](https://github.com/bbednarski9/hermes-agent/tree/feat/relay-native-plugin-init)
branch or a later Hermes build containing the same native initialization. The
branch is based on Hermes main at `8defb9f` and requires no optional Hermes
observability plugin.

Install Hermes and its `nemo-relay` 0.6.x dependency in a Python environment
using the Hermes repository's development instructions. Then point this
example at that source tree and interpreter:

```bash
export HERMES_SOURCE="/absolute/path/to/hermes-agent"
export HERMES_PYTHON="/absolute/path/to/hermes-venv/bin/python"
```

If the branch has its own `.venv`, `HERMES_PYTHON` is optional. If an installed
`hermes` launcher already contains the native initialization, both variables
are optional.

The runner performs two preflight checks before starting Hermes:

- `agent.relay_runtime` must expose native
  `HERMES_NEMO_RELAY_PLUGINS_TOML` support.
- The Hermes interpreter must import `nemo-relay` 0.6.x.

The demo does not build or inject the NeMo Relay binding from this checkout.
That is deliberate: it proves the published Relay 0.6 configuration path that
Hermes currently depends on.

## Local Preflight

Run the deterministic provider and local OTLP capture:

```bash
./examples/hermes-langsmith/run.sh local
```

The command prints the artifact directory after all checks pass. Its rendered
`plugins.toml` is the exact configuration Hermes consumed. The directory also
contains:

- `runtime-preflight.json`
- `plugin-validation.json`
- `otel-capture.jsonl`
- `provider-requests.jsonl`
- Hermes stdout and stderr

The generated `hermes-home/` remains free of plugin activation configuration.
Artifacts default to the repository's ignored `artifacts/` directory. Set
`HERMES_LANGSMITH_ARTIFACT_DIR` to choose a stable path.

## Export to LangSmith Cloud

Set the API endpoint, API key, and project, then run:

```bash
export LANGSMITH_ENDPOINT="https://api.smith.langchain.com"
export LANGSMITH_API_KEY="<secret>"
export LANGSMITH_PROJECT="hermes-nemo-relay-smoke"

./examples/hermes-langsmith/run.sh langsmith
```

The runner derives `https://api.smith.langchain.com/otel/v1/traces`. Set
`LANGSMITH_OTLP_ENDPOINT` explicitly for a different regional deployment.

Relay 0.6 does not support `header_env` in its OpenInference component. The
runner therefore supplies `x-api-key` and `Langsmith-Project` through the
standard `OTEL_EXPORTER_OTLP_HEADERS` environment variable. The API key is
never written to `plugins.toml` or the artifacts.

In LangSmith, open **Tracing Projects**, select
`hermes-nemo-relay-smoke`, and inspect the newest root run. The trace tree
should contain Hermes session and turn spans and an LLM child with
`gpt-4o-mini`, the synthetic prompt, and `pong`. A real Hermes run that invokes
a tool adds `TOOL` spans with `tool.name`, input, and output attributes.

## Query Traces and Create a Dataset

The helper queries recent root runs without printing prompt or response
payloads by default:

```bash
uv run --no-project --with "langsmith>=0.9.4" \
  python examples/hermes-langsmith/query_langsmith.py \
  --project "$LANGSMITH_PROJECT"
```

Add `--show-payloads` when the trace content is safe to print. To explicitly
create a dataset from recent successful LLM child runs:

```bash
uv run --no-project --with "langsmith>=0.9.4" \
  python examples/hermes-langsmith/query_langsmith.py \
  --project "$LANGSMITH_PROJECT" \
  --create-dataset "hermes-nemo-relay-smoke-dataset" \
  --dataset-limit 5
```

The helper uses LLM children rather than the root session span because their
inputs and outputs are directly useful as dataset examples. It records the
source project, run ID, and trace ID in each example's metadata.

For manual curation, select runs in a LangSmith tracing project and choose
**Add to Dataset**. This is preferable when a reviewer should edit or reject
examples before they become evaluation data.

## Relay 0.7 Migration

Do not use Relay 0.7's unified `opentelemetry.endpoints` syntax with the Hermes
0.6 dependency. Relay 0.6 reports that field as unknown and will not use the
configured endpoint.

When Hermes updates to Relay 0.7, migrate this template to the unified endpoint
schema and move the LangSmith headers back into the endpoint configuration.
Until then, the version preflight keeps this example on the tested 0.6 path.

## Data and Security Notes

OpenInference traces can contain prompts, model responses, tool arguments, and
tool results. Run the local preflight first, inspect `otel-capture.jsonl`, and
add NeMo Relay sanitize guardrails before exporting sensitive workloads. The
example's model provider is loopback-only and uses fixture credentials; the
only real secret in LangSmith mode is `LANGSMITH_API_KEY`.

References:

- [LangSmith OpenTelemetry tracing](https://docs.langchain.com/langsmith/trace-with-opentelemetry)
- [LangSmith trace queries](https://docs.langchain.com/langsmith/export-traces)
- [Create datasets from traces](https://docs.langchain.com/langsmith/manage-datasets-programmatically)
- [NeMo Relay observability configuration](../../docs/configure-plugins/observability/configuration.mdx)
