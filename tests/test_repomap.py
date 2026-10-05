from pathlib import Path

from conftest import reply
from lcode import repomap

PY = '''"""Shopping cart."""

TAX_RATE = 0.2


class Cart(Base):
    def __init__(self, owner: str):
        self.items = []

    def add(self, name: str, price: float, quantity: int = 1) -> None:
        self.items.append((name, price, quantity))

    def _hidden(self):
        pass


async def checkout(cart: Cart, *, coupon: str | None = None) -> float:
    return 0.0
'''


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_python_symbols_with_signatures():
    symbols = repomap.python_symbols(PY)
    assert [(s.line, s.text, s.top) for s in symbols] == [
        (3, "TAX_RATE", True),
        (6, "class Cart(Base)", True),
        (7, "def __init__(self, owner: str)", False),
        (10, "def add(self, name: str, price: float, quantity: int=1) -> None", False),
        (17, "async def checkout(cart: Cart, *, coupon: str | None=None) -> float", True),
    ]
    assert repomap.python_symbols("def broken(:\n") == []


def test_other_languages_by_their_declarations():
    ts = repomap.pattern_symbols(
        "export interface Shape {}\nexport class Square implements Shape {\n  area(): number {\n    return 1;\n  }\n}\n"
        "export function totalArea(shapes: Shape[]): number {}\nexport const PI = 3.14;\n"
        "export const scale = (s: Shape, by: number): Shape => s;\n",
        "ts",
    )
    assert [s.text for s in ts] == [
        "interface Shape",
        "class Square",
        "area()",
        "function totalArea(shapes: Shape[])",
        "const PI",
        "function scale(s: Shape, by: number)",
    ]
    go = repomap.pattern_symbols(
        "type Store struct {\n}\nfunc NewStore() *Store {\n}\nfunc (s *Store) Get(k string) int {\n}\n", "go"
    )
    assert [s.text for s in go] == ["type Store struct", "func NewStore()", "func (Store) Get(k string)"]
    rs = repomap.pattern_symbols(
        "pub struct Cache {\n}\nimpl Evict for Cache {\n}\npub fn make(size: usize) -> Cache {\n}\n", "rs"
    )
    assert [s.text for s in rs] == ["struct Cache", "impl Evict for Cache", "fn make(size: usize)"]


def test_files_used_everywhere_come_first_and_tests_last(tmp_path):
    write(
        tmp_path,
        {
            "core/engine.py": "class Engine:\n    pass\n\n\ndef start_engine():\n    pass\n",
            "core/helpers.py": "def rarely_used_helper():\n    pass\n",
            "a.py": "from core.engine import Engine, start_engine\n",
            "b.py": "from core.engine import Engine\n",
            "c.py": "from core.engine import start_engine\n",
            "tests/test_engine.py": "from core.engine import Engine\n\ndef test_engine_starts():\n    Engine()\n",
            "node_modules/dep/index.js": "export function ignored() {}\n",
        },
    )
    maps = repomap.build(tmp_path)
    order = [m.path for m in maps if m.symbols]
    assert order[0] == "core/engine.py" and order[-1] == "tests/test_engine.py"
    assert not any("node_modules" in m.path for m in maps)
    text, complete = repomap.render(maps, 1000)
    assert complete and text.startswith("core/engine.py:\n  1: class Engine\n  5: def start_engine()")


def test_the_map_fits_its_budget_and_zooms_into_folders(tmp_path):
    files = {
        f"pkg{i}/mod{j}.py": f"def function_{i}_{j}(a, b):\n    return a\n\n\nclass Thing{i}{j}:\n    pass\n"
        for i in range(10)
        for j in range(10)
    }
    write(tmp_path, files)
    repo = repomap.RepoMap(tmp_path)
    assert repo.for_prompt() == ""  # too big for the system prompt
    text = repo.for_tool(tokens=300)
    assert len(text) <= 300 * 3 + 80 and text.endswith("(cut to fit: give a folder to see more of it)")
    zoomed = repo.for_tool("pkg3")
    assert all(line.startswith(("pkg3/", "  ")) for line in zoomed.splitlines())
    assert "pkg3/mod9.py:" in zoomed
    assert repo.for_tool("nothing-here") == "No source files with symbols under nothing-here."


def test_a_small_repository_gets_its_map_in_the_prompt(make_agent, repo):
    agent = make_agent(repo_map=True)
    system = agent.messages[0]["content"]
    assert "# Repository map" in system and "src/pkg/math.py:\n  1: def add(a, b)" in system
    assert "repo_map" not in agent.tool_names()  # it's already in the prompt
    off = make_agent(repo_map=False)
    assert "# Repository map" not in off.messages[0]["content"]


def test_a_larger_repository_gets_the_tool(make_agent, monkeypatch):
    monkeypatch.setattr(repomap.RepoMap, "for_prompt", lambda self: "")
    agent = make_agent(
        [reply(tool_calls=[{"function": {"name": "repo_map", "arguments": {"path": "src"}}}]), reply("ok")],
        repo_map=True,
    )
    assert "# Repository map" not in agent.messages[0]["content"] and "repo_map" in agent.tool_names()
    agent.run_turn("where do I start?")
    assert agent.messages[3]["content"].startswith("src/pkg/math.py:\n  1: def add(a, b)")
