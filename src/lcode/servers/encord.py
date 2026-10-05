"""An MCP server for Encord: projects, datasets, ontologies, workflow progress, tasks and labels.

Encord has a Python SDK but no MCP server, so lcode ships this one, built on the SDK
(`pip install encord`; `uvx --from "lcode-cli[encord]" lcode-mcp-encord` brings it along). It signs in
with an SSH key registered in Encord (Settings > Public keys): ENCORD_SSH_KEY_FILE names the key
file, or ENCORD_SSH_KEY holds the key itself. It's read-only unless started with --allow-writes,
which adds assigning tasks and setting their priority.

Run: lcode-mcp-encord [--allow-writes]   (lcode mcp add encord sets it up)
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter

from lcode import __version__
from lcode.mcp.server import Server, ToolFailure

MAX_ROWS = 20_000  # label rows read for progress counts
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _title(value) -> str:
    return getattr(value, "title", None) or str(value or "")


def _status(row) -> str:
    status = getattr(row, "annotation_task_status", None) or getattr(row, "label_status", None)
    return str(getattr(status, "value", status) or "")


def _stage(row) -> str:
    return _title(getattr(row, "workflow_graph_node", None)) or "(no workflow stage)"


def build(client, allow_writes: bool = False) -> Server:
    """The server for an EncordUserClient (or anything with the same methods, in tests)."""
    server = Server(
        "encord",
        __version__,
        "Encord data and annotation platform: projects (annotation work on datasets, with an ontology and a "
        "workflow of stages such as Annotate and Review), datasets, tasks (one per data unit) and labels.",
        allow_writes,
    )
    project_arg = {"type": "string", "description": "Project title or project hash"}

    def find_project(name: str):
        name = name.strip()
        if UUID.match(name):
            return client.get_project(name)
        found = list(client.list_projects(title_eq=name)) or list(client.list_projects(title_like=f"%{name}%"))
        if not found:
            raise ToolFailure(f"no project {name!r}; list_projects shows the ones you can see")
        if len(found) > 1 and not any(p.title == name for p in found):
            raise ToolFailure(f"{len(found)} projects match {name!r}: {', '.join(p.title for p in found[:10])}")
        return next((p for p in found if p.title == name), found[0])

    def rows(p, **filters) -> list:
        return list(p.list_label_rows_v2(include_workflow_graph_node=True, **filters))[:MAX_ROWS]

    @server.tool(
        "list_projects",
        "List the Encord projects you can see.",
        {"title_contains": {"type": "string", "description": "Only projects whose title contains this"}},
    )
    def list_projects(title_contains: str = "") -> str:
        found = list(client.list_projects(title_like=f"%{title_contains}%" if title_contains else None))
        if not found:
            return "No projects."
        return "\n".join(
            f"{p.title}  hash={p.project_hash}  created {str(getattr(p, 'created_at', ''))[:10]}"
            + (f"\n    {p.description[:150]}" if getattr(p, "description", "") else "")
            for p in found
        )

    @server.tool(
        "list_datasets",
        "List the Encord datasets you can see.",
        {"title_contains": {"type": "string", "description": "Only datasets whose title contains this"}},
    )
    def list_datasets(title_contains: str = "") -> str:
        found = client.get_datasets(title_like=f"%{title_contains}%" if title_contains else None)
        if not found:
            return "No datasets."
        lines = []
        for entry in found:
            d = entry.get("dataset") if isinstance(entry, dict) else entry
            lines.append(f"{d.title}  hash={d.dataset_hash}  edited {str(getattr(d, 'last_edited_at', ''))[:10]}")
        return "\n".join(lines)

    @server.tool(
        "get_project",
        "A project's description, datasets, ontology (object and classification classes) and workflow stages.",
        {"project": project_arg},
        ["project"],
    )
    def get_project(project: str) -> str:
        p = find_project(project)
        parts = [f"{p.title}  hash={p.project_hash}"]
        if getattr(p, "description", ""):
            parts.append(p.description)
        datasets = [f"{d.title} ({d.dataset_hash})" for d in p.list_datasets()]
        parts.append(f"datasets: {', '.join(datasets) or 'none'}")
        parts.append(ontology_text(p))
        try:
            stages = [f"{s.title} ({type(s).__name__.removesuffix('Stage').lower()})" for s in p.workflow.stages]
            parts.append(f"workflow stages: {', '.join(stages)}")
        except Exception:  # projects without a workflow
            pass
        return "\n".join(parts)

    def ontology_text(p) -> str:
        structure = p.ontology_structure
        objects = [
            f"{o.name} ({getattr(getattr(o, 'shape', ''), 'value', getattr(o, 'shape', ''))})"
            for o in structure.objects
        ]
        classifications = []
        for c in structure.classifications:
            attributes = getattr(c, "attributes", []) or []
            classifications.append(attributes[0].name if attributes else str(getattr(c, "feature_node_hash", "?")))
        return (
            f"ontology objects: {', '.join(objects) or 'none'}\n"
            f"ontology classifications: {', '.join(classifications) or 'none'}"
        )

    @server.tool(
        "workflow_progress",
        "How many tasks are in each workflow stage, and their statuses: a project's labeling progress.",
        {"project": project_arg},
        ["project"],
    )
    def workflow_progress(project: str) -> str:
        p = find_project(project)
        found = rows(p)
        if not found:
            return "No data units in this project."
        stages = Counter(_stage(r) for r in found)
        statuses = Counter((_stage(r), _status(r)) for r in found)
        lines = [f"{len(found)} data units" + (" (stopped counting here)" if len(found) >= MAX_ROWS else "")]
        for stage, count in stages.most_common():
            detail = ", ".join(f"{s or 'no status'} {n}" for (st, s), n in statuses.items() if st == stage)
            lines.append(f"{stage}: {count}  ({detail})")
        return "\n".join(lines)

    @server.tool(
        "list_tasks",
        "List data units (tasks) of a project with their workflow stage, status and last edit.",
        {
            "project": project_arg,
            "stage": {"type": "string", "description": "Only tasks in this workflow stage, e.g. Review"},
            "title_contains": {"type": "string", "description": "Only data units whose title contains this"},
            "limit": {"type": "integer", "description": "How many (default 50, at most 500)"},
        },
        ["project"],
    )
    def list_tasks(project: str, stage: str = "", title_contains: str = "", limit: int = 50) -> str:
        p = find_project(project)
        filters = {}
        if stage:
            filters["workflow_graph_node_title_eq"] = stage
        if title_contains:
            filters["data_title_like"] = f"%{title_contains}%"
        found = rows(p, **filters)[: max(1, min(int(limit), 500))]
        if not found:
            return "No tasks match."
        return "\n".join(
            f"{r.data_title}  stage={_stage(r)}  status={_status(r) or '-'}  "
            f"edited {str(getattr(r, 'last_edited_at', '') or '')[:16]}  data_hash={r.data_hash}"
            for r in found
        )

    @server.tool(
        "get_labels",
        "The labels on one data unit: how many objects and classifications of each class, and the label JSON.",
        {
            "project": project_arg,
            "data_title": {"type": "string", "description": "The data unit's title (file name), from list_tasks"},
        },
        ["project", "data_title"],
    )
    def get_labels(project: str, data_title: str) -> str:
        p = find_project(project)
        found = rows(p, data_title_eq=data_title)
        if not found:
            raise ToolFailure(f"no data unit titled {data_title!r} in {p.title}")
        row = found[0]
        row.initialise_labels()
        objects = Counter(o.ontology_item.name for o in row.get_object_instances())
        classifications = Counter(_classification_name(c) for c in row.get_classification_instances())
        summary = [
            f"{row.data_title}  stage={_stage(row)}  status={_status(row) or '-'}",
            f"objects: {', '.join(f'{k} ×{v}' for k, v in objects.items()) or 'none'}",
            f"classifications: {', '.join(f'{k} ×{v}' for k, v in classifications.items()) or 'none'}",
        ]
        return "\n".join(summary) + "\n\n" + json.dumps(row.to_encord_dict(), default=str)[:40_000]

    @server.tool(
        "get_ontology",
        "A project's ontology: object classes with their shapes, and classifications.",
        {"project": project_arg},
        ["project"],
    )
    def get_ontology(project: str) -> str:
        return ontology_text(find_project(project))

    @server.tool(
        "assign_task",
        "Assign the task of one data unit, in a workflow stage, to a person (by email).",
        {
            "project": project_arg,
            "stage": {"type": "string", "description": "The workflow stage, e.g. Annotate"},
            "data_title": {"type": "string"},
            "assignee": {"type": "string", "description": "The person's email in Encord"},
        },
        ["project", "stage", "data_title", "assignee"],
        writes=True,
    )
    def assign_task(project: str, stage: str, data_title: str, assignee: str) -> str:
        p = find_project(project)
        try:
            workflow_stage = p.workflow.get_stage(name=stage)
        except Exception as e:
            raise ToolFailure(f"no workflow stage {stage!r} in {p.title}") from e
        tasks = list(workflow_stage.get_tasks(data_title=data_title))
        if not tasks:
            raise ToolFailure(f"no task for {data_title!r} in the {stage} stage")
        for task in tasks:
            task.assign(assignee)
        return f"Assigned {len(tasks)} task(s) for {data_title} in {stage} to {assignee}."

    @server.tool(
        "set_priority",
        "Set the priority of a data unit's task (0 to 1; higher is worked on first).",
        {"project": project_arg, "data_title": {"type": "string"}, "priority": {"type": "number"}},
        ["project", "data_title", "priority"],
        writes=True,
    )
    def set_priority(project: str, data_title: str, priority: float) -> str:
        if not 0 <= float(priority) <= 1:
            raise ToolFailure("priority must be between 0 and 1")
        p = find_project(project)
        found = rows(p, data_title_eq=data_title)
        if not found:
            raise ToolFailure(f"no data unit titled {data_title!r} in {p.title}")
        for row in found:
            row.set_priority(float(priority))
        return f"Set the priority of {data_title} to {float(priority)}."

    return server


def _classification_name(instance) -> str:
    classification = getattr(instance, "ontology_item", None)
    attributes = getattr(classification, "attributes", None) or []
    return attributes[0].name if attributes else str(getattr(classification, "feature_node_hash", "?"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lcode-mcp-encord", description="MCP server for Encord (stdio).")
    parser.add_argument("--allow-writes", action="store_true", help="also offer assigning tasks and setting priorities")
    args = parser.parse_args(argv)
    try:
        from encord import EncordUserClient
    except ImportError:
        parser.error('needs the Encord SDK: run it with uvx --from "lcode-cli[encord]" lcode-mcp-encord')
    if os.environ.get("ENCORD_SSH_KEY_FILE"):
        key_file = os.path.expanduser(os.environ["ENCORD_SSH_KEY_FILE"])
        if not os.path.isfile(key_file):
            parser.error(f"ENCORD_SSH_KEY_FILE is {key_file}, which doesn't exist")
        os.environ["ENCORD_SSH_KEY_FILE"] = key_file
    elif not os.environ.get("ENCORD_SSH_KEY"):
        parser.error("set ENCORD_SSH_KEY_FILE to the path of an SSH key registered in Encord (Settings > Public keys)")
    client = EncordUserClient.create_with_ssh_private_key(
        domain=os.environ.get("ENCORD_DOMAIN") or "https://api.encord.com"
    )
    build(client, args.allow_writes).serve()


if __name__ == "__main__":
    main()
