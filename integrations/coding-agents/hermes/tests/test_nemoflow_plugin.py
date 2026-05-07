# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the prototype Hermes native NeMo Flow plugin."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable


def _load_plugin() -> ModuleType:
    plugin_path = Path(__file__).resolve().parents[1] / "plugins" / "nemoflow" / "__init__.py"
    module_name = "hermes_plugins.nemoflow_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        plugin_path,
        submodule_search_locations=[str(plugin_path.parent)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeContext:
    def __init__(self) -> None:
        self.hooks: list[str] = []

    def register_hook(self, name: str, callback: Callable[..., object]) -> None:
        assert callable(callback)
        self.hooks.append(name)


def test_registers_existing_hermes_hooks() -> None:
    plugin = _load_plugin()
    ctx = _FakeContext()

    plugin.register(ctx)

    assert ctx.hooks == [
        "on_session_start",
        "on_session_finalize",
        "on_session_reset",
        "pre_llm_call",
        "post_llm_call",
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "pre_tool_call",
        "post_tool_call",
        "subagent_start",
        "subagent_stop",
    ]


def test_writes_atif_from_native_hook_sequence(tmp_path: Path, monkeypatch: object) -> None:
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_NEMOFLOW_ATIF_DIR", str(tmp_path))
    plugin.reset_cache_for_tests()

    plugin.on_session_start(session_id="native-test", model="gpt-4.1")
    plugin.on_pre_llm_call(
        session_id="native-test",
        user_message="List files",
        conversation_history=[],
        is_first_turn=True,
        model="gpt-4.1",
        platform="cli",
        turn_id="turn-1",
    )
    plugin.on_pre_api_request(
        session_id="native-test",
        task_id="task-1",
        turn_id="turn-1",
        api_request_id="turn-1:api:1",
        model="gpt-4.1",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_mode="chat",
        api_call_count=1,
        message_count=1,
        tool_count=1,
        approx_input_tokens=10,
        request_char_count=100,
        max_tokens=100,
        request={
            "method": "POST",
            "body": {
                "model": "gpt-4.1",
                "messages": [{"role": "user", "content": "List files"}],
                "tools": [{"function": {"name": "list_files"}}],
            },
        },
    )
    plugin.on_post_api_request(
        session_id="native-test",
        task_id="task-1",
        turn_id="turn-1",
        api_request_id="turn-1:api:1",
        model="gpt-4.1",
        response_model="gpt-4.1",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_mode="chat",
        api_call_count=1,
        api_duration=0.25,
        finish_reason="tool_calls",
        message_count=1,
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 3},
            "total_tokens": 15,
        },
        assistant_content_chars=0,
        assistant_tool_call_count=1,
        response={
            "model": "gpt-4.1",
            "finish_reason": "tool_calls",
            "assistant_message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "list_files",
                            "arguments": '{"path":"."}',
                        },
                    }
                ],
            },
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
            },
        },
    )
    plugin.on_pre_tool_call(
        task_id="task-1",
        turn_id="turn-1",
        tool_name="list_files",
        args={"path": "."},
    )
    plugin.on_post_tool_call(
        session_id="native-test",
        task_id="task-1",
        turn_id="turn-1",
        tool_call_id="call_1",
        tool_name="list_files",
        args={"path": "."},
        result='{"files":["README.md"]}',
        duration_ms=12,
        status="ok",
    )
    plugin.on_post_llm_call(
        session_id="native-test",
        user_message="List files",
        assistant_response="README.md",
        conversation_history=[],
        model="gpt-4.1",
        platform="cli",
        turn_id="turn-1",
    )
    plugin.on_subagent_start(
        parent_session_id="native-test",
        child_session_id="child-session",
        subagent_id="subagent-1",
        task_index=0,
        task_goal="Inspect docs",
        child_role="worker",
        model="gpt-4.1-mini",
        provider="openai",
        api_mode="chat",
        toolsets=["filesystem"],
        depth=1,
    )
    plugin.on_subagent_stop(
        parent_session_id="native-test",
        child_session_id="child-session",
        subagent_id="subagent-1",
        task_index=0,
        child_role="worker",
        child_status="ok",
        child_summary="done",
        duration_ms=42,
        api_calls=1,
        model="gpt-4.1-mini",
        exit_reason="completed",
        tokens={"prompt": 3, "completion": 2},
        tool_trace=[{"tool": "list_files", "is_error": False}],
    )
    plugin.on_session_finalize(session_id="native-test")

    data = json.loads((tmp_path / "native-test.atif.json").read_text())
    assert data["schema_version"] == "ATIF-v1.6"
    assert data["agent"]["name"] == "hermes"
    assert [step["source"] for step in data["steps"]] == [
        "user",
        "agent",
        "system",
        "agent",
        "system",
        "system",
    ]
    assert data["notes"] == (
        "Generated from Hermes native plugin hooks. Observer-grade Hermes "
        "middleware fields are preserved when provided by the runtime."
    )
    assert data["steps"][1]["metrics"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cached_tokens": 3,
        "extra": {"total_tokens": 15},
    }
    assert data["steps"][1]["extra"]["api_request"]["api_request_id"] == "turn-1:api:1"
    assert data["steps"][1]["extra"]["api_request"]["request"]["body"]["messages"] == [
        {"role": "user", "content": "List files"}
    ]
    assert data["steps"][1]["extra"]["api_response"]["response"]["assistant_message"]["tool_calls"][0]["id"] == "call_1"
    assert data["steps"][1]["tool_calls"] == [
        {
            "tool_call_id": "call_1",
            "function_name": "list_files",
            "arguments": {"path": "."},
        }
    ]
    assert data["steps"][2]["observation"]["results"] == [
        {
            "source_call_id": "call_1",
            "content": {"files": ["README.md"]},
        }
    ]
    assert data["steps"][2]["extra"]["invocation"]["status"] == "ok"
    assert data["steps"][4]["message"]["event"] == "subagent_start"
    assert data["steps"][4]["message"]["subagent_id"] == "subagent-1"
    assert data["steps"][5]["message"]["event"] == "subagent_stop"
    assert data["steps"][5]["message"]["tokens"] == {"prompt": 3, "completion": 2}
    assert data["steps"][5]["extra"]["ancestry"]["function_id"] == "subagent-1"
    assert data["final_metrics"] == {
        "total_steps": 6,
        "total_prompt_tokens": 10,
        "total_completion_tokens": 5,
        "total_cached_tokens": 3,
    }


