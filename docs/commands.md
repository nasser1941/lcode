# Commands and skills

Teams repeat the same requests: "review this diff against our checklist", "write a migration the
way we do it", the release steps. `AGENTS.md` can't hold all of that, because it goes into every
request and every line costs context. lcode has two ways to keep such instructions ready without
paying for them until they're used.

| | Command | Skill |
|---|---|---|
| What it is | A prompt template | Instructions, plus scripts and reference files if needed |
| Who starts it | You, with `/name args` | The model, when a task matches its description, or you with `/name` |
| Context cost until used | Nothing | Its name and one-line description |
| Format | `<name>.md` | A folder with a `SKILL.md`, in the [Agent Skills](https://agentskills.io) format |

## Commands

A command is a markdown file whose body is the request to send. `$ARGUMENTS` becomes everything
you type after the command's name, and `$1` to `$9` single words. If the template has no
placeholder, what you type is added at the end.

```markdown
---
description: review the uncommitted changes for bugs and missing tests
argument-hint: "[path or what to focus on]"
allowed-tools: read_file grep glob list_dir bash
---
Review the uncommitted changes in this repository. $ARGUMENTS

Run `git diff` and `git diff --cached` to see them, and read the code around them where needed.
Report bugs first, with `path:line`, then missing tests. Don't change any files.
```

```text
❯ /review the parser changes
```

| Frontmatter (all optional) | |
|---|---|
| `description` | Shown in `/help` and completion (default: the first line) |
| `argument-hint` | Shown after the name in `/help`, e.g. `[path]` |
| `allowed-tools` | The only tools the model gets for this request, e.g. `read_file grep bash` (lcode's tool names) |

| Folder | Commands |
|---|---|
| `.lcode/commands/<name>.md` in the repository | For this repository; commit them to share them |
| `~/.config/lcode/commands/<name>.md` | For every repository |

The file name is the command's name. A repository's command overrides one of yours with the same
name. A command called `review`, `commit` or `pr` replaces lcode's [built-in one](git.md), so a team
can use its own checklist; other built-in commands such as `/help` can't be replaced.

## Skills

A skill is a folder with a `SKILL.md`: a frontmatter with a `name` and a `description` that says
what the skill does and when to use it, then instructions. Scripts, templates and reference files
can sit next to it.

```text
.lcode/skills/commit-message/
├── SKILL.md
└── scripts/
    └── staged.sh
```

```markdown
---
name: commit-message
description: Writes a git commit message for the staged changes, in the style the repository
  already uses. Use when the user asks for a commit message, or asks you to commit their changes.
---
# Commit messages

1. Run `scripts/staged.sh` from this skill's folder. It prints the staged files and the subjects
   of the last 15 commits.
2. Match the style of the recent subjects …
```

How lcode uses them, step by step, so they cost almost nothing until needed:

1. At the start of a session, the model gets only each skill's name and description.
2. When a task matches a description, the model calls the `skill` tool. It gets the instructions,
   the skill's folder and the list of files in it, but not their contents.
3. The model reads or runs those files when the instructions call for them. Files in a skill's
   folder can be read even when the [sandbox](sandbox.md) limits the model to the project.

You can also start a skill yourself: `/commit-message` or `/commit-message only the parser
changes`. After the conversation is compacted, the model is told which skills were in use, so it
can load them again.

lcode reads skills from these folders; a repository's skill overrides one of yours with the same
name:

| Folder | |
|---|---|
| `.lcode/skills/`, `.agents/skills/`, `.claude/skills/` in the repository | For this repository |
| `~/.config/lcode/skills/`, `~/.agents/skills/`, `~/.claude/skills/` | For every repository |

`.agents/skills/` is the folder agents share by convention, and `.claude/skills/` is read too, so
skills you already have for other agents work in lcode. If other agents installed many skills you
don't need here, `lcode config set skills lcode` limits lcode to its own folders (`skills off` turns
skills off). However many there are, the list in the model's instructions stays within about 5% of
the context window: with many skills, each description is shortened so that all of them stay
listed. The same skill installed in two folders is used once. The format is the
[Agent Skills specification](https://agentskills.io/specification); like other agents, lcode is
lenient with skills that bend its rules (a name with capital letters, an unquoted colon in a
description) and skips only those without a description. `/help` lists the skills it found and
any it skipped.

## Approving a repository's commands, skills and agents

A repository's commands, skills and [agents](agents.md#custom-agents) steer the model, and skills
can bring scripts. A freshly cloned repository shouldn't be able to do that silently, so the first
time you start lcode in a repository that has any, it asks:

```text
This repository brings its own commands, skills or agents:
  commands: /review
  skills: commit-message
They can steer the model and include scripts it may run (commands still ask first, unless you
allow them).
  Use them? [y/N]
```

lcode asks again whenever any of their files change. Until you say yes, only your own commands and
skills are used. `lcode -p` never asks and doesn't use them.

## Examples

The lcode repository has a ready-made command and skill in
[`examples/`](https://github.com/nasser1941/lcode/tree/main/examples): `/review` and the
`commit-message` skill above. Copy them into `.lcode/` of a repository, or into
`~/.config/lcode/` to have them everywhere.
