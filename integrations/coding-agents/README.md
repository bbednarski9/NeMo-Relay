<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# NeMo Flow Coding-Agent Integrations

This directory contains first-party coding-agent integrations that can emit
NeMo Flow observability data from native agent extension points.

## Packages

- `hermes/` contains a native Hermes Python plugin prototype that writes ATIF
  from Hermes plugin middleware without running a sidecar HTTP process.
