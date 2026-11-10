import base64
import io
import shutil

import pytest
from rich.console import Console

from conftest import FakeOllama, output, reply
from lcode import catalog, cli, config, hardware, residency
from lcode.agent import Progress
from lcode.hardware import Hardware
from lcode.residency import Placement, RamFit

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def entry(name, size_gb, gpu_gb, context=0):
    return {"name": name, "size": size_gb * 1e9, "size_vram": gpu_gb * 1e9, "context_length": context}


# What Ollama 0.40.13 lists for lcode-qwen3.6-35b at 32K on the laptop: the ~12 GB of experts that stay mapped from
# the model's file in RAM aren't in its size, so 98% of it seems to be on the GPU.
OLLAMA_0_40 = {**entry("lcode-qwen3.6-35b:latest", 10.49, 10.29, 32768), "model": "lcode-qwen3.6-35b:latest"}


def text(agent):
    return " ".join(output(agent).split())


def test_where_a_model_sits():
    assert Placement("m", 22.3e9, 9.8e9).describe() == "22.3 GB, 44% on the GPU and the rest in system RAM"
    assert Placement("m", 6e9, 6e9).describe() == "6.0 GB, all on the GPU"
    assert Placement("m", 6e9, 0).describe() == "6.0 GB, all in system RAM (on the CPU)"


def estimated(model, size_gb, gpu_gb, context=32768, fallback=32768):
    return residency.with_estimate(Placement(model, size_gb * 1e9, gpu_gb * 1e9, context), model, fallback).describe()


def test_the_part_of_a_catalog_model_ollama_leaves_out_is_counted():
    assert estimated("lcode-qwen3.6-35b", 10.49, 10.29) == "~23 GB, 42% on the GPU and the rest in system RAM"
    assert f"~{catalog.find('qwen3.6-35b').memory_gib(32768):.0f} GB" == "~23 GB"  # as lcode setup and doctor say
    assert estimated("qwen3.6-35b", 10.49, 10.29, context=262144) == "~28 GB, 35% on the GPU and the rest in system RAM"
    assert estimated("qwen3.6-35b", 10.49, 10.29, context=0) == "~23 GB, 42% on the GPU and the rest in system RAM"
    assert estimated("qwen3.5-4b", 1.2, 0) == "~5 GB, all in system RAM (on the CPU)"  # no GPU: all of it is in RAM


def test_what_ollama_counts_stands_when_nothing_is_left_out_or_nothing_is_known():
    assert estimated("qwen3.5-9b", 8.5, 8.5, context=65536) == "8.5 GB, all on the GPU"  # below the estimate
    assert estimated("qwen3.6-35b", 26, 10) == "26.0 GB, 38% on the GPU and the rest in system RAM"  # above it
    assert estimated("custom:7b", 10.49, 10.29) == "10.5 GB, 98% on the GPU and the rest in system RAM"  # no catalog


def test_dense_models_that_dont_fit_get_a_warning(make_agent):
    ollama = make_agent().ollama
    warning = residency.dense_warning(ollama, "qwen3.8-27b", 32768, LAPTOP)
    assert warning.startswith("qwen3.8-27b is a dense model: all 27B parameters work on every token.")
    assert "Only about 61% of it fits in the 12 GB of GPU memory" in warning and "/model qwen3.6-35b" in warning
    assert residency.dense_warning(ollama, "qwen3.6-35b", 262144, LAPTOP) == ""  # MoE: fine split
    assert residency.dense_warning(ollama, "qwen3.5-9b", 32768, LAPTOP) == ""  # dense, but it fits
    assert residency.dense_warning(ollama, "qwen3.8-27b", 32768, Hardware("darwin", "M4", 48, unified=True)) == ""
    cpu_only = residency.dense_warning(ollama, "qwen3.5-9b", 32768, Hardware("linux", "x", 31))
    assert "There's no GPU here, so it runs on the CPU" in cpu_only


