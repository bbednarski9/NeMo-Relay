<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Export Native Hermes Agent Telemetry to LangSmith

This example uses Hermes Agent's first-party `observability/nemo_relay` plugin
and a custom NeMo Relay `plugins.toml`. It sends a deterministic Hermes run to
either a local OTLP capture server or a self-hosted LangSmith deployment.

The local run verifies the full export contract without credentials:

- Hermes loads its built-in NeMo Relay integration from an isolated
  `HERMES_HOME`.
- NeMo Relay accepts the generated observability component configuration.
- ATOF `0.1` scope events and an ATIF `v1.7` trajectory are written locally.
- The OTLP request contains OpenInference attributes and the exact LangSmith
  authentication and project headers.

The LangSmith run uses the same agent, provider fixture, component, and
OpenInference projection. Only the OTLP destination and API key change.

## Why OpenInference over OTLP

| Surface | Role in this example | Send directly to LangSmith? |
|---|---|---|
| ATOF | Canonical NeMo Relay event stream for debugging | No; retained as local JSONL |
| ATIF | Completed agent trajectory for replay or offline evaluation | No; retained as local JSON |
| OpenTelemetry | Transport and trace tree | Yes, using OTLP/HTTP protobuf |
| OpenInference | LLM, tool, chain, input, output, model, and token semantics on OTLP spans | Yes; this is the selected endpoint type |

LangSmith accepts OTLP and recognizes OpenInference attributes such as
`openinference.span.kind`, `input.value`, `output.value`, `llm.model_name`, and
`tool.name`. A generic NeMo Relay `full` endpoint would preserve more
Relay-private fields, but `openinference` is the better default for LangSmith's
trace UI and dataset workflows.

## Prerequisites

Install a Hermes Agent checkout or release that contains the first-party
`observability/nemo_relay` plugin. Confirm it is visible with:

```bash
hermes plugins list
```

Build the Python binding from this NeMo Relay checkout so Hermes exercises the
same source and version-3 component schema as this example:

```bash
just build-python
```

The runner deliberately refuses to fall back to an older installed NeMo Relay
wheel. Set `NEMO_RELAY_PYTHON_SOURCE` only when the binding was built in another
NeMo Relay source tree. If the Hermes launcher does not expose its interpreter
in the usual generated wrapper, set `HERMES_PYTHON` to that environment's
Python executable. An `observability config version 3 is unsupported` diagnostic
means Hermes imported an older NeMo Relay runtime instead of the binding built
from this checkout.

## Local Preflight

Run the deterministic provider and local OTLP capture:

```bash
./examples/hermes-langsmith/run.sh local
```

The command prints the artifact directory after all checks pass. Its rendered
`plugins.toml` is the exact configuration Hermes consumed. The directory also
contains:

- `atof/events.jsonl`
- `atif/trajectory-<session-id>.json`
- `otel-capture.jsonl`
- `provider-requests.jsonl`
- Hermes stdout, stderr, and plugin activation logs

Artifacts default to the repository's ignored `artifacts/` directory. Set
`HERMES_LANGSMITH_ARTIFACT_DIR` to choose a stable path.

## Export to Self-Hosted LangSmith

Use the LangSmith API URL, not the browser UI URL. For a conventional
self-hosted deployment, the SDK API and trace-specific OTLP endpoints are:

```text
https://langsmith.example.com/api/v1
https://langsmith.example.com/api/v1/otel/v1/traces
```

Set the API endpoint, API key, and project, then run:

```bash
export LANGSMITH_ENDPOINT="https://langsmith.example.com/api/v1"
export LANGSMITH_API_KEY="<secret>"
export LANGSMITH_PROJECT="hermes-nemo-relay-smoke"

./examples/hermes-langsmith/run.sh langsmith
```

The runner derives
`$LANGSMITH_ENDPOINT/otel/v1/traces`. Set `LANGSMITH_OTLP_ENDPOINT` explicitly
when a reverse proxy exposes OTLP elsewhere. The API key is referenced through
`header_env`; it is never written to `plugins.toml` or the artifacts.

In LangSmith, open **Tracing Projects**, select
`hermes-nemo-relay-smoke`, and inspect the newest root run. The trace tree
should contain Hermes session/turn spans and an LLM child with `gpt-4o-mini`,
the synthetic prompt, and `pong`. A real Hermes run that invokes a tool adds
`TOOL` spans with `tool.name`, input, and output attributes.

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
