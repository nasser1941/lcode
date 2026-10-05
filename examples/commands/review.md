---
description: review the uncommitted changes for bugs and missing tests
argument-hint: "[path or what to focus on]"
allowed-tools: read_file grep glob list_dir bash
---
Review the uncommitted changes in this repository. $ARGUMENTS

Run `git diff` and `git diff --cached` to see them, and read the code around them where needed.

Report, most important first:
1. Bugs: wrong behavior, unhandled errors and edge cases, with `path:line`.
2. Missing or weak tests for the changed behavior.
3. Anything that doesn't match the code around it.

Don't change any files.