def test_models_outside_the_catalog_are_asked_about(make_agent):
    ollama = make_agent().ollama
    ollama.show = lambda model: {"model_info": {"general.architecture": "qwen35moe", "qwen35moe.expert_count": 256}}
    assert residency.is_moe(ollama, "custom:7b") is True
    ollama.show = lambda model: {"model_info": {"general.architecture": "llama"}}
    assert residency.is_moe(ollama, "custom:7b") is False
    ollama.show = lambda model: {}
    assert residency.is_moe(ollama, "custom:7b") is None


def test_a_load_is_reported_once_with_how_it_fits(make_agent):
    agent = make_agent([reply("hi"), reply("again")], context=32768)
    agent.hardware = LAPTOP
    agent.ollama.loaded = []

    def chat_stream(payload, on_open=None):
        agent.ollama.payloads.append(payload)
        agent.ollama.loaded = [OLLAMA_0_40]
        yield from agent.ollama.scripts.pop(0)

    agent.ollama.chat_stream = chat_stream
    agent.run_turn("hello")
    assert "◆ Loaded lcode-qwen3.6-35b: ~23 GB, 42% on the GPU and the rest in system RAM." in text(agent)
    assert "⚠" not in output(agent)  # as much is on the GPU as usual
    agent.console.file.truncate(0)
    agent.run_turn("and again")
    assert "◆" not in output(agent) and "⚠" not in output(agent)  # in memory: nothing to say


def report(model, size_gb, gpu_gb, context=32768, dense=False):
    placement = Placement("m", size_gb * 1e9, gpu_gb * 1e9, context)
    return residency.load_report(model, 14, placement, context, LAPTOP, dense=dense)


def test_less_on_the_gpu_than_usual_is_explained():
    # Another program holds GPU memory: Ollama counts 4.2 GB, 95% of it on the GPU, and leaves out the rest in RAM.
    assert report("qwen3.6-35b", 4.2, 4.0) == (
        "Loaded qwen3.6-35b in 14s: ~23 GB, 16% on the GPU and the rest in system RAM. Less of it is on the GPU than "
        "usual (up to 53% fits): another program or model is using GPU memory, so it runs slower until that's closed."
    )
    assert report("qwen3.6-35b", 10.49, 10.29) == (
        "Loaded qwen3.6-35b in 14s: ~23 GB, 42% on the GPU and the rest in system RAM."  # the usual 9-10 GB
    )
    assert "(all of it fits)" in report("qwen3.5-9b", 7.7, 7.5, context=131072, dense=True)
    split = "It's a dense model split between GPU and CPU, so expect it to be slow."
    assert report("qwen3.8-27b", 18, 11, dense=True).endswith(split)
    assert report("qwen3.8-27b", 11.2, 11, dense=True).endswith(split)  # with the part in RAM left out, too


def test_the_status_line_says_what_comes_before_the_first_token(make_agent):
    agent = make_agent(model="qwen3.6-35b", context=32768)
    agent.ollama.loaded = []
    progress = Progress()
    watch = agent.before_request(progress)
    assert progress.phase == "Loading qwen3.6-35b into memory" and watch is not None
    agent.ollama.loaded = [entry("qwen3.6-35b", 22.3, 9.8, 32768)]
    watch.run()  # what the watching thread does once the model is in memory
    assert progress.phase.startswith("Reading the conversation (")
    watch.stop()
    agent.ollama.loaded = [entry("qwen3.6-35b", 22.3, 9.8, 65536)]
    progress = Progress()
    agent.before_request(progress).stop()
    assert progress.phase == "Reloading qwen3.6-35b with a 32K context"
    agent.ollama.loaded = [entry("qwen3.6-35b", 22.3, 9.8, 32768)]
    agent.sent_chars = 0
    agent.messages.append({"role": "tool", "content": "x" * 30000})
    progress = Progress()
    assert agent.before_request(progress) is None and progress.phase.startswith("Reading ")
    agent.sent_chars = sum(len(m["content"]) for m in agent.messages)
    progress = Progress()
    assert agent.before_request(progress) is None and progress.phase == "Thinking"
    agent.ollama.kind = "openai"  # other servers place their models themselves
    assert agent.before_request(Progress()) is None


