<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Hermes Native NeMo Flow Instrumentation

This report reviews what the Hermes sidecar integration captures today, what
Hermes exposes through native plugin and shell-hook middleware, and what would
be needed to replace the sidecar HTTP process with in-process NeMo Flow
instrumentation.

## Current Sidecar Coverage

The PR #63 sidecar path gives Hermes useful production telemetry without
patching Hermes:

- Session lifecycle: `on_session_start` starts the agent scope, while
  `on_session_finalize` and `on_session_reset` close it.
- Tool lifecycle: `pre_tool_call` and `post_tool_call` become NeMo Flow tool
  start/end events with tool names, arguments, results, status, task IDs, and
  tool call IDs when Hermes provides them.
- Subagent completion: `subagent_stop` becomes a subagent end event. On Hermes
  versions without the observer-grade middleware additions, the sidecar only
  sees completion because `subagent_start` is absent.
- LLM traffic: when Hermes is run through the NeMo Flow sidecar gateway, the
  gateway captures request payloads, response payloads, tool calls, provider
  `usage`, cached-token fields, and timing.
- ATIF export: sidecar ATIF includes `ATIF-v1.6`, `agent.name = "hermes"`,
  user/agent/system steps, tool observations, `extra.llm_request`, and
  provider usage metrics where the gateway observes them.

This is comprehensive for gateway-routed LLM calls plus shell-hook lifecycle and
tool events. It is not comprehensive for Hermes runtime internals:

- Tool lineage is not always nested under the exact LLM span that produced the
  tool call.
- Direct Hermes LLM calls that do not go through the sidecar gateway do not get
  full NeMo Flow LLM spans from the sidecar.
- Native Hermes `pre_llm_call` and `post_llm_call` are per-turn hooks, not
  request/response spans. Installing them as generic sidecar hooks would create
  noisy ATIF system steps unless they are mapped intentionally.
- The sidecar requires a separate HTTP process and hook-forward commands.

## Native Hermes Middleware

Hermes has two extension surfaces that can observe agent execution without a
sidecar process:

- Python plugins are loaded by `hermes_cli.plugins.PluginManager` and register
  callbacks with `ctx.register_hook(...)`.
- Shell hooks are loaded from `.hermes/config.yaml` and are bridged into the
  same plugin hook manager by `agent.shell_hooks.register_from_config(...)`.

The relevant hook events are:

| Hook | Native payload value | Observability use |
|---|---|---|
| `on_session_start` | `session_id`, platform context | Open a trajectory/session. |
| `on_session_finalize` | final session boundary | Flush and write ATIF. |
| `on_session_reset` | reset boundary | Flush and write/reset ATIF. |
| `pre_llm_call` | user message, conversation history, model, platform | Per-turn user step and context. |
| `post_llm_call` | final assistant response, conversation history, model | Per-turn final agent response. |
| `pre_api_request` | model, provider, base URL, API mode, call index, counts | Best native pre-request LLM/API signal. |
| `post_api_request` | duration, finish reason, usage summary, response model, assistant size | Best native usage/timing signal. |
| `pre_tool_call` | tool name, args, task/session/call IDs | Tool call declaration. |
| `post_tool_call` | tool result and duration | Tool observation. |
| `subagent_stop` | parent session, child role/status/summary/duration | Subagent completion record. |
| `pre_approval_request` / `post_approval_response` | command approval lifecycle | Optional policy/security telemetry. |
| `pre_gateway_dispatch` | gateway message before dispatch | Optional gateway ingress telemetry. |

Native Hermes has stronger in-process visibility than shell-hook forwarding for
API request metadata because `pre_api_request` and `post_api_request` expose
provider, model, API mode, request sizing, duration, finish reason, and usage.
However, current native hooks still do not expose the full API request payload
or complete assistant response object at the request boundary.

## Prototype

The prototype plugin lives at:

```text
integrations/coding-agents/hermes/plugins/nemoflow/
```

