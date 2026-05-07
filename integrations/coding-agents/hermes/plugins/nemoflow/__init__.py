"""NeMo Flow native ATIF and OpenInference prototype for Hermes plugin hooks.

This plugin uses Hermes' Python plugin middleware and writes ATIF-v1.6 JSON
without starting the NeMo Flow sidecar HTTP process. When the optional NeMo Flow
Python bindings are importable and an OpenInference endpoint is configured, it
also emits native NeMo Flow lifecycle events for Phoenix/OpenInference export.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "ATIF-v1.6"
DEFAULT_MAX_CHARS = 12000
_SESSION_LOCK = threading.RLock()
_SESSIONS: Dict[str, "SessionState"] = {}
_PENDING_TOOLS: Dict[str, Dict[str, Any]] = {}
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_NF_IMPORT_ATTEMPTED = False
_NF_MODULE: Any = None
_NF_SESSION_HANDLES: Dict[str, Any] = {}
_NF_LLM_HANDLES: Dict[str, Any] = {}
_NF_TOOL_HANDLES: Dict[str, Any] = {}
_NF_CHILD_SESSION_IDS: set[str] = set()
_NF_OPENINFERENCE_SUBSCRIBER: Any = None
_NF_OPENINFERENCE_CLEANUP_REGISTERED = False
_NF_OPENINFERENCE_NAME = "hermes-native-nemoflow-openinference"


@dataclass
class SessionState:
    session_id: str
    model_name: str = ""
    started_at: str = field(default_factory=lambda: _now_iso())
    steps: List[Dict[str, Any]] = field(default_factory=list)
    pending_api: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    active_tools: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    latest_agent_step_index: Optional[int] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch_seconds() -> float:
    return time.time()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_truthy(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def _max_chars() -> int:
    raw = _env("HERMES_NEMOFLOW_MAX_CHARS")
    if not raw:
        return DEFAULT_MAX_CHARS
    try:
        return max(256, int(raw))
    except ValueError:
        return DEFAULT_MAX_CHARS


def _output_dir() -> Optional[Path]:
    raw = _env("HERMES_NEMOFLOW_ATIF_DIR") or _env("NEMO_FLOW_ATIF_DIR")
    return Path(raw).expanduser() if raw else None


def _session_id_from(kwargs: Dict[str, Any]) -> str:
    for key in ("session_id", "task_id", "parent_session_id"):
        value = kwargs.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return f"hermes-{uuid.uuid4().hex}"


def _api_key(kwargs: Dict[str, Any]) -> str:
    api_request_id = kwargs.get("api_request_id")
    if api_request_id is not None and str(api_request_id).strip():
        return str(api_request_id)
    session_id = _session_id_from(kwargs)
    task_id = str(kwargs.get("task_id") or "")
    api_call_count = str(kwargs.get("api_call_count") or "")
    return f"{session_id}:{task_id}:{api_call_count}"


def _get_session(session_id: str, model_name: str = "") -> SessionState:
    with _SESSION_LOCK:
        state = _SESSIONS.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id, model_name=model_name or "")
            _SESSIONS[session_id] = state
        elif model_name and not state.model_name:
            state.model_name = model_name
        return state


def _safe_value(value: Any) -> Any:
    max_chars = _max_chars()
    normalized = _jsonable(value)
    try:
        encoded = json.dumps(normalized, ensure_ascii=False, default=str)
    except TypeError:
        normalized = str(value)
        encoded = json.dumps(normalized)
    if len(encoded) <= max_chars:
        return normalized
    return {
        "_truncated_json": encoded[:max_chars],
        "_original_chars": len(encoded),
    }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return _jsonable(vars(value))
        except Exception:
            pass
    return str(value)


def _maybe_parse_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except Exception:
        return value


def _nemo_flow() -> Any:
    global _NF_IMPORT_ATTEMPTED, _NF_MODULE
    if _NF_IMPORT_ATTEMPTED:
        return _NF_MODULE
    _NF_IMPORT_ATTEMPTED = True
    try:
        import nemo_flow  # type: ignore[import-not-found]
    except ImportError as exc:
        if getattr(exc, "name", None) not in ("nemo_flow", None):
            raise
        _NF_MODULE = None
        return None
    _NF_MODULE = nemo_flow
    return _NF_MODULE


def _openinference_enabled() -> bool:
    return _env_truthy("HERMES_NEMOFLOW_OPENINFERENCE_ENABLED") or bool(
        _env("HERMES_NEMOFLOW_OPENINFERENCE_ENDPOINT")
    )


def _set_optional_attr(obj: Any, attr: str, env_name: str) -> None:
    value = _env(env_name)
    if value:
        setattr(obj, attr, value)


def _ensure_openinference() -> Any:
    global _NF_OPENINFERENCE_SUBSCRIBER, _NF_OPENINFERENCE_CLEANUP_REGISTERED

    if not _openinference_enabled():
        return None
    nf = _nemo_flow()
    if nf is None:
        return None
    if _NF_OPENINFERENCE_SUBSCRIBER is not None:
        return nf

    try:
        config = nf.OpenInferenceConfig()
        config.transport = _env("HERMES_NEMOFLOW_OPENINFERENCE_TRANSPORT", "http_binary")
        config.service_name = _env(
            "HERMES_NEMOFLOW_OPENINFERENCE_SERVICE_NAME",
            "hermes-native-nemoflow",
        )
        config.instrumentation_scope = _env(
            "HERMES_NEMOFLOW_OPENINFERENCE_INSTRUMENTATION_SCOPE",
            "hermes/native-nemoflow",
        )
        _set_optional_attr(config, "endpoint", "HERMES_NEMOFLOW_OPENINFERENCE_ENDPOINT")
        _set_optional_attr(
            config,
            "service_namespace",
            "HERMES_NEMOFLOW_OPENINFERENCE_SERVICE_NAMESPACE",
        )
        _set_optional_attr(
            config,
            "service_version",
            "HERMES_NEMOFLOW_OPENINFERENCE_SERVICE_VERSION",
        )
        timeout = _env("HERMES_NEMOFLOW_OPENINFERENCE_TIMEOUT_MILLIS")
        if timeout:
            config.timeout_millis = int(timeout)
        subscriber = nf.OpenInferenceSubscriber(config)
        subscriber.register(_NF_OPENINFERENCE_NAME)
    except Exception:
        return None

    _NF_OPENINFERENCE_SUBSCRIBER = subscriber
    if not _NF_OPENINFERENCE_CLEANUP_REGISTERED:
        atexit.register(_shutdown_openinference)
        _NF_OPENINFERENCE_CLEANUP_REGISTERED = True
    return nf


def _shutdown_openinference() -> None:
    global _NF_OPENINFERENCE_SUBSCRIBER

    subscriber = _NF_OPENINFERENCE_SUBSCRIBER
    _NF_OPENINFERENCE_SUBSCRIBER = None
    if subscriber is None:
        return
    try:
        subscriber.force_flush()
    except Exception:
        pass
    try:
        subscriber.deregister(_NF_OPENINFERENCE_NAME)
    except Exception:
        pass
    try:
        subscriber.shutdown()
    except Exception:
        pass


def _nf_session_start(session_id: str, model_name: str = "", platform: str = "") -> None:
    if session_id in _NF_CHILD_SESSION_IDS:
        return
    nf = _ensure_openinference()
    if nf is None or session_id in _NF_SESSION_HANDLES:
        return
    try:
        handle = nf.scope.push(
            name=f"hermes-session-{session_id}",
            scope_type=nf.ScopeType.Agent,
            data={
                "session_id": session_id,
                "model": model_name,
                "platform": platform,
                "source": "hermes-native-plugin",
            },
            metadata={"source": "hermes.plugins.nemoflow"},
            input={"session_id": session_id, "model": model_name},
        )
    except Exception:
        return
    _NF_SESSION_HANDLES[session_id] = handle


def _nf_session_finalize(session_id: str, *, boundary: str) -> None:
    nf = _nemo_flow()
    if nf is None:
        return
    for key in [key for key in _NF_LLM_HANDLES if key.startswith(f"{session_id}:")]:
        handle = _NF_LLM_HANDLES.pop(key)
        try:
            nf.llm.call_end(
                handle,
                {"status": f"session_{boundary}_before_api_completion"},
                metadata={"source": "hermes.plugins.nemoflow", "boundary": boundary},
            )
        except Exception:
            pass
    for key in [key for key in _NF_TOOL_HANDLES if key.startswith(f"{session_id}:")]:
        handle = _NF_TOOL_HANDLES.pop(key)
        try:
            nf.tools.call_end(
                handle,
                {"status": f"session_{boundary}_before_tool_completion"},
                metadata={"source": "hermes.plugins.nemoflow", "boundary": boundary},
            )
        except Exception:
            pass
    handle = _NF_SESSION_HANDLES.pop(session_id, None)
    if handle is not None:
        try:
            nf.scope.pop(
                handle,
                output={"session_id": session_id, "boundary": boundary},
            )
        except Exception:
            pass
    if _NF_OPENINFERENCE_SUBSCRIBER is not None:
        try:
            _NF_OPENINFERENCE_SUBSCRIBER.force_flush()
        except Exception:
            pass


def _nf_llm_key(session_id: str, kwargs: Dict[str, Any]) -> str:
    return f"{session_id}:{_api_key(kwargs)}"


def _nf_tool_key(session_id: str, kwargs: Dict[str, Any]) -> str:
    call_id = _tool_call_id(kwargs, generate=False)
    if call_id:
        return f"{session_id}:{call_id}"
    return f"{session_id}:{_tool_signature(kwargs)}"


def _nf_pre_api_request(session_id: str, kwargs: Dict[str, Any], pending: Dict[str, Any]) -> None:
    if session_id in _NF_CHILD_SESSION_IDS:
        return
    model = str(kwargs.get("model") or pending.get("model") or "")
    _nf_session_start(session_id, model_name=model, platform=str(kwargs.get("platform") or ""))
    nf = _ensure_openinference()
    handle = _NF_SESSION_HANDLES.get(session_id)
    if nf is None or handle is None:
        return
    request = {
        "model": model,
        "provider": kwargs.get("provider") or pending.get("provider") or "",
        "base_url": kwargs.get("base_url") or pending.get("base_url") or "",
        "api_mode": kwargs.get("api_mode") or pending.get("api_mode") or "",
        "task_id": kwargs.get("task_id") or pending.get("task_id") or "",
        "turn_id": kwargs.get("turn_id") or pending.get("turn_id") or "",
        "api_request_id": kwargs.get("api_request_id") or pending.get("api_request_id") or "",
        "message_count": kwargs.get("message_count"),
        "tool_count": kwargs.get("tool_count"),
        "approx_input_tokens": kwargs.get("approx_input_tokens"),
        "request_char_count": kwargs.get("request_char_count"),
        "max_tokens": kwargs.get("max_tokens"),
        "request": kwargs.get("request") or pending.get("request"),
    }
    try:
        llm_request = nf.LLMRequest({}, _safe_value(request))
        llm_handle = nf.llm.call(
            str(kwargs.get("provider") or "llm"),
            llm_request,
            handle=handle,
            model_name=model or None,
            data={
                "session_id": session_id,
                "api_request_id": kwargs.get("api_request_id") or pending.get("api_request_id") or "",
            },
            metadata={"source": "hermes.plugins.nemoflow", "hook": "pre_api_request"},
        )
    except Exception:
        return
    _NF_LLM_HANDLES[_nf_llm_key(session_id, kwargs)] = llm_handle


def _nf_post_api_request(session_id: str, kwargs: Dict[str, Any], api_response: Dict[str, Any]) -> None:
    if session_id in _NF_CHILD_SESSION_IDS:
        return
    nf = _ensure_openinference()
    if nf is None:
        return
    handle = _NF_LLM_HANDLES.pop(_nf_llm_key(session_id, kwargs), None)
    if handle is None:
        return
    response = {
        "status": "ok",
        "finish_reason": kwargs.get("finish_reason"),
        "model": kwargs.get("response_model") or kwargs.get("model"),
        "usage": kwargs.get("usage"),
        "api_response": api_response,
    }
    annotated_response = _annotated_response_from_usage(
        model=kwargs.get("response_model") or kwargs.get("model"),
        usage=kwargs.get("usage"),
    )
    call_end_kwargs: Dict[str, Any] = {
        "metadata": {
            "source": "hermes.plugins.nemoflow",
            "hook": "post_api_request",
            "status": "ok",
        }
    }
    if annotated_response is not None:
        call_end_kwargs["annotated_response"] = annotated_response
    try:
        nf.llm.call_end(handle, _safe_value(response), **call_end_kwargs)
    except Exception:
        pass


def _nf_api_request_error(session_id: str, kwargs: Dict[str, Any], pending: Dict[str, Any]) -> None:
    if session_id in _NF_CHILD_SESSION_IDS:
        return
    nf = _ensure_openinference()
    if nf is None:
        return
    handle = _NF_LLM_HANDLES.pop(_nf_llm_key(session_id, kwargs), None)
    if handle is None:
        return
    error = kwargs.get("error") or {}
    try:
        nf.llm.call_end(
            handle,
            _safe_value(
                {
                    "status": "error",
                    "error": error,
                    "status_code": kwargs.get("status_code"),
                    "retry_count": kwargs.get("retry_count"),
                    "max_retries": kwargs.get("max_retries"),
                    "retryable": kwargs.get("retryable"),
                    "reason": kwargs.get("reason"),
                    "request": kwargs.get("request") or pending.get("request"),
                }
            ),
            metadata={
                "source": "hermes.plugins.nemoflow",
                "hook": "api_request_error",
                "status": "error",
            },
        )
    except Exception:
        pass


def _nf_pre_tool_call(session_id: str, kwargs: Dict[str, Any], active: Dict[str, Any]) -> None:
    if session_id in _NF_CHILD_SESSION_IDS:
        return
    _nf_session_start(session_id)
    nf = _ensure_openinference()
    handle = _NF_SESSION_HANDLES.get(session_id)
    if nf is None or handle is None:
        return
    tool_name = str(active.get("tool_name") or kwargs.get("tool_name") or "unknown_tool")
    try:
        tool_handle = nf.tools.call(
            tool_name,
            _safe_value(active.get("args") if "args" in active else kwargs.get("args") or {}),
            handle=handle,
            tool_call_id=_tool_call_id(kwargs, generate=False) or None,
            data={
                "session_id": session_id,
                "task_id": kwargs.get("task_id") or active.get("task_id") or "",
                "turn_id": kwargs.get("turn_id") or active.get("turn_id") or "",
            },
            metadata={"source": "hermes.plugins.nemoflow", "hook": "pre_tool_call"},
        )
    except Exception:
        return
    _NF_TOOL_HANDLES[_nf_tool_key(session_id, kwargs)] = tool_handle


def _nf_post_tool_call(session_id: str, kwargs: Dict[str, Any], result: Any, status: str) -> None:
    if session_id in _NF_CHILD_SESSION_IDS:
        return
    nf = _ensure_openinference()
    if nf is None:
        return
    handle = _NF_TOOL_HANDLES.pop(_nf_tool_key(session_id, kwargs), None)
    if handle is None:
        _nf_pre_tool_call(
            session_id,
            kwargs,
            {
                "tool_name": kwargs.get("tool_name") or "unknown_tool",
                "args": kwargs.get("args") or {},
                "task_id": kwargs.get("task_id") or "",
                "turn_id": kwargs.get("turn_id") or "",
            },
        )
        handle = _NF_TOOL_HANDLES.pop(_nf_tool_key(session_id, kwargs), None)
    if handle is None:
        return
    try:
        nf.tools.call_end(
            handle,
            _safe_value(result),
            data={
                "status": status,
                "duration_ms": kwargs.get("duration_ms"),
                "error_message": kwargs.get("error_message"),
            },
            metadata={
                "source": "hermes.plugins.nemoflow",
                "hook": "post_tool_call",
                "status": status,
            },
        )
    except Exception:
        pass


def _nf_mark(session_id: str, name: str, payload: Dict[str, Any]) -> None:
    nf = _ensure_openinference()
    handle = _NF_SESSION_HANDLES.get(session_id)
    if nf is None or handle is None:
        return
    try:
        nf.scope.event(
            name,
            handle=handle,
            data=_safe_value(payload),
            metadata={"source": "hermes.plugins.nemoflow"},
        )
    except Exception:
        pass


def _ancestry(function_id: str, function_name: str, parent_id: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "function_id": function_id,
        "function_name": function_name,
    }
    if parent_id:
        result["parent_id"] = parent_id
        result["parent_name"] = "hermes"
    return result


def _add_step(
    state: SessionState,
    source: str,
    message: Any,
    *,
    model_name: str = "",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    observation: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    step: Dict[str, Any] = {
        "step_id": 0,
        "source": source,
        "message": _safe_value(message),
        "timestamp": _now_iso(),
    }
    model = model_name or state.model_name
    if model:
        step["model_name"] = model
    if tool_calls:
        step["tool_calls"] = tool_calls
    if observation:
        step["observation"] = observation
    if metrics:
        step["metrics"] = metrics
    if extra:
        step["extra"] = _safe_value(extra)
    state.steps.append(step)
    if source == "agent":
        state.latest_agent_step_index = len(state.steps) - 1
    return step


def _latest_agent_step(state: SessionState) -> Optional[Dict[str, Any]]:
    if state.latest_agent_step_index is None:
        return None
    if state.latest_agent_step_index >= len(state.steps):
        return None
    return state.steps[state.latest_agent_step_index]


def _metrics_from_usage(usage: Any) -> Optional[Dict[str, Any]]:
    usage = _maybe_parse_json_string(usage)
    if not isinstance(usage, dict):
        return None

    prompt = _first_u64(usage, ("prompt_tokens", "input_tokens"))
    completion = _first_u64(usage, ("completion_tokens", "output_tokens"))
    cached = (
        _first_u64(usage, ("cached_tokens",))
        or _nested_u64(usage, ("prompt_tokens_details", "cached_tokens"))
        or _sum_u64(usage, ("cache_read_input_tokens", "cache_creation_input_tokens"))
    )
    cost = _float_or_none(usage.get("cost_usd"))

    known = {
        "prompt_tokens",
        "input_tokens",
        "completion_tokens",
        "output_tokens",
        "cached_tokens",
        "prompt_tokens_details",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cost_usd",
    }
    extra = {k: _safe_value(v) for k, v in usage.items() if k not in known}

    metrics: Dict[str, Any] = {}
    if prompt is not None:
        metrics["prompt_tokens"] = prompt
    if completion is not None:
        metrics["completion_tokens"] = completion
    if cached is not None:
        metrics["cached_tokens"] = cached
    if cost is not None:
        metrics["cost_usd"] = cost
    if extra:
        metrics["extra"] = extra

    return metrics or None


def _annotated_response_from_usage(*, model: Any, usage: Any) -> Optional[Dict[str, Any]]:
    usage = _maybe_parse_json_string(usage)
    if not isinstance(usage, dict):
        return None

    prompt = _first_u64(usage, ("prompt_tokens", "input_tokens"))
    completion = _first_u64(usage, ("completion_tokens", "output_tokens"))
    total = _first_u64(usage, ("total_tokens",))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    cache_read = (
        _first_u64(usage, ("cache_read_tokens", "cache_read_input_tokens"))
        or _nested_u64(usage, ("prompt_tokens_details", "cached_tokens"))
        or _first_u64(usage, ("cached_tokens",))
    )
    cache_write = _first_u64(usage, ("cache_write_tokens", "cache_creation_input_tokens"))

    annotated_usage: Dict[str, int] = {}
    if prompt is not None:
        annotated_usage["prompt_tokens"] = prompt
    if completion is not None:
        annotated_usage["completion_tokens"] = completion
    if total is not None:
        annotated_usage["total_tokens"] = total
    if cache_read is not None:
        annotated_usage["cache_read_tokens"] = cache_read
    if cache_write is not None:
        annotated_usage["cache_write_tokens"] = cache_write
    if not annotated_usage:
        return None

    annotated_response: Dict[str, Any] = {"usage": annotated_usage}
    if model:
        annotated_response["model"] = str(model)
    return annotated_response


def _first_u64(values: Dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value >= 0:
            return int(value)
    return None


def _nested_u64(values: Dict[str, Any], path: tuple[str, ...]) -> Optional[int]:
    current: Any = values
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, bool):
        return None
    if isinstance(current, int) and current >= 0:
        return current
    if isinstance(current, float) and current >= 0:
        return int(current)
    return None


def _sum_u64(values: Dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    total = 0
    found = False
    for key in keys:
        value = _first_u64(values, (key,))
        if value is not None:
            total += value
            found = True
    return total if found else None


def _float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _tool_call_id(kwargs: Dict[str, Any], *, generate: bool = True) -> str:
    value = kwargs.get("tool_call_id") or kwargs.get("call_id") or ""
    if value:
        return str(value)
    return f"tool-{uuid.uuid4().hex}" if generate else ""


def _tool_signature(kwargs: Dict[str, Any]) -> str:
    sid = _session_id_from(kwargs)
    tool_name = str(kwargs.get("tool_name") or "unknown_tool")
    args = _safe_value(kwargs.get("args") or {})
    try:
        encoded_args = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        encoded_args = str(args)
    return f"{sid}:{kwargs.get('task_id') or ''}:{tool_name}:{encoded_args}"


def _has_tool_call(step: Dict[str, Any], call_id: str) -> bool:
    return any(item.get("tool_call_id") == call_id for item in step.get("tool_calls") or [])


def _append_tool_call(state: SessionState, tool_call: Dict[str, Any]) -> None:
    step = _latest_agent_step(state)
    if step is None:
        step = _add_step(
            state,
            "agent",
            "",
            extra={
                "ancestry": _ancestry(state.session_id, "hermes"),
                "native_hook_event": "pre_tool_call_without_api_step",
            },
        )
    existing = step.setdefault("tool_calls", [])
    call_id = tool_call["tool_call_id"]
    if not any(item.get("tool_call_id") == call_id for item in existing):
        existing.append(tool_call)


def on_session_start(*, session_id: str = "", model: str = "", **kwargs: Any) -> None:
    sid = session_id or _session_id_from(kwargs)
    with _SESSION_LOCK:
        _get_session(sid, model_name=model)
    _nf_session_start(sid, model_name=model, platform=str(kwargs.get("platform") or ""))


def on_pre_llm_call(**kwargs: Any) -> None:
    sid = _session_id_from(kwargs)
    model = str(kwargs.get("model") or "")
    with _SESSION_LOCK:
        state = _get_session(sid, model_name=model)
        history = _safe_value(kwargs.get("conversation_history") or [])
        user_message = kwargs.get("user_message") or ""
        llm_request = {
            "model": model,
            "messages": [*history, {"role": "user", "content": user_message}]
            if isinstance(history, list)
            else [{"role": "user", "content": user_message}],
            "platform": kwargs.get("platform") or "",
            "is_first_turn": bool(kwargs.get("is_first_turn")),
            "turn_id": kwargs.get("turn_id") or "",
        }
        _add_step(
            state,
            "user",
            user_message,
            model_name=model,
            extra={
                "ancestry": _ancestry(sid, "hermes"),
                "llm_request": llm_request,
                "native_hook_event": "pre_llm_call",
                "turn_id": kwargs.get("turn_id") or "",
            },
        )


def on_post_llm_call(**kwargs: Any) -> None:
    sid = _session_id_from(kwargs)
    model = str(kwargs.get("model") or "")
    assistant_response = kwargs.get("assistant_response")
    if assistant_response is None:
        return
    with _SESSION_LOCK:
        state = _get_session(sid, model_name=model)
        _add_step(
            state,
            "agent",
            assistant_response,
            model_name=model,
            extra={
                "ancestry": _ancestry(sid, "hermes"),
                "native_hook_event": "post_llm_call",
                "platform": kwargs.get("platform") or "",
                "turn_id": kwargs.get("turn_id") or "",
            },
        )


def on_pre_api_request(**kwargs: Any) -> None:
    sid = _session_id_from(kwargs)
    model = str(kwargs.get("model") or "")
    pending_record: Dict[str, Any]
    with _SESSION_LOCK:
        state = _get_session(sid, model_name=model)
        key = _api_key(kwargs)
        pending_record = {
            "start_timestamp": _epoch_seconds(),
            "task_id": kwargs.get("task_id") or "",
            "turn_id": kwargs.get("turn_id") or "",
            "api_request_id": kwargs.get("api_request_id") or key,
            "session_id": sid,
            "platform": kwargs.get("platform") or "",
            "model": model,
            "provider": kwargs.get("provider") or "",
            "base_url": kwargs.get("base_url") or "",
            "api_mode": kwargs.get("api_mode") or "",
            "api_call_count": kwargs.get("api_call_count"),
            "message_count": kwargs.get("message_count"),
            "tool_count": kwargs.get("tool_count"),
            "approx_input_tokens": kwargs.get("approx_input_tokens"),
            "request_char_count": kwargs.get("request_char_count"),
            "max_tokens": kwargs.get("max_tokens"),
        }
        if "request" in kwargs:
            pending_record["request"] = _safe_value(kwargs.get("request"))
        state.pending_api[key] = pending_record
    _nf_pre_api_request(sid, kwargs, pending_record)


def on_post_api_request(**kwargs: Any) -> None:
    sid = _session_id_from(kwargs)
    model = str(kwargs.get("response_model") or kwargs.get("model") or "")
    pending: Dict[str, Any]
    api_response: Dict[str, Any]
    with _SESSION_LOCK:
        state = _get_session(sid, model_name=model)
        key = _api_key(kwargs)
        pending = state.pending_api.pop(key, {})
        metrics = _metrics_from_usage(kwargs.get("usage"))
        finish_reason = kwargs.get("finish_reason")
        response_summary = {
            "finish_reason": finish_reason,
            "assistant_content_chars": kwargs.get("assistant_content_chars"),
            "assistant_tool_call_count": kwargs.get("assistant_tool_call_count"),
        }
        invocation = {
            "start_timestamp": pending.get("start_timestamp"),
            "end_timestamp": _epoch_seconds(),
            "invocation_id": key,
            "status": "ok" if finish_reason else "unknown",
            "framework": "hermes",
        }
        api_response = {
            "api_duration": kwargs.get("api_duration"),
            "finish_reason": finish_reason,
            "response_model": kwargs.get("response_model"),
            "usage": _safe_value(kwargs.get("usage")),
        }
        if "response" in kwargs:
            api_response["response"] = _safe_value(kwargs.get("response"))
        _add_step(
            state,
            "agent",
            response_summary,
            model_name=model,
            metrics=metrics,
            extra={
                "ancestry": _ancestry(key, "hermes_api_request", parent_id=sid),
                "invocation": invocation,
                "api_request": pending,
                "api_response": api_response,
                "native_hook_event": "post_api_request",
                "turn_id": kwargs.get("turn_id") or pending.get("turn_id") or "",
                "api_request_id": kwargs.get("api_request_id") or pending.get("api_request_id") or key,
            },
        )
    _nf_post_api_request(sid, kwargs, api_response)


def on_api_request_error(**kwargs: Any) -> None:
    sid = _session_id_from(kwargs)
    model = str(kwargs.get("model") or "")
    pending: Dict[str, Any]
    with _SESSION_LOCK:
        state = _get_session(sid, model_name=model)
        key = _api_key(kwargs)
        pending = state.pending_api.pop(key, {})
        error = kwargs.get("error") or {}
        if not isinstance(error, dict):
            error = {"message": str(error)}
        error_summary = {
            "error": _safe_value(error),
            "status_code": kwargs.get("status_code"),
            "retry_count": kwargs.get("retry_count"),
            "max_retries": kwargs.get("max_retries"),
            "retryable": kwargs.get("retryable"),
            "reason": kwargs.get("reason"),
        }
        invocation = {
            "start_timestamp": pending.get("start_timestamp"),
            "end_timestamp": _epoch_seconds(),
            "invocation_id": key,
            "status": "error",
            "framework": "hermes",
        }
        if "request" in kwargs and "request" not in pending:
            pending["request"] = _safe_value(kwargs.get("request"))
        _add_step(
            state,
            "agent",
            error_summary,
            model_name=model,
            extra={
                "ancestry": _ancestry(key, "hermes_api_request", parent_id=sid),
                "invocation": invocation,
                "api_request": pending,
                "api_error": {
                    "api_duration": kwargs.get("api_duration"),
                    "status_code": kwargs.get("status_code"),
                    "retry_count": kwargs.get("retry_count"),
                    "max_retries": kwargs.get("max_retries"),
                    "retryable": kwargs.get("retryable"),
                    "reason": kwargs.get("reason"),
                    "error": _safe_value(error),
                },
                "native_hook_event": "api_request_error",
                "turn_id": kwargs.get("turn_id") or pending.get("turn_id") or "",
                "api_request_id": kwargs.get("api_request_id") or pending.get("api_request_id") or key,
            },
        )
    _nf_api_request_error(sid, kwargs, pending)


def on_pre_tool_call(**kwargs: Any) -> None:
    sid = _session_id_from(kwargs)
    active: Dict[str, Any]
    with _SESSION_LOCK:
        state = _get_session(sid)
        call_id = _tool_call_id(kwargs, generate=False)
        tool_name = str(kwargs.get("tool_name") or "unknown_tool")
        args = _safe_value(kwargs.get("args") or {})
        active = {
            "start_timestamp": _epoch_seconds(),
            "tool_name": tool_name,
            "args": args,
            "task_id": kwargs.get("task_id") or "",
            "turn_id": kwargs.get("turn_id") or "",
        }
        if call_id:
            _append_tool_call(
                state,
                {
                    "tool_call_id": call_id,
                    "function_name": tool_name,
                    "arguments": args,
                },
            )
            state.active_tools[call_id] = active
        else:
            _PENDING_TOOLS[_tool_signature(kwargs)] = active
    _nf_pre_tool_call(sid, kwargs, active)


def on_post_tool_call(**kwargs: Any) -> None:
    sid = _session_id_from(kwargs)
    result: Any
    status: str
    with _SESSION_LOCK:
        state = _get_session(sid)
        call_id = _tool_call_id(kwargs)
        active = state.active_tools.pop(call_id, {}) or _PENDING_TOOLS.pop(_tool_signature(kwargs), {})
        tool_name = str(active.get("tool_name") or kwargs.get("tool_name") or "unknown_tool")
        args = active.get("args")
        if args is None:
            args = _safe_value(kwargs.get("args") or {})
        latest = _latest_agent_step(state)
        if latest is None or not _has_tool_call(latest, call_id):
            _append_tool_call(
                state,
                {
                    "tool_call_id": call_id,
                    "function_name": tool_name,
                    "arguments": args,
                },
            )
        result = _safe_value(_maybe_parse_json_string(kwargs.get("result")))
        error_message = kwargs.get("error_message")
        if result is None and error_message:
            result = {"error": error_message}
        end_ts = _epoch_seconds()
        start_ts = active.get("start_timestamp")
        if start_ts is None and kwargs.get("duration_ms") is not None:
            try:
                start_ts = end_ts - (float(kwargs["duration_ms"]) / 1000.0)
            except Exception:
                start_ts = None
        status = str(kwargs.get("status") or ("error" if error_message else "ok"))
        _add_step(
            state,
            "system",
            result,
            observation={
                "results": [
                    {
                        "source_call_id": call_id,
                        "content": result,
                    }
                ]
            },
            extra={
                "ancestry": _ancestry(call_id, tool_name, parent_id=sid),
                "invocation": {
                    "start_timestamp": start_ts,
                    "end_timestamp": end_ts,
                    "invocation_id": call_id,
                    "status": status,
                    "framework": "hermes",
                },
                "tool": {
                    "task_id": kwargs.get("task_id") or active.get("task_id") or "",
                    "turn_id": kwargs.get("turn_id") or active.get("turn_id") or "",
                    "duration_ms": kwargs.get("duration_ms"),
                    "status": status,
                    "error_message": error_message,
                },
                "native_hook_event": "post_tool_call",
            },
        )
    _nf_post_tool_call(sid, kwargs, result, status)


def on_subagent_start(**kwargs: Any) -> None:
    sid = str(kwargs.get("parent_session_id") or _session_id_from(kwargs))
    child_session_id = kwargs.get("child_session_id")
    if child_session_id:
        _NF_CHILD_SESSION_IDS.add(str(child_session_id))
    message: Dict[str, Any]
    with _SESSION_LOCK:
        state = _get_session(sid)
        child_role = str(kwargs.get("child_role") or "subagent")
        child_id = str(
            kwargs.get("subagent_id")
            or kwargs.get("child_session_id")
            or f"{sid}:{child_role}:{len(state.steps)}"
        )
        message = {
            "event": "subagent_start",
            "child_role": child_role,
            "task_goal": kwargs.get("task_goal"),
            "task_index": kwargs.get("task_index"),
            "child_session_id": kwargs.get("child_session_id"),
            "subagent_id": kwargs.get("subagent_id"),
            "model": kwargs.get("model"),
            "provider": kwargs.get("provider"),
            "api_mode": kwargs.get("api_mode"),
            "toolsets": _safe_value(kwargs.get("toolsets")),
            "depth": kwargs.get("depth"),
        }
        _add_step(
            state,
            "system",
            message,
            extra={
                "ancestry": _ancestry(child_id, f"subagent:{child_role}", parent_id=sid),
                "native_hook_event": "subagent_start",
                "parent_subagent_id": kwargs.get("parent_subagent_id"),
            },
        )
    _nf_mark(sid, "hermes.subagent_start", message)


def on_subagent_stop(**kwargs: Any) -> None:
    sid = str(kwargs.get("parent_session_id") or _session_id_from(kwargs))
    message: Dict[str, Any]
    with _SESSION_LOCK:
        state = _get_session(sid)
        child_role = str(kwargs.get("child_role") or "subagent")
        child_id = str(
            kwargs.get("subagent_id")
            or kwargs.get("child_session_id")
            or f"{sid}:{child_role}:{len(state.steps)}"
        )
        message = {
            "event": "subagent_stop",
            "child_role": child_role,
            "child_status": kwargs.get("child_status"),
            "child_summary": kwargs.get("child_summary"),
            "duration_ms": kwargs.get("duration_ms"),
            "task_index": kwargs.get("task_index"),
            "child_session_id": kwargs.get("child_session_id"),
            "subagent_id": kwargs.get("subagent_id"),
            "api_calls": kwargs.get("api_calls"),
            "model": kwargs.get("model"),
            "exit_reason": kwargs.get("exit_reason"),
            "tokens": _safe_value(kwargs.get("tokens")),
            "tool_trace": _safe_value(kwargs.get("tool_trace")),
            "error": kwargs.get("error"),
        }
        _add_step(
            state,
            "system",
            message,
            extra={
                "ancestry": _ancestry(child_id, f"subagent:{child_role}", parent_id=sid),
                "invocation": {
                    "invocation_id": child_id,
                    "status": kwargs.get("child_status") or "unknown",
                    "framework": "hermes",
                },
                "native_hook_event": "subagent_stop",
                "parent_subagent_id": kwargs.get("parent_subagent_id"),
            },
        )
    _nf_mark(sid, "hermes.subagent_stop", message)
    _flush_child_session_if_present(sid, kwargs.get("child_session_id"))


def on_session_finalize(*, session_id: str = "", **kwargs: Any) -> None:
    sid = session_id or _session_id_from(kwargs)
    _flush_session(sid, boundary="on_session_finalize")


def on_session_reset(*, session_id: str = "", **kwargs: Any) -> None:
    sid = session_id or _session_id_from(kwargs)
    _flush_session(sid, boundary="on_session_reset")


def _flush_session(session_id: str, *, boundary: str) -> None:
    output_dir = _output_dir()
    if output_dir is None:
        _nf_session_finalize(session_id, boundary=boundary)
        return
    with _SESSION_LOCK:
        state = _SESSIONS.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id)
            _SESSIONS[session_id] = state
        trajectory = _trajectory(state, boundary=boundary)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _SAFE_FILENAME_RE.sub("_", session_id).strip("._") or "hermes-session"
    path = output_dir / f"{safe_name}.atif.json"
    tmp = output_dir / f".{safe_name}.atif.json.tmp"
    tmp.write_text(json.dumps(trajectory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    _nf_session_finalize(session_id, boundary=boundary)


def _flush_child_session_if_present(parent_session_id: str, child_session_id: Any) -> None:
    if child_session_id is None:
        return
    child_sid = str(child_session_id)
    if not child_sid or child_sid == parent_session_id:
        return
    with _SESSION_LOCK:
        exists = child_sid in _SESSIONS
    if exists:
        _flush_session(child_sid, boundary="subagent_stop")
    _NF_CHILD_SESSION_IDS.discard(child_sid)


def _trajectory(state: SessionState, *, boundary: str) -> Dict[str, Any]:
    steps = []
    for index, step in enumerate(state.steps, start=1):
        item = dict(step)
        item["step_id"] = index
        steps.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": state.session_id,
        "agent": {
            "name": "hermes",
            "version": "native-plugin-prototype",
            "model_name": state.model_name or None,
            "extra": {
                "instrumentation": "nemoflow-native-hermes-plugin",
                "boundary": boundary,
                "started_at": state.started_at,
            },
        },
        "steps": steps,
        "notes": (
            "Generated from Hermes native plugin hooks. Observer-grade Hermes "
            "middleware fields are preserved when provided by the runtime."
        ),
        "final_metrics": _final_metrics(steps),
        "extra": {
            "source": "hermes-native-plugin",
            "boundary": boundary,
        },
    }


def _final_metrics(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals: Dict[str, Any] = {"total_steps": len(steps)}
    for metric_key, total_key in (
        ("prompt_tokens", "total_prompt_tokens"),
        ("completion_tokens", "total_completion_tokens"),
        ("cached_tokens", "total_cached_tokens"),
        ("cost_usd", "total_cost_usd"),
    ):
        total: Any = 0.0 if metric_key == "cost_usd" else 0
        found = False
        for step in steps:
            metrics = step.get("metrics") or {}
            value = metrics.get(metric_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += value
                found = True
        if found:
            totals[total_key] = total
    return totals


def register(ctx: Any) -> None:
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("on_session_reset", on_session_reset)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("api_request_error", on_api_request_error)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("subagent_start", on_subagent_start)
    ctx.register_hook("subagent_stop", on_subagent_stop)


def reset_cache_for_tests() -> None:
    global _NF_IMPORT_ATTEMPTED, _NF_MODULE, _NF_OPENINFERENCE_SUBSCRIBER
    global _NF_OPENINFERENCE_CLEANUP_REGISTERED
    with _SESSION_LOCK:
        _SESSIONS.clear()
        _PENDING_TOOLS.clear()
        _NF_IMPORT_ATTEMPTED = False
        _NF_MODULE = None
        _NF_SESSION_HANDLES.clear()
        _NF_LLM_HANDLES.clear()
        _NF_TOOL_HANDLES.clear()
        _NF_CHILD_SESSION_IDS.clear()
        _NF_OPENINFERENCE_SUBSCRIBER = None
        _NF_OPENINFERENCE_CLEANUP_REGISTERED = False