def test_the_vision_model_making_room_is_said_and_the_reload_named(make_agent, repo):
    (repo / "bug.png").write_bytes(PNG)
    agent = make_agent(model="qwen3.6-35b")
    agent.ollama.capabilities["qwen3.6:35b-a3b-coding"] = ["completion", "vision"]
    agent._vision = False
    agent.ollama.loaded = [entry("qwen3.6-35b", 22.3, 9.8)]
    real_chat = agent.ollama.chat

    def chat(payload):
        agent.ollama.loaded = [entry("qwen3.6:35b-a3b-coding", 23, 10)]  # Ollama swapped the models
        return real_chat(payload)

    agent.ollama.chat = chat
    agent.look(repo / "bug.png")
    assert "◆ qwen3.6-35b was unloaded to make room for qwen3.6:35b-a3b-coding; it reloads for the next step." in (
        text(agent)
    )
    progress = Progress()
    agent.before_request(progress).stop()
    assert (
        progress.phase == "Reloading qwen3.6-35b into memory (it made room for the vision model qwen3.6:35b-a3b-coding)"
    )


def test_the_status_bar_shows_a_background_load(make_agent):
    from lcode import repl

    agent = make_agent(model="qwen3.6-35b", context=32768)
    agent.ollama.loaded = []
    started = []
    agent.ollama.load = lambda *args: started.append(agent.load_state)
    repl.preload(agent)
    for _ in range(100):
        if started and agent.load_state == "":
            break
        __import__("time").sleep(0.01)
    assert started == ["loading"] and agent.load_state == ""
    agent.ollama.loaded = [entry("qwen3.6-35b", 22.3, 9.8, 32768)]
    started.clear()
    repl.preload(agent)  # already in memory: nothing to load
    assert started == []


def test_the_status_bar_text_while_loading(make_agent, monkeypatch, tmp_path):
    from lcode import repl

    monkeypatch.setattr(repl, "STATE_DIR", tmp_path)
    agent = make_agent()
    session = repl.build_session(agent)
    assert "loading the model" not in session.bottom_toolbar().value
    agent.load_state = "loading"
    assert "loading the model into memory…" in session.bottom_toolbar().value


def test_switching_models_picks_the_vision_model_again(make_agent, monkeypatch):
    from lcode.repl import handle_command

    agent = make_agent()
    agent._vision = "qwen3.8:27b"  # decided for the previous model
    monkeypatch.setattr("lcode.cli.resolve_model", lambda ollama, name: ("custom:7b", None))
    handle_command(agent, "/model custom:7b", LAPTOP)
    assert agent._vision != "qwen3.8:27b"  # decided again for the new model (here: none can see)


# ----------------------------------------------------------------------------- short of RAM


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Sets what /proc says: the RAM available and what a running Ollama holds for itself, and a program beside it."""

    def setup(available: float | None, ollama_private: float | None = None, other_private: float = 3.0):
        proc = tmp_path / "fake-proc"
        shutil.rmtree(proc, ignore_errors=True)
        proc.mkdir()
        if available is not None:
            kib = int(available * 2**20)
            (proc / "meminfo").write_text(f"MemTotal: 32461080 kB\nMemFree: 1024 kB\nMemAvailable: {kib} kB\n")
        programs = {"1": (b"/usr/bin/ide\0--open\0", other_private)}
        if ollama_private is not None:
            programs["2"] = (b"/usr/lib/ollama/llama-server\0--model\0sha256-abc\0", ollama_private)
            programs["3"] = (b"/bin/ollama\0serve\0", 0.0)
        for pid, (cmdline, private) in programs.items():
            (proc / pid).mkdir()
            (proc / pid / "cmdline").write_bytes(cmdline)
            status = f"Name:\tx\nVmRSS:\t 99999999 kB\nRssAnon:\t {int(private * 2**20)} kB\nRssFile:\t 1234 kB\n"
            (proc / pid / "status").write_text(status)
        (proc / "4").mkdir()  # a process that ended while lcode looked: its files are gone
        monkeypatch.setattr("lcode.hardware.PROC", proc)

    return setup


def here(ollama):
    """Ollama on this machine, where RAM can be checked: FakeOllama's address is not a local one."""
    ollama.host = "http://localhost:11434"
    return ollama