def test_writes_api_request_error_step(tmp_path: Path, monkeypatch: object) -> None:
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_NEMOFLOW_ATIF_DIR", str(tmp_path))
    plugin.reset_cache_for_tests()

    plugin.on_session_start(session_id="native-error", model="gpt-4.1")
    plugin.on_pre_api_request(
        session_id="native-error",
        task_id="task-err",
        turn_id="turn-err",
        api_request_id="turn-err:api:1",
        model="gpt-4.1",
        provider="openai",
        api_call_count=1,
        request={"body": {"messages": [{"role": "user", "content": "fail"}]}},
    )
    plugin.on_api_request_error(
        session_id="native-error",
        task_id="task-err",
        turn_id="turn-err",
        api_request_id="turn-err:api:1",
        model="gpt-4.1",
        provider="openai",
        api_call_count=1,
        api_duration=0.1,
        status_code=429,
        retry_count=1,
        max_retries=3,
        retryable=True,
        reason="rate_limit",
        error={"type": "RateLimitError", "message": "too many requests"},
    )
    plugin.on_session_finalize(session_id="native-error")

    data = json.loads((tmp_path / "native-error.atif.json").read_text())
    assert [step["extra"]["native_hook_event"] for step in data["steps"]] == [
        "api_request_error"
    ]
    step = data["steps"][0]
    assert step["source"] == "agent"
    assert step["message"]["status_code"] == 429
    assert step["extra"]["invocation"]["status"] == "error"
    assert step["extra"]["api_request"]["request"]["body"]["messages"][0]["content"] == "fail"
    assert step["extra"]["api_error"]["error"] == {
        "type": "RateLimitError",
        "message": "too many requests",
    }


def test_subagent_stop_flushes_child_session(tmp_path: Path, monkeypatch: object) -> None:
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_NEMOFLOW_ATIF_DIR", str(tmp_path))
    plugin.reset_cache_for_tests()

    plugin.on_session_start(session_id="parent", model="gpt-4.1")
    plugin.on_session_start(session_id="child", model="gpt-4.1-mini")
    plugin.on_pre_llm_call(
        session_id="child",
        user_message="Check README",
        conversation_history=[],
        model="gpt-4.1-mini",
    )
    plugin.on_subagent_stop(
        parent_session_id="parent",
        child_session_id="child",
        subagent_id="subagent-1",
        child_role="worker",
        child_status="completed",
        child_summary="done",
    )

    child = json.loads((tmp_path / "child.atif.json").read_text())
    assert child["session_id"] == "child"
    assert child["extra"]["boundary"] == "subagent_stop"
    assert child["steps"][0]["extra"]["native_hook_event"] == "pre_llm_call"


