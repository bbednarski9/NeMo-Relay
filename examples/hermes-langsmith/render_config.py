# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render the Hermes LangSmith example's plugins.toml."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--otlp-traces-endpoint", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    rendered = args.template.read_text(encoding="utf-8")
    replacements = {
        "__OTLP_TRACES_ENDPOINT__": args.otlp_traces_endpoint,
        "__LANGSMITH_PROJECT__": args.project,
    }
    for placeholder, value in replacements.items():
        if rendered.count(placeholder) != 1:
            raise ValueError(f"expected exactly one {placeholder} placeholder")
        rendered = rendered.replace(placeholder, json.dumps(value))

    unresolved = [token for token in replacements if token in rendered]
    if unresolved:
        raise ValueError(f"unresolved placeholders: {', '.join(unresolved)}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