It is a Hermes Python plugin, not a Hermes core patch. It registers native hook
callbacks and writes ATIF JSON directly from Hermes middleware events when an
output directory is configured:

```bash
export HERMES_NEMOFLOW_ATIF_DIR=/tmp/hermes-atif
mkdir -p ~/.hermes/plugins
cp -R integrations/coding-agents/hermes/plugins/nemoflow ~/.hermes/plugins/
hermes plugins enable nemoflow
hermes chat
```

When the NeMo Flow Python bindings are importable, the same plugin can also
export OpenInference spans to Phoenix without a sidecar process:

```bash
export PYTHONPATH=/path/to/NeMo-Flow/python${PYTHONPATH:+:$PYTHONPATH}
export HERMES_NEMOFLOW_OPENINFERENCE_ENDPOINT=http://127.0.0.1:4318/v1/traces
export HERMES_NEMOFLOW_OPENINFERENCE_TRANSPORT=http_binary
```

The plugin is intentionally fail-open:

- Missing output directory means no files are written.
- Hook callback errors are contained by Hermes' plugin manager.
- Payloads are JSON-normalized and bounded by `HERMES_NEMOFLOW_MAX_CHARS`
  (default `12000`) to avoid unbounded ATIF fields.
- Missing NeMo Flow Python bindings or OpenInference configuration disables
  Phoenix export without disabling ATIF export.
- Tool call declarations are backfilled from `post_tool_call` when current
  Hermes `pre_tool_call` payloads do not include `session_id` or
  `tool_call_id`.

Expected output:

```text
$HERMES_NEMOFLOW_ATIF_DIR/<session_id>.atif.json
```

## Native Versus Sidecar Parity

With the observer-grade Hermes middleware additions, the native prototype should
match the sidecar for:

- Session boundaries when `on_session_finalize` or `on_session_reset` fires.
- Per-turn user message capture from `pre_llm_call`.
- Final assistant response capture from `post_llm_call`.
- API request/response payloads, timing, stable request IDs, and usage from
  `pre_api_request` / `post_api_request`.
- Prompt-token and completion-token metrics from provider usage data.
- Tool calls and observations from `pre_tool_call` / `post_tool_call`.
- Tool status and error details from `post_tool_call`.
- Subagent lifecycle from `subagent_start` and `subagent_stop`.

Known remaining native gaps:

- Phoenix token-count attributes are limited by the current manual NeMo Flow
  Python LLM API because manual `llm.call_end(...)` cannot attach a normalized
  annotated response. The raw usage payload is still present in OpenInference
  `output.value`, and ATIF token metrics remain first-class.
- `post_llm_call` is still per-turn, so a multi-tool turn can contain multiple
  API request spans plus one final assistant response step.
- Subagent events are exported to Phoenix as marks to avoid corrupting the
  runtime scope stack when parallel child agents start and stop out of order.

The conclusion is that native instrumentation is viable for a no-sidecar
prototype when Hermes exposes observer-grade hook payloads. It now covers the
critical ATIF fields and can send comparable traces to Phoenix, with the token
attribute limitation above tracked separately from ATIF correctness.

## Upstream Hermes Middleware Proposal

To make native NeMo Flow instrumentation comprehensive, propose these upstream
features:

- Add stable `turn_id` and `api_request_id` fields to `pre_llm_call`,
  `pre_api_request`, `post_api_request`, `pre_tool_call`, `post_tool_call`, and
  `post_llm_call`.
- Add full sanitized request payload to `pre_api_request`, including messages,
  tools, model, generation config, provider/API mode, and stream flag.
- Add full sanitized response payload to `post_api_request`, including assistant
  content, tool calls, reasoning fields where present, finish reason, usage,
  provider response model, and error status.
- Add `subagent_start` with parent session, child role, child session/task ID,
  and launch metadata.
- Clarify lifecycle semantics for `on_session_end` versus
  `on_session_finalize` so telemetry plugins can avoid premature flushes.
- Document an observer-only telemetry contract with stable hook payload schemas
  and compatibility expectations.

## Validation Protocol

