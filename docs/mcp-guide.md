# MCP server guide

What each of lcode's ready-made MCP servers does, how to add it, and what to ask. How MCP works in lcode (sign-in,
settings, approvals, context) is on the [MCP servers](mcp.md) page.

## The short version

```bash
lcode mcp catalog          # the ready-made servers, and what's missing on your machine
lcode mcp add github       # add one: lcode asks for what it needs and tests the connection
lcode mcp list             # your servers and whether they work
lcode mcp tools github     # the tools a server offers
```

Then start `lcode` and ask in plain words, naming the service. lcode shows each call and waits for your answer:
++y++ for once, ++a++ for always in this session, ++n++ to refuse.

| To do this | Command |
|---|---|
| Turn a server off, and on again | `lcode mcp disable NAME`, `lcode mcp enable NAME` |
| Sign in again, or forget the sign-in | `lcode mcp login NAME`, `lcode mcp logout NAME` |
| Remove a server | `lcode mcp remove NAME` |
| See the servers in a session | `/mcp`, `/mcp tools NAME` |
| Start without any MCP server | `lcode --no-mcp` |

Settings are in `~/.config/lcode/mcp.json`. Add only the servers you use: each one takes context.
[Offering fewer tools](mcp.md#context) helps small models most.

## All servers

| Server | What it's for | Can it change things? | You need |
|---|---|---|---|
| [`github`](#github) | Repositories, issues, pull requests, Actions | yes | a GitHub token |
| [`atlassian`](#atlassian) | Jira, Confluence, Bitbucket, Compass | yes | browser sign-in |
| [`linear`](#linear) | Issues, projects, comments | yes | browser sign-in |
| [`notion`](#notion) | Pages and databases | yes | browser sign-in |
| [`google-drive`](#google-drive) | Files in Google Drive | creates files | your own Google OAuth client |
| [`sentry`](#sentry) | Errors, traces and releases | yes | browser sign-in |
| [`grafana`](#grafana) | Dashboards, metrics, logs, alerts | read-only to start | `uvx`, a service account token |
| [`aws`](#aws) | AWS CLI commands | read-only to start | `uvx`, AWS credentials |
| [`aws-mcp`](#aws-mcp) | AWS API calls, docs and skills | yes, within your IAM permissions | `uvx`, AWS credentials |
| [`aws-knowledge`](#aws-knowledge) | AWS documentation | no | nothing |
| [`gcp`](#gcp) | `gcloud` commands | yes | `npx`, `gcloud` signed in |
| [`kubernetes`](#kubernetes) | Pods, logs, events | read-only to start | `npx`, a kubeconfig |
| [`postgres`](#postgres) | Schemas, queries, performance | read-only to start | `uvx`, a connection URL |
| [`metabase`](#metabase) | Questions about your data | yes | Metabase with MCP on |
| [`context7`](#context7) | Documentation of libraries | no | nothing |
| [`playwright`](#playwright) | A real browser | yes, on the sites it opens | `npx` |
| [`encord`](#encord) | Labeling projects | read-only to start | `uvx`, an SSH key |
| [`valohai`](#valohai) | ML executions and pipelines | read-only to start | `uvx`, an API token |
| [`rea`](#rea) | Reverse engineering of apps | writes extracted files | `npx`, Ghidra for native programs |

Whatever a server can do, your account's permissions limit it, and lcode asks before every call unless you put a
tool in the server's `allow` list or use `yolo` mode.

`npx` comes with [Node.js](https://nodejs.org) and `uvx` with [uv](https://docs.astral.sh/uv/getting-started/installation/).

## Code and projects

### github

```bash
lcode mcp add github
```

Uses the GitHub CLI's token if you have `gh`, or one [you create](mcp.md#github). Four toolsets are on:

- **Repositories:** read files, list branches, commits, tags and releases, search code and commits; also create and
  change files, branches and repositories, and delete files or repositories.
- **Issues:** search, read, create and change issues and comments.
- **Pull requests:** search, read, create, review, update and merge.
- **Actions:** list runs, read job logs, start, rerun or cancel workflows.

> *"Read the logs of the last failed Actions run in OWNER/REPO and find the cause in this code."*

### atlassian

```bash
lcode mcp add atlassian
```

Signs you in through the browser. The server covers Jira, Confluence, Bitbucket Cloud and Compass, and more of
Atlassian's products. It shows its most-used tools and lets the model look up the rest.

- An organization's admin decides which groups of tools (read, write, search) are allowed. Deleting and managing in
  Jira is off unless an admin turns it on.
- Bitbucket works only if the workspace is linked to an Atlassian organization.
- Jira Service Management accepts API tokens only, so it isn't available after the browser sign-in. See
  [Signing in](mcp.md#signing-in) for adding the server with a token.

> *"Summarize the open bugs assigned to me in Jira and find the code for the top one."*

### linear

```bash
lcode mcp add linear
```

Signs you in through the browser. Finds, creates and updates issues, projects and comments. For read-only access,
change the address in `mcp.json` to `https://mcp.linear.app/mcp/readonly`.

> *"List my Linear issues in the current cycle and say which ones touch this repository."*

### notion

```bash
lcode mcp add notion
```

Signs you in through the browser. Searches and reads Notion, drafts and updates pages, and manages databases,
views, comments and files.

> *"Find our API style guide in Notion and check this module against it."*

### google-drive

```bash
lcode mcp add google-drive
```

Searches files, reads their content and metadata, downloads them and creates new ones. It needs an OAuth client from
your own Google Cloud project ([steps](mcp.md#google-drive)).

> *"Find the latest requirements document in my Drive and list what this project doesn't implement yet."*

## Operations and cloud

### sentry

```bash
lcode mcp add sentry
```

Signs you in through the browser. Looks up issues, errors, traces and logs, and can have Sentry's Seer analyze an
issue. Some tools create or change projects, alert rules and monitors, so read each call before you allow it.

> *"Fix this Sentry issue: PASTE-THE-ISSUE-URL"*

### grafana

```bash
lcode mcp add grafana
```

Asks for Grafana's address and a service account token (**Administration > Users and access > Service accounts**).
Covers dashboards, Prometheus and Loki queries, other data sources, alert rules, incidents and on-call schedules.
It starts read-only: remove `--disable-write` from its `args` in `mcp.json` to let it change
dashboards and alert rules. It has about 80 tools, so list the ones you use under `tools` in `mcp.json`.

> *"Query Loki for errors of the checkout service in the last hour and find where this code logs them."*

### aws

```bash
lcode mcp add aws
```

Uses your local AWS credentials.

- `call_aws` runs AWS CLI commands.
- `suggest_aws_commands` suggests commands for a task described in words.

It starts read-only: set `READ_OPERATIONS_ONLY` to `"false"` in `mcp.json` to allow changes. AWS says its newer
server, [`aws-mcp`](#aws-mcp), supersedes this one, which still works but shows a notice. Add one of the two, not both.

> *"Which Lambda functions in eu-central-1 still use Python 3.10?"*

### aws-mcp

```bash
lcode mcp add aws-mcp
```

The [AWS MCP Server](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/mcp-server.html), which AWS runs. A small
local proxy (`mcp-proxy-for-aws-cli`, through `uvx`) signs the requests with your AWS credentials.

| Tool | |
|---|---|
| `aws___run_script` | Runs Python with AWS API access in AWS's sandbox: list resources, check them, change them |
| `aws___get_tasks` | The status of a long-running script |
| `aws___get_presigned_url` | Upload and download links for S3 |
| `aws___search_documentation`, `aws___read_documentation`, `aws___retrieve_skill` | AWS documentation and AWS's step-by-step skills |
| `aws___list_regions`, `aws___get_regional_availability` | Regions, and what's available in each |

It **can change your resources**: unlike `aws`, it has no read-only setting that keeps API access. Your IAM permissions
decide what it can do, so use a profile with only the access you want, for example AWS's `ReadOnlyAccess` policy, and
read each script before you allow it. `--read-only` in its `args` in `mcp.json` hides the tools that can write, which
probably leaves only the documentation tools. Add either `aws` or `aws-mcp`, not both, and leave out `aws-knowledge`
too, since its tools are included: AWS says overlapping tools confuse the model.

> *"List my S3 buckets that aren't encrypted with KMS."*

### aws-knowledge

```bash
lcode mcp add aws-knowledge
```

Needs no account. Five tools, about 2,600 tokens: search and read AWS documentation, list regions, check what's
available in a region, and fetch AWS's own skills (step-by-step guides).

> *"Look up the current limits of SQS FIFO queues."*

### gcp

```bash
lcode mcp add gcp
```

One tool, `run_gcloud_command`, which runs `gcloud` with your own sign-in; the server refuses some commands. It isn't
read-only, so read each command before you allow it.

> *"List my Cloud Run services and their last deployment."*

### kubernetes

```bash
lcode mcp add kubernetes
```

Uses your kubeconfig and its current context. Lists and reads pods, logs, events, nodes, namespaces and other
resources, and resource use. It starts read-only: remove `--read-only` in `mcp.json` to allow changes.

> *"Why does the api pod in the staging namespace keep restarting?"*

## Data

### postgres

```bash
lcode mcp add postgres
```

Asks for a connection URL. Read-only to start (restricted mode).

| Tool | |
|---|---|
| `list_schemas`, `list_objects`, `get_object_details` | What's in the database |
| `execute_sql` | Run a query |
| `explain_query` | A query's execution plan, also with hypothetical indexes |
| `get_top_queries` | The slowest queries (needs `pg_stat_statements`) |
| `analyze_workload_indexes`, `analyze_query_indexes` | Index recommendations |
| `analyze_db_health` | Health checks: caches, connections, indexes and more |

> *"Explain why this query in reports.py is slow and suggest an index."*

### metabase

```bash
lcode mcp add metabase
```

Asks for your Metabase address, then signs you in through the browser. Searches, browses and queries your data through
Metabase's semantic layer, and creates content, within your Metabase permissions. An admin must have the MCP server on
([details](mcp.md#metabase)). Inline charts aren't shown in a terminal.

> *"Using Metabase, what were the ten best-selling products last month?"*

## Writing and testing code

### context7

```bash
lcode mcp add context7
```

Needs no account; a free API key raises the rate limits. Two tools, about 1,700 tokens: `resolve-library-id` finds a
library and `query-docs` fetches its current documentation and examples.

> *"Use context7 to look up how Pydantic 2 validators work, then update this model."*

### playwright

```bash
lcode mcp add playwright
```

A headless browser: open pages, click, type, fill forms, read the page, take screenshots, and look at the console and
network. The first use may download the browser. Pages it opens are untrusted content.

> *"Open http://localhost:3000, sign up with test data and tell me which errors appear in the console."*

## Machine learning

### encord

```bash
lcode mcp add encord
```

Ships with lcode and signs in with an SSH key you registered in Encord. Lists projects, datasets and tasks, shows
ontologies, workflow progress and the labels on a data unit. Assigning tasks and setting priorities needs
`--allow-writes` in the server's `args` ([tools](mcp.md#encord)).

> *"How many tasks are left in review in the project Street Scenes?"*

### valohai

```bash
lcode mcp add valohai
```

Ships with lcode and needs an API token. Lists projects, executions and pipelines, reads logs, metrics and outputs, and
compares executions. Starting and stopping executions needs `--allow-writes` ([tools](mcp.md#valohai)).

> *"Compare the last two training executions and explain the drop in accuracy."*

## Reverse engineering

### rea

```bash
lcode mcp add rea
```

Only for software you own or are authorized to examine. Ten tools, about 4,700 tokens.

| Target | Tools the model uses | You need |
|---|---|---|
| Electron apps (`.asar`) and archives | `open_binary`, `extract_artifact`, then lcode reads the files | a recent Node.js |
| Native programs | `open_binary`, `binary_overview`, `list_procedures`, `search_strings`, `trace_feature`, `xrefs`, `procedure_callers`, `procedure_pseudo_code` | also Ghidra 12.2 and a full JDK 25 |

`close_binary` lets go of the target. Press Enter at both questions of `lcode mcp add rea` if you only need Electron
apps. With a local model, Electron apps worked in our tests, while native programs often didn't: see the
[measured results](mcp.md#rea), and check the answers.

> *"Use REA to extract /path/to/app.asar, then explain how search works in this app. Name the file and function."*

## Any other server

```bash
lcode mcp add NAME --url https://mcp.example.com/mcp     # a remote server
lcode mcp add NAME -- npx -y some-mcp-server             # a local one
```

See [Adding your own servers](mcp.md#adding-your-own-servers). Anyone can write an MCP server, so add only those you
trust: a local one runs on your machine with your permissions.

## If something doesn't work

`lcode mcp list` says what's wrong. `needs sign-in` means `lcode mcp login NAME`; see also
[Troubleshooting](mcp.md#troubleshooting).

The tools described here come from each project's own documentation in November 2026. lcode's maintainers tested
`aws-knowledge`, `context7` and `rea` for this page; the others can change on their side, so
`lcode mcp tools NAME` shows what a server offers today.