def test_optional_openinference_export_uses_nemoflow_runtime(monkeypatch: object) -> None:
    plugin = _load_plugin()
    plugin.reset_cache_for_tests()
    calls: list[tuple[str, object]] = []

    class FakeOpenInferenceConfig:
        def __init__(self) -> None:
            self.transport = ""
            self.service_name = ""
            self.instrumentation_scope = ""
            self.endpoint = ""

    class FakeOpenInferenceSubscriber:
        def __init__(self, config: object) -> None:
            self.config = config

        def register(self, name: str) -> None:
            calls.append(("subscriber.register", name))

        def force_flush(self) -> None:
            calls.append(("subscriber.force_flush", None))

        def deregister(self, name: str) -> None:
            calls.append(("subscriber.deregister", name))

        def shutdown(self) -> None:
            calls.append(("subscriber.shutdown", None))

    class FakeLLMRequest:
        def __init__(self, headers: dict[str, object], content: dict[str, object]) -> None:
            self.headers = headers
            self.content = content

    class FakeScope:
        def push(self, **kwargs: object) -> str:
            calls.append(("scope.push", kwargs))
            return "scope-native"

        def pop(self, handle: object, *, output: object = None) -> None:
            calls.append(("scope.pop", {"handle": handle, "output": output}))

        def event(self, name: str, **kwargs: object) -> None:
            calls.append((f"scope.event:{name}", kwargs))

    class FakeLLM:
        def call(self, name: str, request: FakeLLMRequest, **kwargs: object) -> str:
            calls.append(("llm.call", {"name": name, "request": request.content, **kwargs}))
            return "llm-native"

        def call_end(self, handle: object, response: object, **kwargs: object) -> None:
            calls.append(("llm.call_end", {"handle": handle, "response": response, **kwargs}))

    class FakeTools:
        def call(self, name: str, args: object, **kwargs: object) -> str:
            calls.append(("tools.call", {"name": name, "args": args, **kwargs}))
            return "tool-native"

        def call_end(self, handle: object, result: object, **kwargs: object) -> None:
            calls.append(("tools.call_end", {"handle": handle, "result": result, **kwargs}))

    fake_nemo_flow = SimpleNamespace(
        OpenInferenceConfig=FakeOpenInferenceConfig,
        OpenInferenceSubscriber=FakeOpenInferenceSubscriber,
        LLMRequest=FakeLLMRequest,
        ScopeType=SimpleNamespace(Agent="agent"),
        scope=FakeScope(),
        llm=FakeLLM(),
        tools=FakeTools(),
    )
    monkeypatch.setitem(sys.modules, "nemo_flow", fake_nemo_flow)
    monkeypatch.setenv(
        "HERMES_NEMOFLOW_OPENINFERENCE_ENDPOINT",
        "http://127.0.0.1:4318/v1/traces",
    )

    plugin.on_session_start(session_id="native-oi", model="gpt-4.1", platform="cli")
    plugin.on_pre_api_request(
        session_id="native-oi",
        task_id="task-1",
        turn_id="turn-1",
        api_request_id="turn-1:api:1",
        model="gpt-4.1",
        provider="openai",
        request={"body": {"messages": [{"role": "user", "content": "hi"}]}},
    )
    plugin.on_post_api_request(
        session_id="native-oi",
        task_id="task-1",
        turn_id="turn-1",
        api_request_id="turn-1:api:1",
        model="gpt-4.1",
        provider="openai",
        finish_reason="stop",
        usage={"prompt_tokens": 4, "completion_tokens": 2},
        response={"usage": {"prompt_tokens": 4, "completion_tokens": 2}},
    )
    plugin.on_pre_tool_call(
        session_id="native-oi",
        task_id="task-1",
        turn_id="turn-1",
        tool_call_id="call_1",
        tool_name="list_files",
        args={"path": "."},
    )
    plugin.on_post_tool_call(
        session_id="native-oi",
        task_id="task-1",
        turn_id="turn-1",
        tool_call_id="call_1",
        tool_name="list_files",
        args={"path": "."},
        result={"files": ["README.md"]},
        status="ok",
    )
    plugin.on_subagent_start(
        parent_session_id="native-oi",
        subagent_id="subagent-1",
        child_role="worker",
        task_goal="Inspect",
    )
    plugin.on_session_finalize(session_id="native-oi")

    names = [name for name, _payload in calls]
    assert names.count("subscriber.register") == 1
    assert "scope.push" in names
    assert "llm.call" in names
    assert "llm.call_end" in names
    llm_end = next(payload for name, payload in calls if name == "llm.call_end")
    assert llm_end["annotated_response"] == {
        "model": "gpt-4.1",
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }
    assert "tools.call" in names
    assert "tools.call_end" in names
    assert "scope.event:hermes.subagent_start" in names
    assert "scope.pop" in names
    assert "subscriber.force_flush" in names
