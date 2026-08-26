# GitHub Actions Chain Handoff Bugfix

## Problem

The controller waited for every account group in one job. When the cumulative runtime reached the GitHub Actions job limit, artifact download, XLSX generation, and Telegram delivery never ran.

## Root Cause

`dynamic-controller.yml` called a Python loop that dispatched each group and polled it to completion. Reporting was placed after that loop in the same runner.

## Changes

- The controller now freezes the configured group list and dispatches only the first group.
- Each group finalizer records its exact run ID and dispatches the next group.
- The last group dispatches an independent summary workflow.
- Summary downloads artifacts by exact run ID and continues when a group artifact is missing.
- Report account placeholders are constrained by the frozen group list and account counts.
- Chain state is transferred as Base64 JSON so shells and workflow inputs cannot strip JSON quotes.

## Validation

- Python compilation.
- Full unit test suite, including chain order, 80-group state size, duplicate dispatch, exact run-ID download, missing artifact, and empty frozen chain cases.
- GitHub Actions YAML parsing.
- `git diff --check`.

## Remaining Risk

A workflow that is forcibly cancelled before its finalizer starts cannot dispatch its successor. The controller remains manually runnable to start a new chain.

## Review State

Self-reviewed against duplicate dispatch, missing artifact, empty chain, changed Secrets, manual single-group, and six-hour runtime failure modes.

## Git Snapshot

Pending.

## Rollback

Revert the commit that introduces the chain handoff workflow and restore the prior controller implementation.
