"""The tools' JSON schemas, as the model sees them: the core tools, and those offered when their feature is on."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


SCHEMAS = [
    _fn(
        "read_file",
        "Read a text file. Returns lines prefixed with line numbers (cat -n style). Reads up to `limit` lines "
        "starting at `offset` (1-based). Read a file before editing it.",
        {
            "path": {"type": "string", "description": "File path, relative to the working directory or absolute"},
            "offset": {"type": "integer", "description": "1-based line to start from (default 1)"},
            "limit": {"type": "integer", "description": "Maximum lines to read (default 2000)"},
        },
        ["path"],
    ),
    _fn(
        "write_file",
        "Create a new file or completely overwrite an existing one with `content`. Parent directories are "
        "created. For small changes to existing files prefer edit_file.",
        {"path": {"type": "string"}, "content": {"type": "string", "description": "Full file content"}},
        ["path", "content"],
    ),
    _fn(
        "edit_file",
        "Replace `old_string` with `new_string` in a file. `old_string` must match the file exactly (including "
        "indentation) and be unique unless replace_all is true: include enough surrounding lines to make it "
        "unique. Do NOT include the line-number prefixes from read_file output.",
        {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
        },
        ["path", "old_string", "new_string"],
    ),
    _fn(
        "apply_patch",
        "Apply a unified diff to one or more files, as `git diff` writes it: `--- a/path` and `+++ b/path` "
        "headers, then `@@` hunks of context lines (starting with a space), removed lines (-) and added lines (+). "
        "Hunks are found by their text, so the @@ line numbers may be off; include 2-3 context lines around each "
        "change. Use `--- /dev/null` for a new file. For a single change, edit_file is simpler.",
        {"patch": {"type": "string", "description": "The unified diff"}},
        ["patch"],
    ),
    _fn(
        "list_dir",
        "Show a directory tree (skips .git, node_modules, virtualenvs and caches).",
        {
            "path": {"type": "string", "description": "Directory (default: working directory)"},
            "depth": {"type": "integer", "description": "Maximum depth (default 2)"},
        },
        [],
    ),
    _fn(
        "glob",
        "Find files by glob pattern, e.g. '**/*.py' or 'src/**/test_*.ts'. Newest first.",
        {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Base directory"}},
        ["pattern"],
    ),
    _fn(
        "grep",
        "Search file contents with a regular expression. Returns path:line:text matches.",
        {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "File or directory to search (default: working directory)"},
            "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'"},
            "ignore_case": {"type": "boolean"},
            "context": {"type": "integer", "description": "Lines of context around each match"},
        },
        ["pattern"],
    ),
    _fn(
        "bash",
        "Run a shell command with bash in the working directory and return its output and exit code. The "
        "working directory persists between calls (cd works). Use it to run scripts and tests, use git, install "
        "packages, etc. Avoid interactive commands. For a process that keeps running (a dev server, a watcher), "
        "set background=true and give the plain command, without & or redirecting its output: lcode keeps it "
        "running and keeps its output, you get a job id back and can carry on. Background jobs stop when lcode "
        "exits, unless you also set keep=true.",
        {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 180)"},
            "background": {
                "type": "boolean",
                "description": "Keep it running in the background; read its output with bash_output and stop it "
                "with bash_stop (not with kill)",
            },
            "keep": {
                "type": "boolean",
                "description": "With background=true: leave it running after lcode exits. Only for a service the "
                "user wants left running when you're done (a server they asked you to set up), not for one you "
                "need only while you work",
            },
        },
        ["command"],
    ),
    _fn(
        "bash_output",
        "Read what a background command (bash with background=true) printed since you last read it, and whether "
        "it's still running.",
        {"id": {"type": "string", "description": "The job id bash returned"}},
        ["id"],
    ),
    _fn("bash_stop", "Stop a background command.", {"id": {"type": "string"}}, ["id"]),
    _fn(
        "todo_write",
        "Create or update your task list for multi-step work. Pass the full list every time. Keep exactly one "
        "item in_progress while working. In `notes`, keep what you must not forget: decisions and why, findings, "
        "open questions. The list and the notes stay with you even when old messages are removed.",
        {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    },
                    "required": ["content", "status"],
                },
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short notes, the full list each time (omit to keep the current ones)",
            },
        },
        ["todos"],
    ),
    _fn(
        "screenshot",
        "See a desktop program or web page as the user would, without a screen: it runs the program (or opens the "
        "page), saves a screenshot in .lcode/screenshots/ and returns what it shows, with the sizes of windows and "
        "widgets (Qt) or of the page, and the browser's console errors. Use it to check GUI and layout work instead "
        "of guessing. Python Qt programs run offscreen; other desktop programs on a virtual display (Linux); web pages "
        "in a headless browser. For a web app, give the command that starts its server and the url (lcode starts and "
        "stops it); if a server is already running, or for an .html file, give only the url. For something that "
        "only shows after a click or a key (a Python Qt program), give `actions`: they run first, then the picture.",
        {
            "command": {"type": "string", "description": "Command that starts the program, or the web server"},
            "url": {"type": "string", "description": "Page to open: a URL, localhost:PORT/path or an .html file"},
            "question": {
                "type": "string",
                "description": "What to check, e.g. 'Are all six panels inside the window?'",
            },
            "screen": {"type": "string", "description": "Screen (or browser window) size, WxH (default 1920x1080)"},
            "wait": {"type": "integer", "description": "Seconds to let it start and draw first (default 3)"},
            "full_page": {"type": "boolean", "description": "Web pages: the whole page, not just the window"},
            "actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Python Qt programs: steps to take before the picture, as a user would: "
                '"click X,Y", "double_click X,Y", "right_click X,Y" (a point in the window, as the sizes show), '
                'or with the widget\'s text: "double_click Camera 1"; "key Escape", "key ctrl+s"; "type some text"; '
                '"wait 2". The result says what each step hit.',
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "qt", "xvfb"],
                "description": "Desktop programs: auto (default), qt (Python Qt, offscreen), xvfb (virtual display)",
            },
        },
        [],
    ),
]


WEB_SEARCH_SCHEMA = _fn(
    "web_search",
    "Search the web for up-to-date information: latest versions and releases, documentation, API changes, "
    "error messages, security advisories. Returns titles, URLs and snippets; read promising results with "
    "web_fetch. Look in the repository first for questions about this codebase.",
    {
        "query": {"type": "string", "description": "Search query"},
        "max_results": {"type": "integer", "description": "Number of results, 1-10 (default 5)"},
    },
    ["query"],
)


WEB_FETCH_SCHEMA = _fn(
    "web_fetch",
    "Download a web page or text file by URL and return its main content as text (HTML is converted).",
    {
        "url": {"type": "string", "description": "http(s) URL"},
        "max_chars": {"type": "integer", "description": "Maximum characters to return (default 20000)"},
    },
    ["url"],
)


VIEW_IMAGE_SCHEMA = _fn(
    "view_image",
    "Look at an image file (screenshot, diagram, photo, mockup): returns a detailed description with all visible "
    "text transcribed. Ask a specific question to focus it.",
    {
        "path": {"type": "string", "description": "Image file: png, jpg, webp or gif"},
        "question": {"type": "string", "description": "What you need to know from the image (optional)"},
    },
    ["path"],
)


MEMORY_SCHEMA = _fn(
    "memory",
    "Your memory across sessions. save: keep a fact that a future session needs and that isn't in the code, git "
    "history or AGENTS.md (one fact per note; saving under an existing name updates that note). delete: remove a "
    "note that turned out wrong. read: a note's details, or the list of all notes when no name is given.",
    {
        "action": {"type": "string", "enum": ["save", "read", "delete"]},
        "name": {"type": "string", "description": "Short kebab-case name, e.g. use-pnpm"},
        "type": {
            "type": "string",
            "enum": ["feedback", "project", "reference", "user"],
            "description": "save: feedback = the user's corrections and preferences; project = decisions, "
            "constraints, ongoing work; reference = where things live outside the repository; user = who the "
            "user is",
        },
        "description": {"type": "string", "description": "save: the fact, in one line"},
        "details": {"type": "string", "description": "save (optional): why, and how to apply it"},
        "scope": {
            "type": "string",
            "enum": ["project", "user"],
            "description": "save: project (default) = this repository only; user = every repository",
        },
    },
    ["action"],
)


REPO_MAP_SCHEMA = _fn(
    "repo_map",
    "A map of the repository: the important files with their classes and functions, signatures and line numbers, "
    "ranked by how much of the code uses them. Use it to find your way around before reading files.",
    {"path": {"type": "string", "description": "Only this folder (to see more of it)"}},
    [],
)


SEARCH_CODE_SCHEMA = _fn(
    "search_code",
    "Find code by what it does, not by its exact words: e.g. 'where are failed uploads retried'. Returns the "
    "closest matches with file:line. Use grep for exact names.",
    {
        "query": {"type": "string", "description": "What you're looking for, in words"},
        "path": {"type": "string", "description": "Only in this folder (optional)"},
    },
    ["query"],
)


LSP_SCHEMA = _fn(
    "lsp",
    "Ask the language server about code: where a symbol is defined, where it's used, its type or signature, or "
    "the symbols in a file (or in the whole workspace, by name). More precise than grep, and cheaper on context.",
    {
        "action": {"type": "string", "enum": ["definition", "references", "hover", "symbols"]},
        "path": {"type": "string", "description": "The file (for symbols: leave it out to search the workspace)"},
        "line": {"type": "integer", "description": "The line (1-based) where the symbol appears"},
        "symbol": {"type": "string", "description": "The symbol's name on that line"},
        "query": {"type": "string", "description": "symbols without a path: the name to search for"},
    },
    ["action"],
)


PRESENT_PLAN_SCHEMA = _fn(
    "present_plan",
    "Plan mode: show the user your plan and ask for approval. Once approved, plan mode ends and you carry it out.",
    {
        "title": {"type": "string", "description": "A short name for the plan, e.g. 'Add retries to webhooks'"},
        "plan": {
            "type": "string",
            "description": "Markdown: numbered steps, the files you'll change and how, the risks, and how you'll "
            "verify the result",
        },
    },
    ["title", "plan"],
)


LCODE_HELP_SCHEMA = _fn(
    "lcode_help",
    "Look things up in the manual of lcode, the program you run in: its slash commands, its modes (code, image and "
    "video), settings, models and options. When the user asks how to do something in lcode, use it and tell them "
    "exactly what to type; don't answer such questions from memory.",
    {"question": {"type": "string", "description": "What the user wants to do in lcode, in words"}},
    ["question"],
)


OPTIONAL_SCHEMAS = [
    LCODE_HELP_SCHEMA,
    WEB_SEARCH_SCHEMA,
    WEB_FETCH_SCHEMA,
    VIEW_IMAGE_SCHEMA,
    MEMORY_SCHEMA,
    PRESENT_PLAN_SCHEMA,
    LSP_SCHEMA,
    REPO_MAP_SCHEMA,
    SEARCH_CODE_SCHEMA,
]


TOOL_NAMES = {s["function"]["name"] for s in [*SCHEMAS, *OPTIONAL_SCHEMAS]} | {
    "agent",
    "skill",
}  # those depend on the session
