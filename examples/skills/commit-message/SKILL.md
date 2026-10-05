---
name: commit-message
description: Writes a git commit message for the staged changes, in the style the repository already uses. Use when the user asks for a commit message, or asks you to commit their changes.
license: MIT
---
# Commit messages

1. Run `scripts/staged.sh` from this skill's folder (with its absolute path). It prints the staged
   files and the subjects of the last 15 commits.
2. If nothing is staged, tell the user and stop. Don't stage files yourself unless they asked.
3. Read the staged diff (`git diff --cached`) of the files that matter.
4. Match the style of the recent subjects: imperative mood or not, prefixes such as `feat:` or
   `fix(scope):` only if the repository uses them, capitalization and length.
5. Write a subject under 72 characters that says what the change does. For anything but a trivial
   change, add a blank line and a short body that says why, wrapped at 72 characters.
6. Show the message. Commit only if the user asked you to: `git commit -F -` with the message on
   standard input.
