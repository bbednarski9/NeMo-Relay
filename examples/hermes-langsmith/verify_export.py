# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify local artifacts and the captured OpenInference OTLP request."""

from __future__ import annotations

import argparse
import base64
import collections
import json
from pathlib import Path
from typing import Any


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _verify_atof(artifact_directory: Path) -> int:
    events_path = artifact_directory / "atof" / "events.jsonl"
    events = _read_json_lines(events_path)
    if not events or any(event.get("atof_version") != "0.1" for event in events):
        raise AssertionError(f"{events_path} does not contain only ATOF 0.1 events")

    lifecycles: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for event in events:
        if event.get("kind") == "scope":
            lifecycles[str(event["uuid"])][str(event["scope_category"])] += 1
    if not lifecycles:
        raise AssertionError("ATOF output contains no scope lifecycle events")
    unbalanced = {scope_id: counts for scope_id, counts in lifecycles.items() if counts != {"start": 1, "end": 1}}
    if unbalanced:
        raise AssertionError(f"ATOF output contains unbalanced scope lifecycles: {unbalanced}")
    if not any(event.get("category") == "llm" for event in events):
        raise AssertionError("ATOF output contains no Hermes LLM lifecycle")
    if not any(event.get("category") == "agent" for event in events):
        raise AssertionError("ATOF output contains no Hermes Agent scope")
    return len(events)


def _verify_atif(artifact_directory: Path) -> int:
    trajectories = sorted((artifact_directory / "atif").glob("trajectory-*.json"))
    if not trajectories:
        raise AssertionError("ATIF output contains no trajectory")
    for path in trajectories:
        trajectory = json.loads(path.read_text(encoding="utf-8"))
        if trajectory.get("schema_version") != "ATIF-v1.7":
            raise AssertionError(f"{path} is not an ATIF v1.7 trajectory")
        if not isinstance(trajectory.get("steps"), list) or not trajectory["steps"]:
            raise AssertionError(f"{path} contains no trajectory steps")
    return len(trajectories)


def _verify_provider(artifact_directory: Path) -> None:
    requests = _read_json_lines(artifact_directory / "provider-requests.jsonl")
    if not any(str(request.get("path", "")).endswith("/chat/completions") for request in requests):
        raise AssertionError("Hermes did not call the synthetic OpenAI-compatible provider")


def _verify_otlp(artifact_directory: Path, project: str) -> int:
    records = _read_json_lines(artifact_directory / "otel-capture.jsonl")
    requests = [record for record in records if record.get("type") == "request"]
    if not requests:
        raise AssertionError("the local OTLP collector received no trace requests")

    for request in requests:
        headers = {str(key).lower(): str(value) for key, value in request["headers"].items()}
        if headers.get("x-api-key") != "local-fixture-key":
            raise AssertionError("the OTLP x-api-key header was not resolved through header_env")
        if headers.get("langsmith-project") != project:
            raise AssertionError("the OTLP Langsmith-Project header does not match the configured project")

    payload = b"".join(base64.b64decode(request["body"]) for request in requests)
    required_openinference_strings = (
        b"openinference.span.kind",
        b"input.value",
        b"output.value",
        b"llm.model_name",
        b"gpt-4o-mini",
        b"hermes-agent",
        b"pong",
    )
    missing = [value.decode() for value in required_openinference_strings if value not in payload]
    if missing:
        raise AssertionError(f"OTLP protobuf is missing OpenInference data: {', '.join(missing)}")
    return len(requests)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_directory", type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--skip-otlp-capture", action="store_true")
    args = parser.parse_args()

    event_count = _verify_atof(args.artifact_directory)
    trajectory_count = _verify_atif(args.artifact_directory)
    _verify_provider(args.artifact_directory)
    if args.skip_otlp_capture:
        print(f"verified {event_count} ATOF events and {trajectory_count} ATIF trajectories")
        return
    request_count = _verify_otlp(args.artifact_directory, args.project)
    print(
        f"verified {event_count} ATOF events, {trajectory_count} ATIF trajectories, "
        f"and {request_count} OpenInference OTLP requests"
    )


if __name__ == "__main__":
    main()