def fit_at(available, key="qwen3.6-35b", context=32768, hw=LAPTOP):
    return RamFit(catalog.find(key), context, hw, available)


def test_what_linux_says_about_ram_is_read(machine):
    machine(available=6.0, ollama_private=9.0)
    assert hardware.available_ram_gib() == pytest.approx(6.0)
    assert hardware.ollama_private_gib() == pytest.approx(9.0)  # the program beside it isn't counted
    runner = hardware.PROC / "5"  # Ollama can also run a model as a process of its own program
    runner.mkdir()
    (runner / "cmdline").write_bytes(b"/usr/local/bin/ollama\0runner\0--model\0sha256-abc\0")
    (runner / "status").write_text(f"RssAnon:\t {2**20} kB\n")
    assert hardware.ollama_private_gib() == pytest.approx(10.0)


def test_nothing_is_known_about_ram_off_linux(monkeypatch, tmp_path):
    monkeypatch.setattr("lcode.hardware.PROC", tmp_path / "no-proc-here")
    assert hardware.available_ram_gib() is None and hardware.ollama_private_gib() == 0


def test_the_model_outside_the_gpu_and_the_prompt_cache_have_to_fit_in_ram():
    fit = fit_at(25.0)
    assert not fit.short
    assert fit.describe() == (
        "~25 GB available now, ~21 GB needed in a long session: ~11 GB of the model that doesn't fit on the GPU, "
        "up to 10 GB for Ollama's server and prompt cache"
    )
    assert fit_at(15.0).short and not fit_at(21.0).short  # as the rounded numbers on the screen say


def test_what_ollama_already_holds_counts_as_available(make_agent, machine):
    # Measured during a run short of RAM: 6 GiB available, llama-server holding 9 GiB, its cache full.
    machine(available=6.0, ollama_private=9.0)
    spec = catalog.find("qwen3.6-35b")
    ollama = here(make_agent().ollama)
    fit = residency.ram_fit(ollama, spec, 32768, LAPTOP)
    assert fit.available == pytest.approx(15.0) and fit.short
    # With the same cache but the machine's other programs closed, it fits: the cache isn't counted twice.
    machine(available=13.0, ollama_private=9.0)
    assert not residency.ram_fit(ollama, spec, 32768, LAPTOP).short


def test_short_of_ram_says_what_to_do_with_the_commands_that_do_it():
    advice = fit_at(22.0, context=262144).advice()
    assert advice == (
        "Close other programs to free ~4 GB, use a smaller context (saves ~4 GB: lcode config set context 64k), or "
        "pick a smaller model such as qwen3.5-9b (lcode setup qwen3.5-9b)."
    )
    assert fit_at(22.0, context=262144).advice(residency.SESSION_COMMANDS) == (
        "Close other programs to free ~4 GB, use a smaller context (saves ~4 GB: /ctx 64k), or "
        "pick a smaller model such as qwen3.5-9b (/model qwen3.5-9b)."
    )
    assert "When RAM runs short, Linux drops the model from memory" in fit_at(22.0).why()


def test_the_smallest_context_is_suggested_when_none_is_enough():
    advice = fit_at(15.0, context=262144).advice()  # no context makes it fit: 32K saves the most
    assert "use a smaller context (saves ~5 GB: lcode config set context 32k)" in advice


def test_no_smaller_context_is_suggested_from_the_smallest_useful_one():
    assert "context" not in fit_at(15.0).advice()


