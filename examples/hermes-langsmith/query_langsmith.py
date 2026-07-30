# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Query Hermes traces and optionally create a dataset from their LLM runs."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from langsmith import Client


def _mapping(value: Any, fallback_key: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {fallback_key: value}


def main() -> None:
    parser = argparse.ArgumentParser(description="Query a LangSmith project populated by the Hermes example.")
    parser.add_argument("--api-url", default=os.environ.get("LANGSMITH_ENDPOINT"))
    parser.add_argument("--project", default=os.environ.get("LANGSMITH_PROJECT", "hermes-nemo-relay-smoke"))
    parser.add_argument("--since-minutes", type=int, default=30)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--show-payloads", action="store_true")
    parser.add_argument(
        "--create-dataset",
        metavar="NAME",
        help="Create NAME and add recent successful LLM runs. This writes to LangSmith.",
    )
    parser.add_argument("--dataset-limit", type=int, default=5)
    args = parser.parse_args()

    api_key = os.environ.get("LANGSMITH_API_KEY")
    if not args.api_url:
        parser.error("set LANGSMITH_ENDPOINT or pass --api-url")
    if not api_key:
        parser.error("set LANGSMITH_API_KEY")
    if args.since_minutes <= 0 or args.limit <= 0 or args.dataset_limit <= 0:
        parser.error("time windows and limits must be positive")

    client = Client(api_url=args.api_url, api_key=api_key)
    start_time = datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes)
    roots = list(
        client.list_runs(
            project_name=args.project,
            is_root=True,
            start_time=start_time,
            limit=args.limit,
        )
    )
    summaries = []
    for run in roots:
        summary: dict[str, Any] = {
            "id": str(run.id),
            "trace_id": str(run.trace_id),
            "name": run.name,
            "run_type": run.run_type,
            "start_time": run.start_time.isoformat(),
            "error": run.error,
        }
        if args.show_payloads:
            summary["inputs"] = run.inputs
            summary["outputs"] = run.outputs
        summaries.append(summary)
    print(json.dumps({"project": args.project, "root_runs": summaries}, indent=2, default=str))

    if not args.create_dataset:
        return

    llm_runs = list(
        client.list_runs(
            project_name=args.project,
            run_type="llm",
            error=False,
            start_time=start_time,
            limit=args.dataset_limit,
        )
    )
    if not llm_runs:
        raise SystemExit("no successful LLM runs matched the dataset query")

    dataset = client.create_dataset(
        dataset_name=args.create_dataset,
        description=f"Hermes Agent LLM examples exported from LangSmith project {args.project}.",
    )
    examples = [
        {
            "inputs": _mapping(run.inputs, "input"),
            "outputs": _mapping(run.outputs, "output"),
            "metadata": {
                "source_project": args.project,
                "source_run_id": str(run.id),
                "source_trace_id": str(run.trace_id),
            },
        }
        for run in llm_runs
    ]
    client.create_examples(dataset_id=dataset.id, examples=examples)
    print(f"created dataset {args.create_dataset!r} with {len(examples)} examples")


if __name__ == "__main__":
    main()
