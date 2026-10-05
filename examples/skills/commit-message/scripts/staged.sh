#!/bin/sh
# The staged changes and recent commit subjects, for a commit message in the repository's style.
set -e
echo "Staged files:"
git diff --cached --stat
echo
echo "Recent commit subjects:"
git log --format='%s' -15