def test_a_smaller_model_is_suggested_only_with_the_context_it_would_get():
    # laguna-xs-2.1 is smaller, and fits at 32K, but `lcode setup` gives it 256K: too much for 22 GB.
    assert (
        catalog.find("laguna-xs-2.1").ram_needed_gib(32768, LAPTOP)
        < 22
        < (catalog.find("laguna-xs-2.1").ram_needed_gib(262144, LAPTOP))
    )
    assert "qwen3.5-9b" in fit_at(22.0, context=262144).smaller_model().key
    assert "pick a smaller model (lcode models)" in fit_at(9.0).advice()  # none fits what's left: look at the list


def test_dense_models_that_would_be_split_are_not_suggested():
    gpu = Hardware("linux", "x", 64, "GPU", 12)
    # qwen3.8-27b and qwen3.6-27b come first and fit in 19 GB of RAM, but ~8 tok/s when split isn't an improvement.
    assert catalog.find("qwen3.8-27b").ram_needed_gib(32768, gpu) < 19
    assert fit_at(19.0, key="nemotron-3.5-lightning", hw=gpu).smaller_model().key == "qwen3.5-9b"


def test_closing_programs_isnt_suggested_when_it_cant_be_enough():
    small = Hardware("linux", "x", 16, "RTX 4060", 8)
    advice = fit_at(8.0, key="gpt-oss-20b", context=65536, hw=small).advice()
    assert "close other programs" not in advice.lower() and "use a smaller context" in advice.lower()


def test_a_model_all_on_the_gpu_only_needs_room_for_the_cache():
    fit = fit_at(7.0, key="qwen3.5-9b")
    assert fit.short and fit.describe() == (
        "~7 GB available now, ~10 GB needed in a long session: up to 10 GB for Ollama's server and prompt cache"
    )
    assert "swap" in fit.why() and "reading it back from disk" not in fit.why()
    assert fit.advice() == "Close other programs to free ~3 GB."  # a smaller model or context saves nothing


def test_without_a_gpu_all_of_the_model_stays_in_ram():
    cpu = Hardware("linux", "x", 31)
    assert "~8 GB for the model" in fit_at(5.0, key="qwen3.5-9b", hw=cpu).describe()


def test_nothing_to_suggest_on_a_machine_with_too_little_ram():
    tiny = Hardware("linux", "x", 8)
    assert fit_at(5.0, key="qwen3.5-4b", hw=tiny).advice() == (
        "This machine has too little RAM for that, so expect long sessions to slow down."
    )


def test_ram_isnt_checked_when_it_cant_be_told(make_agent, machine):
    machine(available=2.0, ollama_private=0.0)
    spec = catalog.find("qwen3.6-35b")
    ollama = make_agent().ollama
    assert residency.ram_fit(ollama, spec, 32768, LAPTOP) is None  # Ollama on another machine: its RAM counts
    ollama.host = "http://192.0.2.7:11434"
    assert residency.ram_fit(ollama, spec, 32768, LAPTOP) is None
    for host in ("http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434"):
        ollama.host = host
        assert residency.ram_fit(ollama, spec, 32768, LAPTOP) is not None
    ollama.kind = "openai"  # another server manages its own memory
    assert residency.ram_fit(ollama, spec, 32768, LAPTOP) is None
    del ollama.kind
    machine(available=None)  # not Linux
    assert residency.ram_fit(ollama, spec, 32768, LAPTOP) is None


# -- lcode doctor and lcode setup


class LocalOllama(FakeOllama):
    def __init__(self):
        super().__init__()
        self.host = "http://localhost:11434"

    def version(self):
        return "0.40.13"