Use the same prompt or task shape for sidecar and native runs.

1. Run Hermes through `nemo-flow-sidecar run --agent hermes` with ATIF enabled.
2. Save the sidecar ATIF output.
3. Enable the native `nemoflow` Hermes plugin and set
   `HERMES_NEMOFLOW_ATIF_DIR`.
4. Run the same or equivalent Hermes session without the sidecar HTTP process.
5. Compare the two trajectories:
   - `schema_version == "ATIF-v1.6"`
   - `agent.name == "hermes"`
   - user steps are present
   - agent response steps are present
   - API usage metrics are present when provider usage exists
   - prompt-token and completion-token metrics are validated separately
   - `final_metrics` token totals match the sum of per-step token metrics
   - tool calls have matching system observations where call IDs are available
   - subagent completion is represented when delegation occurs
6. Record every missing field as either a plugin bug or an upstream Hermes
   middleware gap.

## Manual Token And ATIF Validation

Manual validation must cover both trajectory shape and token usage. Treat
missing prompt/completion usage as a blocker unless the provider returned no
usage data.

Run the same two prompt shapes through both implementations:

- No-tool prompt: a short deterministic response.
- Tool prompt: inspect one small file or list a tiny directory so Hermes emits a
  tool observation.

Sidecar run:

```bash
export NEMO_FLOW_ATIF_DIR=/tmp/hermes-sidecar-atif
nemo-flow-sidecar run --agent hermes --atif-dir "$NEMO_FLOW_ATIF_DIR" -- hermes chat
```

Native plugin run:

```bash
export HERMES_NEMOFLOW_ATIF_DIR=/tmp/hermes-native-atif
export PYTHONPATH=/path/to/NeMo-Flow/python${PYTHONPATH:+:$PYTHONPATH}
export HERMES_NEMOFLOW_OPENINFERENCE_ENDPOINT=http://127.0.0.1:4318/v1/traces
export HERMES_NEMOFLOW_OPENINFERENCE_TRANSPORT=http_binary
mkdir -p ~/.hermes/plugins
cp -R integrations/coding-agents/hermes/plugins/nemoflow ~/.hermes/plugins/
hermes plugins enable nemoflow
hermes chat
```

For each exported ATIF file, check structure and token totals:

```bash
jq '.schema_version, .agent.name, (.steps | length)' "$ATIF_FILE"
jq '.steps[] | {step_id, source, has_metrics: (.metrics != null), prompt: .metrics.prompt_tokens, completion: .metrics.completion_tokens, cached: .metrics.cached_tokens}' "$ATIF_FILE"
jq '[.steps[].metrics?.prompt_tokens? // empty] | add // 0' "$ATIF_FILE"
jq '[.steps[].metrics?.completion_tokens? // empty] | add // 0' "$ATIF_FILE"
jq '.final_metrics' "$ATIF_FILE"
```

Acceptance criteria:

- ATIF trajectory structure is valid for both sidecar and native plugin runs.
- `prompt_tokens` and `completion_tokens` are present on completed LLM/API
  steps whenever provider usage exists.
- `cached_tokens` is preserved when the provider reports it.
- `final_metrics.total_prompt_tokens`,
  `final_metrics.total_completion_tokens`, and
  `final_metrics.total_cached_tokens` match the per-step metric sums.
- Any sidecar/native difference is explained by provider routing, hook payload
  limits, or a concrete exporter bug.

Targeted checks:

```bash
jq '.schema_version, .agent.name, (.steps | length)' "$ATIF_FILE"
jq '.steps[] | {step_id, source, model_name, has_metrics: (.metrics != null), has_tools: (.tool_calls != null), has_observation: (.observation != null)}' "$ATIF_FILE"
jq '[.steps[].metrics? // empty] | length' "$ATIF_FILE"
```

The committed regression test covers the native hook sequence and the current
Hermes tool-hook asymmetry:

```bash
python3 -m pytest integrations/coding-agents/hermes/tests/test_nemoflow_plugin.py
```
