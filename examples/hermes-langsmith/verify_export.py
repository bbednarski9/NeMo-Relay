# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify local artifacts and the captured OpenInference OTLP request."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
            raise AssertionError("the OTLP x-api-key header was not resolved from OTEL exporter headers")
        if headers.get("langsmith-project") != project:
            raise AssertionError("the OTLP Langsmith-Project header does not match the configured project")

    payload = b"".join(base64.b64decode(request["body"]) for request in requests)
    required_openinference_strings = (
        b"openinference.span.kind",
        b"input.value",
        b"output.value",
        b"llm.model_name",
        b"gpt-4o-mini",
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

    _verify_provider(args.artifact_directory)
    if args.skip_otlp_capture:
        print("verified the deterministic Hermes provider call")
        return
    request_count = _verify_otlp(args.artifact_directory, args.project)
    print(f"verified {request_count} OpenInference OTLP requests")


if __name__ == "__main__":
    main()
