# MCP servers

The [Model Context Protocol](https://modelcontextprotocol.io) (MCP) lets lcode use tools from other
systems: your issue tracker, GitHub, cloud accounts, databases, dashboards, a browser. Each MCP server
adds a set of tools that the model can call, just like reading files or running commands, and every
call is shown to you for approval first.

```bash
lcode mcp catalog          # ready-made servers
lcode mcp add atlassian    # add one (signs you in through the browser)
lcode mcp list             # check that your servers work
```

Then start `lcode` as usual and ask, for example, *"Summarize the open bugs assigned to me in Jira
and find the code for the top one."*

## Ready-made servers

`lcode mcp add <name>` sets these up, asks for anything they need and tests the connection:

| Name | What it gives the model | You need |
|---|---|---|
| `atlassian` | Jira, Confluence, Jira Service Management, Compass, Bitbucket | an Atlassian account (browser sign-in) |
| `aws` | AWS CLI commands on your account, read-only unless you change it | `uvx` and AWS credentials |
| `aws-knowledge` | Current AWS documentation, code samples, regional availability | nothing |
| `google-drive` | Search, read and create files in Google Drive | your own Google OAuth client ([below](#google-drive)) |
| `grafana` | Dashboards, Prometheus and Loki queries, alerts, incidents | `uvx`, a Grafana service account token |
| `gcp` | `gcloud` commands on your Google Cloud projects | `npx` and the `gcloud` CLI, signed in |
| `github` | Repositories, issues, pull requests, Actions | a GitHub token (or the GitHub CLI) |
| `playwright` | A real (headless) browser: open pages, click, fill forms | `npx` |
| `comfyui` | Generate and edit images with local models (FLUX, SDXL, SD 1.5, Qwen-Image) | `uvx` and [ComfyUI](#images-with-comfyui) running locally |
| `xai` | Generate and edit images, and generate videos and speech, with xAI's Grok Imagine (paid, in the cloud) | `uvx`, an xAI API key ([below](#images-videos-and-speech-with-xai)) |
| `context7` | Up-to-date docs and examples for thousands of libraries | nothing (an API key is optional) |
| `sentry` | Errors, issues, traces and releases | a Sentry account (browser sign-in) |
| `postgres` | Schemas, read-only queries, query performance | `uvx`, a connection URL |
| `kubernetes` | Pods, deployments, logs and events, read-only | `npx` and a kubeconfig |
| `linear` | Issues, projects and cycles | a Linear account (browser sign-in) |
| `notion` | Pages and databases | a Notion account (browser sign-in) |
| `metabase` | Explore your data and ask questions through Metabase's semantic layer | Metabase 60 or later with MCP on ([below](#metabase)) |
| `encord` | Projects, datasets, ontologies, labeling progress, tasks and labels, read-only | `uvx`, an SSH key registered in Encord ([below](#encord)) |
| `valohai` | ML executions with their logs, metrics and outputs, and pipelines, read-only | `uvx`, a Valohai API token ([below](#valohai)) |

`npx` comes with [Node.js](https://nodejs.org) (18 or newer) and `uvx` with
[uv](https://docs.astral.sh/uv/getting-started/installation/). `lcode mcp catalog` shows what's missing
on your machine.

Servers that can change things start read-only where the server supports it (`aws`, `postgres`,
`kubernetes`, `encord`, `valohai`); the setup note tells you which setting to change to allow writes.

### Signing in

Atlassian, Sentry, Linear and Notion use OAuth: lcode opens your browser, you sign in and approve
access, and the browser hands a code back to lcode on `localhost`. lcode stores the tokens in
`~/.local/state/lcode/mcp-auth/` (readable only by you) and refreshes them automatically.

```bash
lcode mcp login atlassian     # sign in again, e.g. after revoking access
lcode mcp logout atlassian    # forget the tokens
```

In a session, `/mcp login <name>` does the same. On a machine without a browser, lcode prints the
sign-in URL; open it on any computer that can reach the machine's `localhost` (for example through an
SSH tunnel), or sign in on your own machine and copy `~/.local/state/lcode/mcp-auth/`.

Atlassian organizations can also allow API tokens instead; see
[Atlassian's guide](https://developer.atlassian.com/cloud/rovo-mcp/guides/configuring-authentication-via-api-token/)
and add the server [by hand](#adding-your-own-servers) with an `Authorization` header.

### GitHub

`lcode mcp add github` offers the GitHub CLI's token if you use `gh`; otherwise create a
[fine-grained token](https://github.com/settings/personal-access-tokens/new) with access to the
repositories you want. Only the `repos`, `issues`, `pull_requests` and `actions` toolsets are on, to
keep the tool list short; change `X-MCP-Toolsets` in `mcp.json` for
[others](https://github.com/github/github-mcp-server#available-toolsets).

### Google Drive

Google doesn't let apps register themselves, so the Drive server needs an OAuth client from your own
Google Cloud project:

1. In a Google Cloud project, enable the **Google Drive API** and the **Google Drive MCP API**.
2. Configure the OAuth consent screen (while it's in testing, add yourself as a test user).
3. Create an **OAuth client ID** of type **Desktop app**, and copy its ID and secret.
4. Run `lcode mcp add google-drive` and paste them; lcode then signs you in through the browser.

See [Google's guide](https://developers.google.com/workspace/guides/configure-mcp-servers) for
details.

### Images with ComfyUI

[ComfyUI](https://github.com/comfyanonymous/ComfyUI) runs image generation and editing models
locally; its official MCP server lets lcode use it, for app icons, illustrations, mockups or
placeholder art.

```bash
uv tool install comfy-cli && comfy install                 # ComfyUI, once
comfy launch --background -- --disable-smart-memory          # start it (http://127.0.0.1:8188)
lcode mcp add comfyui
```

Then add models in ComfyUI (its model manager, or ask lcode: *"download FLUX.2 Klein 4B in
ComfyUI"*), and ask for images: *"make a 512×512 app icon of a paper plane and save it in
assets/"*. To edit an image, ask lcode to upload it and run an editing workflow (Kontext or
Qwen-Image-Edit).

| Model | Good for | On a 12 GB GPU |
|---|---|---|
| FLUX.2 Klein 4B | Generation and editing, fast | Fits (about 8 GB) |
| SDXL and its community models | Generation, inpainting | Fits |
| Stable Diffusion 1.5 and its community models | Light generation, inpainting | Fits easily |
| FLUX.1 Dev and finetunes | High-quality generation | Needs an fp8 or GGUF version; slower |
| FLUX.1 Kontext Dev | Editing an image from instructions | Needs an fp8 or GGUF version; slower |
| Qwen-Image / Qwen-Image-Edit | Generation and editing, good text in images | Heavy: a GGUF version and RAM offloading |

Only ComfyUI's local tools are turned on; Comfy Cloud's partner tools are left out, so prompts and
images stay on your machine.

**Sharing one GPU.** A GPU rarely holds a coding model and an image model at the same time, so the
two take turns: before ComfyUI generates, lcode unloads its own model (the `free_gpu` setting), and
ComfyUI started with `--disable-smart-memory` gives the GPU back after each image. lcode's model
then reloads for the next step, which adds a few seconds to half a minute per image request. With
enough GPU memory for both (or on a Mac with plenty of memory), remove `free_gpu` from the server's
settings in `mcp.json`. lcode can also look at the results ([Images](usage.md#images)).

Measured on an RTX 4080 Laptop GPU (12 GB) with qwen3.6-35b at 64K context and FLUX.2 Klein Base 4B
(fp8, 512×512, 20 steps):

| | |
|---|---|
| Generating while lcode's model is loaded | Fails: ComfyUI runs out of GPU memory |
| Freeing the GPU (lcode unloads its model) | 0.2 s |
| Generating one image | about 12 s |
| Reloading lcode's model afterwards | about 20 s |

The first time, the model has to find the right template and fill in its settings (model file names,
size, prompt), which can take several minutes of trial and error. Once an image comes out right, ask
lcode to save that workflow in your project (for example `assets/icon.workflow.json`) and reuse it:
later images are a single `run_workflow` call.

### Images, videos and speech with xAI

xAI's [Grok Imagine](https://docs.x.ai/developers/model-capabilities/imagine) and
[text to speech](https://docs.x.ai/developers/models/text-to-speech) APIs make images, videos and
speech in the cloud: no GPU needed, but each call is billed to your xAI account. lcode ships an MCP
server for them, `lcode-mcp-xai`.

```bash
lcode mcp add xai             # asks for your xAI API key (create one at https://console.x.ai)
```

The key is typed hidden and kept in `~/.config/lcode/mcp.json`, readable only by you. If
`XAI_API_KEY` is already set in your environment, lcode uses that and doesn't store the key.

| Tool | | Costs |
|---|---|---|
| `generate_image` | Images from a description: 1–10 at a time, aspect ratio, 1k or 2k | $0.02–0.05 an image |
| `edit_image` | Change a local image as described | the same |
| `generate_video` | A 1–15 second MP4 from a description, optionally starting from a local image, up to 1080p. Waits for it (a minute or more) | $0.02–0.08 a second |
| `video_status` | Fetch a video that wasn't ready within 10 minutes | |
| `text_to_speech` | Speech in one of 26 voices and 20 languages, as MP3 or WAV | per character |

Files are saved in `generated/` in the project, or where you ask (*"generate an app icon and save
it as assets/icon.png"*). xAI has no music generation API.

Your prompts, and the images you edit or animate, go to xAI. lcode asks before each call, since each
one costs money. To stop asking for some tools, list them under `"allow"` for the server in
`mcp.json`, for example `"allow": ["generate_image", "text_to_speech"]`. xAI applies its own usage
policies to what it generates; when it refuses a prompt, lcode passes its message to the model.

### Metabase

Metabase has its own MCP server inside every instance (Metabase 60 and later), at
`/api/metabase-mcp`. An admin switches it on under **Admin > AI > MCP**. Then:

```bash
lcode mcp add metabase        # asks for your Metabase address, then signs in in the browser
```

You sign in with your Metabase account, and the model sees exactly the data your Metabase
permissions allow. See [Metabase's MCP docs](https://www.metabase.com/docs/latest/ai/mcp).

### Encord

Encord has no MCP server of its own, so lcode ships one: `lcode-mcp-encord`, built on the Encord SDK
and run with `uvx --isolated --from "lcode-cli[encord]"`. It signs in with an SSH key that you register in
Encord under **Settings > Public keys**; `lcode mcp add encord` asks for the key file's path.

| Tool | |
|---|---|
| `list_projects`, `list_datasets` | What you can see |
| `get_project`, `get_ontology` | A project's datasets, ontology classes and workflow stages |
| `workflow_progress` | How many tasks are in each workflow stage, by status |
| `list_tasks` | Data units with their stage, status and last edit, filtered by stage or title |
| `get_labels` | The labels on one data unit: counts per class and the label JSON |
| `assign_task`, `set_priority` | Only with `--allow-writes` in the server's `args` in `mcp.json` |

### Valohai

Valohai has no MCP server of its own either, so lcode ships `lcode-mcp-valohai`, which talks to
Valohai's REST API. It needs an API token (**My Profile > Authentication > Manage tokens**), and the
address of a self-hosted installation if you use one; `lcode mcp add valohai` asks for both.

| Tool | |
|---|---|
| `list_projects`, `list_pipelines` | Projects and their pipelines |
| `list_executions`, `get_execution` | Executions with their status, parameters, metrics, outputs and errors |
| `get_execution_logs` | The last lines of an execution's log, optionally only stderr |
| `list_outputs` | The files an execution produced, with `datum://` URIs to use as inputs |
| `compare_executions` | Metrics and differing parameters of several executions side by side |
| `start_execution`, `stop_execution` | Only with `--allow-writes` in the server's `args` in `mcp.json` |

Both servers are ordinary MCP servers: other MCP clients can use them too, for example
`uvx --isolated --from lcode-cli lcode-mcp-valohai` with `VALOHAI_TOKEN` set. Keep `--isolated`: without it,
`uvx` reuses an lcode you installed with `uv tool install`, which may be older or lack the Encord SDK.

## Adding your own servers

Any MCP server works. For a remote server, give its URL; for a local one, the command that starts it:

```bash
lcode mcp add tracker --url https://mcp.example.com/mcp
lcode mcp add tracker --url https://mcp.example.com/mcp --header "Authorization: Bearer \${TRACKER_TOKEN}"
lcode mcp add notes -- npx -y @modelcontextprotocol/server-filesystem /home/me/notes
lcode mcp add db --env DATABASE_URL=postgres://localhost/app -- uvx some-postgres-mcp
```

Servers are saved in `~/.config/lcode/mcp.json` in the `mcpServers` format that most MCP servers
document, so you can also paste a server's example config into that file:

```json
{
  "mcpServers": {
    "tracker": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${TRACKER_TOKEN}" }
    },
    "notes": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"],
      "tools": ["read_file", "search_files"],
      "allow": ["read_file"]
    }
  }
}
```

| Key | Meaning |
|---|---|
| `command`, `args`, `env` | A local server, started by lcode and spoken to over stdin/stdout |
| `type: "http"`, `url`, `headers` | A remote server (Streamable HTTP). Without an `Authorization` header, lcode signs in with OAuth when the server asks for it |
| `tools` | Only offer these tools to the model (fewer tools save context and help smaller models) |
| `allow` | Tools that run without asking; `["*"]` for all of the server's tools |
| `timeout` | Seconds a tool call may take (default 300) |
| `disabled` | `true` to turn the server off (`lcode mcp disable <name>`) |
| `free_gpu` | Tools that need the GPU to themselves (`true` for all): lcode unloads its model first and reloads it afterwards |
| `oauth` | For servers that need a pre-registered OAuth client: `client_id`, `client_secret`, `scopes`, `authorize_params` |

Values can use environment variables: `${NAME}`, or `${NAME:-default}`. Keep tokens in environment
variables if you prefer not to store them in the file; `mcp.json` is created readable only by you
either way.

Servers that only offer the old HTTP+SSE transport (URLs ending in `/sse`) aren't supported. Most of
them also have a Streamable HTTP endpoint (often `/mcp`); otherwise bridge one with
`npx -y mcp-remote <url>` as a local server.

### Project servers

A repository can share servers with everyone who works on it in a `.mcp.json` at its root, in the same
format. Because those servers run programs or connect to services on your behalf, lcode asks before
using them the first time, and again whenever the file changes.

## In a session

The banner lists your servers. They start in the background, and lcode waits for them (at most a
couple of minutes, the first time `npx` or `uvx` downloads a server) before your first request.

```text
❯ /mcp
MCP servers
┏━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Server        ┃ Status ┃ Tools ┃ Details                                        ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ aws-knowledge │ ready  │     5 │ https://knowledge-mcp.global.api.aws           │
│ everything    │ ready  │    13 │ npx -y @modelcontextprotocol/server-everything │
│ context7      │ ready  │     2 │ https://mcp.context7.com/mcp                   │
└───────────────┴────────┴───────┴────────────────────────────────────────────────┘
           Tool definitions: ~6.2K tokens · all sent with every request
```

A server that can't start shows `failed` with the reason (for example, a missing environment
variable), and one that needs you to sign in shows `needs sign-in`; the others work normally.

| Command | |
|---|---|
| `/mcp` | Servers, their status and the context their tools take |
| `/mcp tools <name>` | A server's tools |
| `/mcp login <name>` | Sign in (or again) and reconnect |
| `/mcp restart <name>` | Restart a server, e.g. after changing its settings |

Start a session without MCP servers with `lcode --no-mcp`.

### Approvals

Every MCP tool call shows the server, the tool and its arguments, and waits for your answer, in the
`ask` and `auto-edit` modes alike: answer ++y++ (once), ++a++ (always, for this tool, until you quit)
or ++n++ with an optional reason. Only `yolo` mode, or the server's `allow` list, skips the question.

### Context

Every tool definition is sent to the model with every request. A few servers can add tens of
thousands of tokens, which leaves less room for your code and makes smaller models pick the wrong
tool. So when the definitions would take more than 15% of the context window, lcode doesn't send
them: the model sees the tool names, looks up the ones it needs with `mcp_find_tools` and calls them
with `mcp_call`. `/mcp` and `/context` show which way is used.

| `mcp_tools` setting | |
|---|---|
| `auto` (default) | Send the definitions if they fit in 15% of the context, otherwise on demand |
| `direct` | Always send them |
| `search` | Always on demand |

```bash
lcode config set mcp_tools search
```

Limiting a server's `tools` in `mcp.json` is the best fix for a crowded tool list. Some servers are
large: Grafana offers about 80 tools and Playwright about 25, while most have a handful.

## Troubleshooting

- `lcode mcp list` connects to every server and shows what's wrong.
- A local server's error output is in `~/.local/state/lcode/mcp-logs/<name>.log`.
- **A `uvx` server fails with a Python error:** some servers don't work on the newest Python yet;
  the ready-made ones pin Python 3.12 with `--python 3.12`, which you can add to your own. A
  `No module named 'mcp.server.fastmcp'` error means the server needs version 1 of the MCP Python
  SDK: add `"--with", "mcp<2"` before the package name in its `args`.
- **"needs sign-in":** run `lcode mcp login <name>`. If the sign-in page says the redirect URL isn't
  allowed, the service needs a pre-registered client (see [Google Drive](#google-drive)).
- **Very slow first start:** `npx -y` and `uvx` download the server the first time.

## Protocol support

lcode implements MCP tools over stdio and Streamable HTTP, for both the current protocol
(`2026-07-28`, stateless) and the earlier `initialize`-based versions (`2025-11-25` and before), and
the MCP authorization spec (OAuth 2.1 with PKCE, protected-resource discovery, dynamic client
registration and refresh tokens). MCP resources, prompts, sampling and elicitation aren't supported
yet.
