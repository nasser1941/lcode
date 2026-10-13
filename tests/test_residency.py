import base64

from conftest import output, reply
from lcode import residency
from lcode.agent import Progress
from lcode.hardware import Hardware
from lcode.residency import Placement

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def entry(name, size_gb, gpu_gb, context=0):
    return {"name": name, "size": size_gb * 1e9, "size_vram": gpu_gb * 1e9, "context_length": context}


def text(agent):
    return " ".join(output(agent).split())


def test_where_a_model_sits():
    assert Placement("m", 22.3e9, 9.8e9).describe() == "22.3 GB, 44% on the GPU and the rest in system RAM"
    assert Placement("m", 6e9, 6e9).describe() == "6.0 GB, all on the GPU"
    assert Placement("m", 6e9, 0).describe() == "6.0 GB, all in system RAM (on the CPU)"


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
    agent = make_agent([reply("hi"), reply("again")], model="qwen3.6-35b", context=32768)
    agent.hardware = LAPTOP
    agent.ollama.loaded = []

    def chat_stream(payload, on_open=None):
        agent.ollama.payloads.append(payload)
        agent.ollama.loaded = [entry("qwen3.6-35b:latest", 22.3, 9.8, 32768)]
        yield from agent.ollama.scripts.pop(0)

    agent.ollama.chat_stream = chat_stream
    agent.run_turn("hello")
    assert "◆ Loaded qwen3.6-35b: 22.3 GB, 44% on the GPU and the rest in system RAM." in text(agent)
    agent.console.file.truncate(0)
    agent.run_turn("and again")
    assert "◆" not in output(agent) and "⚠" not in output(agent)  # in memory: nothing to say


def test_less_on_the_gpu_than_usual_is_explained(make_agent):
    report = residency.load_report("qwen3.6-35b", 14, Placement("m", 22.3e9, 2e9), 32768, LAPTOP, dense=False)
    assert report.startswith("Loaded qwen3.6-35b in 14s: 22.3 GB, 9% on the GPU")
    assert "another program or model is using GPU memory" in report
    dense = residency.load_report("qwen3.8-27b", 40, Placement("m", 18e9, 11e9), 32768, LAPTOP, dense=True)
    assert dense.endswith("It's a dense model split between GPU and CPU, so expect it to be slow.")


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
    monkeypatch.setattr("lcode.cli.resolve_model", lambda ollama, name: ("lcode-qwen3.6-35b", None))
    handle_command(agent, "/model qwen3.6-35b", LAPTOP)
    assert agent._vision != "qwen3.8:27b"  # decided again for the new model (here: none can see)
