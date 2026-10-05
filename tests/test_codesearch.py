import re

import pytest

from conftest import FakeOllama, reply
from lcode import codesearch

VOCABULARY = ["upload", "retry", "wait", "invoice", "tax", "login", "password", "chart"]


class EmbeddingOllama(FakeOllama):
    """Embeds text as counts of a few words, which is enough to test search, and counts the work done."""

    def __init__(self, scripts=None):
        super().__init__(scripts)
        self.embedded: list[str] = []
        self.options: list[dict] = []

    def installed_names(self):
        return {"lcode-qwen3.6-35b", "nomic-embed-text"}

    def embed(self, model, inputs, options=None, keep_alive="5m"):
        self.embedded += inputs
        self.options.append(options or {})
        return [[float(len(re.findall(word, text.lower()))) + 0.01 for word in VOCABULARY] for text in inputs]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("lcode.config.STATE_DIR", tmp_path / "state")
    root = tmp_path / "shop"
    (root / "billing").mkdir(parents=True)
    (root / "storage").mkdir()
    (root / "billing" / "tax.py").write_text("def vat(invoice):\n    # tax on an invoice\n    return invoice * 0.2\n")
    (root / "storage" / "send.py").write_text(
        "def send(part):\n    # upload a part; on failure wait and retry the upload\n    pass\n"
        + "\n" * 60
        + "def later():\n    pass\n"
    )
    return root


def test_files_become_overlapping_chunks():
    text = "\n".join(f"line {i}" for i in range(1, 101))
    found = codesearch.chunks("a.py", text)
    assert [(start, end) for start, end, _ in found] == [(1, 40), (33, 72), (65, 100)]
    assert found[0][2].startswith("a.py (lines 1-40)\nline 1")
    assert codesearch.chunks("empty.py", "\n\n") == []


def test_index_and_search(project):
    ollama = EmbeddingOllama()
    index = codesearch.Index(project, "nomic-embed-text")
    assert not index.exists()
    files, chunks = index.update(ollama, on_gpu=True)
    assert (files, chunks) == (2, 3) and ollama.options[0] == codesearch.GPU
    hits = index.search(ollama, "where is a failed upload retried after a wait")
    assert (hits[0].path, hits[0].start) == ("storage/send.py", 1)
    assert ollama.options[-1] == codesearch.CPU  # searching never moves lcode's model off the GPU
    assert index.search(ollama, "tax on invoices", folder="billing")[0].path == "billing/tax.py"
    text = codesearch.format_hits(project, hits[:1])
    assert text.startswith("storage/send.py:1-40 (match ") and "     2\t    # upload a part" in text
    reloaded = codesearch.Index(project, "nomic-embed-text")
    assert reloaded.exists() and len(reloaded.rows()) == 3


def test_only_changed_files_are_embedded_again(project):
    ollama = EmbeddingOllama()
    index = codesearch.Index(project, "nomic-embed-text")
    index.update(ollama)
    ollama.embedded.clear()
    assert index.update(ollama) == (0, 0) and ollama.embedded == []
    (project / "billing" / "tax.py").write_text("def vat(invoice):\n    return invoice * 0.21  # new tax rate\n")
    (project / "storage" / "send.py").unlink()
    assert index.update(ollama) == (1, 1)
    assert [r[0] for r in index.rows()] == ["billing/tax.py"]
    assert index.search(ollama, "tax")[0].path == "billing/tax.py"


def test_too_many_changes_wait_for_lcode_index(project):
    ollama = EmbeddingOllama()
    index = codesearch.Index(project, "nomic-embed-text")
    with pytest.raises(codesearch.SearchError, match="2 files changed since the index was built"):
        index.update(ollama, limit=1)
    with pytest.raises(codesearch.SearchError, match="run `lcode index`"):
        index.search(ollama, "anything")


def test_another_models_index_is_not_used(project):
    codesearch.Index(project, "nomic-embed-text").update(EmbeddingOllama())
    assert not codesearch.Index(project, "qwen3-embedding:0.6b").exists()


def test_picking_the_embedding_model():
    ollama = EmbeddingOllama()
    assert codesearch.pick_model(ollama, "auto") == "nomic-embed-text"
    assert codesearch.pick_model(ollama, "off") is None
    assert codesearch.pick_model(ollama, "bge-m3") is None  # not installed


def test_the_model_searches_once_the_repository_is_indexed(make_agent, repo, monkeypatch):
    agent = make_agent(
        [
            reply(tool_calls=[{"function": {"name": "search_code", "arguments": {"query": "add two numbers"}}}]),
            reply("ok"),
        ],
        embed_model="auto",
    )
    ollama = EmbeddingOllama(agent.ollama.scripts)
    agent.ollama = ollama
    agent._code_index = None
    assert "search_code" not in agent.tool_names()  # not indexed yet
    codesearch.Index(repo, "nomic-embed-text").update(ollama)
    agent._code_index = None
    assert "search_code" in agent.tool_names()
    (repo / "src" / "pkg" / "extra.py").write_text("def chart():\n    pass\n")  # refreshed before the search
    agent.run_turn("where do we add numbers?")
    result = agent.messages[3]["content"]
    assert "src/pkg/" in result and "(match " in result
    assert any("extra.py" in text for text in ollama.embedded)
