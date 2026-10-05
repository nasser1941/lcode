# Examples

Ready-made extensions for lcode. Copy them into a repository (or into `~/.config/lcode/` to use
them everywhere):

| Example | Copy to | Use it |
|---|---|---|
| [`commands/review.md`](commands/review.md) | `.lcode/commands/review.md` | `/review` or `/review the parser` |
| [`skills/commit-message/`](skills/commit-message/) | `.lcode/skills/commit-message/` | ask for a commit message, or `/commit-message` |
| [`api/fix_tests.py`](api/fix_tests.py) | anywhere | `python fix_tests.py path/to/repo`: lcode from Python ([Automation](https://nasser1941.github.io/lcode/automation/)) |

```bash
mkdir -p .lcode/commands .lcode/skills
cp examples/commands/review.md .lcode/commands/
cp -r examples/skills/commit-message .lcode/skills/
```

See [Commands and skills](https://nasser1941.github.io/lcode/commands/) for the format.
