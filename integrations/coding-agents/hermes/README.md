<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Hermes Native Integration Prototype

This directory contains a prototype Hermes Python plugin that emits ATIF
trajectories from Hermes native plugin middleware. With the NeMo Flow Python
bindings on `PYTHONPATH`, it can also emit OpenInference traces to Phoenix
without starting the sidecar HTTP process.

## Install Locally

Copy the plugin into Hermes' user plugin directory and enable it:

```bash
mkdir -p ~/.hermes/plugins
cp -R integrations/coding-agents/hermes/plugins/nemoflow ~/.hermes/plugins/
hermes plugins enable nemoflow
```

Configure an ATIF output directory:

```bash
export HERMES_NEMOFLOW_ATIF_DIR=/tmp/hermes-native-atif
```

Then run Hermes normally:

```bash
hermes chat
```

The plugin writes one ATIF file per finalized or reset session:

```text
$HERMES_NEMOFLOW_ATIF_DIR/<session_id>.atif.json
```

## Phoenix Export

Set an OpenInference endpoint and make the local NeMo Flow Python bindings
importable before starting Hermes:

```bash
export PYTHONPATH=/path/to/NeMo-Flow/python${PYTHONPATH:+:$PYTHONPATH}
export HERMES_NEMOFLOW_OPENINFERENCE_ENDPOINT=http://127.0.0.1:4318/v1/traces
export HERMES_NEMOFLOW_OPENINFERENCE_TRANSPORT=http_binary
```

The plugin registers an in-process NeMo Flow OpenInference subscriber, emits
agent, LLM, tool, and subagent mark events from native Hermes hooks, and flushes
on `on_session_finalize` or `on_session_reset`.

## Current Limits

The prototype requires the observer-grade Hermes hook payload additions for full
native fidelity. With those hooks present, it captures stable turn/API IDs, full
bounded API request/response payloads, token usage, tool status/error details,
and subagent start/stop lifecycle payloads. Phoenix token-count attributes are
limited by the current manual NeMo Flow Python LLM API; token usage remains
preserved in ATIF and OpenInference output payloads.

## Test

```bash
python3 -m pytest integrations/coding-agents/hermes/tests/test_nemoflow_plugin.py
```
