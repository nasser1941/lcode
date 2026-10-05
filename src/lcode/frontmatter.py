"""The `---` header of markdown files: memory notes, agents, commands and skills.

A small, lenient subset of YAML, enough for these headers without a YAML dependency:
`key: value` lines, quoted values, block scalars (`key: |` and `key: >`, with indented lines below),
and nested maps or lists (kept as raw text). Skills written for other agents sometimes have values
that strict YAML rejects, such as an unquoted colon in a description; those are read as plain text.
"""

from __future__ import annotations

import re

BLOCK = re.compile(r"^[|>][+-]?$")


def split(text: str) -> tuple[dict[str, str], str] | None:
    """The header fields and the body after them, or None when the text has no header."""
    m = re.match(r"﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)", text, re.S)
    if not m:
        return None
    return parse(m.group(1)), m.group(2)


def parse(header: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    lines = header.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        nested = []
        while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
            nested.append(lines[i])
            i += 1
        while nested and not nested[-1].strip():
            nested.pop()
        if BLOCK.match(value):
            indent = min((len(n) - len(n.lstrip()) for n in nested if n.strip()), default=0)
            body = [n[indent:] for n in nested]
            if value.startswith(">"):
                value = re.sub(r"(?<!\n)\n(?!\n)", " ", "\n".join(body)).strip()
            else:
                value = "\n".join(body).rstrip()
        elif not value and nested:
            value = "\n".join(nested)  # a nested map or list, kept as text
        elif nested:
            value = " ".join([value, *(n.strip() for n in nested)]).strip()  # a plain value over several lines
        fields[key] = unquote(value)
    return fields


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        inner = value[1:-1]
        return inner.replace("''", "'") if value[0] == "'" else inner.replace('\\"', '"').replace("\\n", "\n")
    return value
