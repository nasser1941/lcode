"""Fix a repository's failing tests with lcode, from Python.

    python fix_tests.py path/to/repo

lcode may edit files, and run the test command only: every other shell command is refused,
because nobody is there to ask.
"""

import sys

from lcode import Session

TEST_COMMANDS = ("pytest", "python -m pytest", "python3 -m pytest")


def approve(request: dict) -> bool:
    return request["kind"] == "bash" and request["target"].startswith(TEST_COMMANDS)


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    with Session(repo, permission_mode="auto-edit", approve=approve, max_steps=40) as session:
        for event in session.stream("Run the tests, fix the code until they pass, and run them again."):
            if event["type"] == "tool_result":
                mark = "✗" if event["error"] else "✓"
                print(f"{mark} {event['name']}: {event['output'].splitlines()[0][:80] if event['output'] else ''}")
            elif event["type"] == "result":
                print(f"\n{event['status']} in {event['seconds']:.0f}s, {event['usage']['requests']} model requests")
                for change in event["files_changed"]:
                    print(f"  {change['status']} {change['path']}")
                print(f"\n{event['text']}")
                return 0 if event["status"] == "success" else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