@pytest.fixture
def cli_here(monkeypatch, tmp_path):
    """The lcode command, on the laptop, with Ollama on this machine; returns what it printed so far."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('model = "qwen3.6-35b"\ncontext = 32768\ncomfyui_url = "http://127.0.0.1:9"\n')
    monkeypatch.setattr(config, "CONFIG_PATH", config_file)
    monkeypatch.setattr(cli, "Ollama", lambda host: LocalOllama())
    monkeypatch.setattr(cli, "detect", lambda: LAPTOP)
    monkeypatch.setattr(cli, "console", Console(file=io.StringIO(), width=2000, highlight=False))
    monkeypatch.chdir(tmp_path)
    return lambda: cli.console.file.getvalue()


def run_doctor() -> int:
    with pytest.raises(SystemExit) as stop:
        cli.main(["doctor"])
    return stop.value.code


def test_doctor_says_when_ram_is_short_and_what_to_do(cli_here, machine):
    machine(available=6.0, ollama_private=9.0)
    assert run_doctor() == 0  # a warning: the installation works
    lines = cli_here().splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("! RAM"))
    assert lines[at - 1].startswith("✓ Context    32K tokens")
    assert lines[at] == (
        "! RAM        ~15 GB available now, ~21 GB needed in a long session: ~11 GB of the model that doesn't fit on "
        "the GPU, up to 10 GB for Ollama's server and prompt cache"
    )
    assert lines[at + 1].startswith("!            When RAM runs short, Linux drops the model from memory")
    assert lines[at + 1].endswith(
        "Close other programs to free ~6 GB, or pick a smaller model such as qwen3.5-9b (lcode setup qwen3.5-9b)."
    )


def test_doctor_shows_the_ram_a_model_needs_when_it_fits(cli_here, machine):
    machine(available=25.0, ollama_private=0.0)
    assert run_doctor() == 0
    assert "✓ RAM        ~25 GB available now, ~21 GB needed in a long session" in cli_here()
    assert "When RAM runs short" not in cli_here()


def test_doctor_says_nothing_about_ram_it_cant_tell(cli_here, machine):
    machine(available=None)
    assert run_doctor() == 0
    assert "RAM     " not in cli_here()


def test_doctor_shows_how_the_loaded_model_fits(cli_here, machine, monkeypatch):
    machine(available=25.0, ollama_private=0.0)
    ollama = LocalOllama()
    ollama.loaded = [OLLAMA_0_40]  # as lcode-qwen3.6-35b:latest, while the model resolves to lcode-qwen3.6-35b
    monkeypatch.setattr(cli, "Ollama", lambda host: ollama)
    assert run_doctor() == 0
    assert "✓ Loaded     ~23 GB, 42% on the GPU and the rest in system RAM · 32K context\n" in cli_here()
    ollama.loaded = [entry("qwen3.5-9b", 6.6, 6.6, 131072)]  # another model
    assert run_doctor() == 0
    assert cli_here().count("Loaded") == 1


def test_setup_warns_about_ram_on_its_recommendation(cli_here, machine, monkeypatch):
    machine(available=22.0, ollama_private=0.0)
    monkeypatch.setattr(cli.Confirm, "ask", lambda *args, **kwargs: False)
    cli.main(["setup", "--context", "256k"])
    printed = " ".join(cli_here().split())
    assert "Recommended: Qwen3.6 35B-A3B Coding" in printed
    assert "warning: RAM is short: ~22 GB available now, ~26 GB needed in a long session" in printed
    assert "use a smaller context (saves ~4 GB: lcode setup qwen3.6-35b --context 64k)" in printed
    assert "pick a smaller model such as qwen3.5-9b (lcode setup qwen3.5-9b)" in printed
    assert printed.endswith("Nothing changed. See all options with `lcode models`.")


def test_setup_is_quiet_about_ram_that_fits_and_one_warning_is_enough(cli_here, machine, monkeypatch):
    machine(available=30.0, ollama_private=0.0)
    monkeypatch.setattr(cli.Confirm, "ask", lambda *args, **kwargs: False)
    cli.main(["setup", "qwen3.6-35b", "--context", "32k"])
    assert "warning" not in cli_here()
    machine(available=2.0, ollama_private=0.0)
    monkeypatch.setattr(cli, "detect", lambda: Hardware("linux", "x", 16, "RTX 4060", 4))
    cli.main(["setup", "qwen3.6-35b", "--context", "32k"])
    assert "this probably doesn't fit in memory" in cli_here() and "RAM is short" not in cli_here()


# -- in a session


def timed_reply(speed: float, tokens: int = 200) -> list[dict]:
    chunks = reply("done")
    chunks[-1].update(eval_count=tokens, eval_duration=int(tokens / speed * 1e9))
    return chunks


def session(make_agent, speeds, **settings):
    """A session on the laptop, with Ollama on this machine, whose replies come at these tokens a second."""
    agent = make_agent([timed_reply(speed) for speed in speeds], model="qwen3.6-35b", context=32768, **settings)
    here(agent.ollama)
    agent.hardware = LAPTOP
    return agent


def test_a_session_that_slows_down_says_ram_is_the_likely_cause_once(make_agent, machine):
    machine(available=6.0, ollama_private=9.0)
    agent = session(make_agent, [65, 66, 64, 20, 21, 19, 20, 19])
    for _ in range(5):
        agent.run_turn("go on")
    assert "⚠" not in output(agent)  # two slow replies could be anything
    agent.run_turn("go on")
    said = text(agent)
    assert "⚠ Replies slowed from 65 to 20 tokens a second, most likely because RAM is short: ~15 GB available" in said
    assert "Close other programs to free ~6 GB, or pick a smaller model such as qwen3.5-9b (/model qwen3.5-9b)." in said
    agent.console.file.truncate(0)
    agent.run_turn("go on")
    agent.run_turn("and on")
    assert "⚠" not in output(agent)  # once


def test_a_session_that_slows_down_with_ram_to_spare_blames_nothing(make_agent, machine):
    machine(available=25.0, ollama_private=0.0)
    agent = session(make_agent, [65, 66, 64, 20, 21, 19])
    for _ in range(6):
        agent.run_turn("go on")
    assert "⚠" not in output(agent)
    assert not agent.reply_speeds.explained  # RAM may still run short later


def test_only_replies_of_some_length_tell_the_speed(make_agent, machine):
    machine(available=6.0, ollama_private=9.0)
    agent = session(make_agent, [])
    agent.ollama.scripts = [timed_reply(65) for _ in range(3)] + [timed_reply(5, tokens=20) for _ in range(5)]
    for _ in range(8):
        agent.run_turn("go on")
    assert "⚠" not in output(agent)
    assert residency.SpeedWatch().add("m", 32768, 200, 0.0) is None  # a server that doesn't say how long it took


def test_a_small_drop_in_speed_says_nothing(make_agent, machine):
    machine(available=6.0, ollama_private=9.0)
    agent = session(make_agent, [65, 66, 64, 40, 41, 39])
    for _ in range(6):
        agent.run_turn("go on")
    assert "⚠" not in output(agent)


def test_another_model_or_context_starts_the_comparison_again():
    watch = residency.SpeedWatch()
    for speed in (65, 66, 64):
        assert watch.add("big", 32768, 200, 200 / speed) is None
    assert watch.add("small", 32768, 200, 10.0) is None
    assert watch.add("small", 32768, 200, 10.0) is None
    assert watch.add("small", 32768, 200, 10.0) is None  # nothing to compare with: another model's speed doesn't count
    assert watch.add("small", 65536, 200, 10.0) is None
    assert watch.speeds == [20.0]


def test_a_slowdown_is_only_explained_on_this_machine_and_for_a_known_model(make_agent, machine):
    machine(available=6.0, ollama_private=9.0)
    agent = session(make_agent, [65, 66, 64, 20, 21, 19])
    agent.ollama.host = "http://192.0.2.7:11434"
    for _ in range(6):
        agent.run_turn("go on")
    assert "⚠" not in output(agent)
    agent = session(make_agent, [65, 66, 64, 20, 21, 19])
    agent.settings.model = "custom:7b"  # outside the catalog: its needs aren't known
    for _ in range(6):
        agent.run_turn("go on")
    assert "⚠" not in output(agent)
    agent = session(make_agent, [65, 66, 64, 20, 21, 19])
    agent.hardware = None  # a subagent: the session it works for watches the speed
    for _ in range(6):
        agent.run_turn("go on")
    assert "⚠" not in output(agent)
