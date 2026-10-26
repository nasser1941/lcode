"""Handing the GPU from the coding model to ComfyUI: unloading waits until the memory is free, a load still going when
you leave code mode gives the GPU back, and running out of GPU memory or disk, or a crash, is said plainly."""

import threading
import time
from collections import namedtuple

import pytest

from comfy_standin import StandIn
from lcode import comfy, hardware, media, residency
from lcode.repl import preload

MODEL = "lcode-qwen3.6-35b"


class Ollama:
    """Ollama as far as unloading goes: it stops listing a model a few polls after the unload."""

    kind = "ollama"

    def __init__(self, polls_until_gone=3):
        self.loaded = [{"name": f"{MODEL}:latest", "size": 11e9, "size_vram": 10.5 * 2**30}]
        self.unloaded, self.polls_until_gone, self.polls = [], polls_until_gone, 0

    def running(self):
        if self.unloaded:
            self.polls += 1
            if self.polls >= self.polls_until_gone:
                self.loaded = []
        return self.loaded

    def unload(self, model):
        self.unloaded.append(model)


def gpu_readings(monkeypatch, values):
    """nvidia-smi's memory in use, one value per call (the last one repeats)."""
    readings = list(values)
    calls = []

    def used():
        calls.append(readings[0] if len(readings) == 1 else readings.pop(0))
        return calls[-1]

    monkeypatch.setattr(hardware, "gpu_used_gib", used)
    return calls


def test_unloading_waits_until_the_memory_is_free(monkeypatch):
    calls = gpu_readings(monkeypatch, [11.0, 11.0, 11.0, 10.9, 0.6])
    ollama = Ollama()
    assert residency.release(ollama, MODEL, wait=5) is True
    assert ollama.unloaded == [MODEL] and ollama.loaded == [] and calls[-1] == 0.6
    assert residency.release(Ollama(), "another-model", wait=5) is False  # not loaded: nothing to do


def test_unloading_stops_waiting_when_the_memory_stops_going_down(monkeypatch):
    gpu_readings(monkeypatch, [11.0, 6.0])  # another program keeps part of it: no point waiting the whole time
    started = time.monotonic()
    assert residency.release(Ollama(polls_until_gone=1), MODEL, wait=10) is True
    assert 0.9 < time.monotonic() - started < 4


def test_a_job_waits_for_the_coding_model_still_loading(make_agent, monkeypatch):
    agent = make_agent()
    agent.ollama.loaded = []
    agent.load_state = "loading"
    threading.Timer(0.4, lambda: setattr(agent, "load_state", "")).start()
    started = time.monotonic()
    media.free_gpu(agent)
    assert time.monotonic() - started >= 0.35


def test_a_load_that_ends_after_leaving_code_mode_gives_the_gpu_back(make_agent):
    agent = make_agent()
    agent.ollama.loaded = []
    go = threading.Event()

    def slow_load(model, options, keep_alive):  # the 35B at 256K takes half a minute
        go.wait(5)
        agent.ollama.loaded = [{"name": f"{model}:latest", "size": 11e9, "size_vram": 11e9}]

    agent.ollama.load = slow_load
    preload(agent)
    assert agent.load_state == "loading"
    agent.settings.mode = "video"  # /mode video before it's loaded
    go.set()
    deadline = time.monotonic() + 5
    while agent.load_state and time.monotonic() < deadline:
        time.sleep(0.05)
    assert agent.ollama.unloaded == [MODEL]


class Process:
    """A ComfyUI lcode started: running, or ended with `code`."""

    def __init__(self, code=None):
        self.code, self.pid = code, 0

    def poll(self):
        return self.code


@pytest.fixture
def agent(make_agent):
    with StandIn() as server:
        agent = make_agent(comfyui_url=server.url)
        agent.settings.mode = "image"
        agent.interactive = False
        agent.standin = server
        yield agent


def test_running_out_of_gpu_memory_says_what_holds_it(agent, monkeypatch):
    stopped = []
    monkeypatch.setattr(comfy, "stop", stopped.append)
    agent.comfy_process = Process()
    monkeypatch.setattr(hardware, "gpu_users", lambda: [("Ollama", 10.5), ("render.py", 1.6), ("ComfyUI", 0.03)])
    agent.standin.fail = "Allocation on device 0 would exceed allowed memory. (out of memory)"
    with pytest.raises(media.MediaError) as e:
        media.generate_image(agent, "a fox")
    assert str(e.value) == (
        "The GPU ran out of memory (SamplerCustomAdvanced). Other programs hold GPU memory: Ollama (10.5 GB), "
        "render.py (1.6 GB). Close what you can, or try --quality fast or a smaller model (/models). "
        "lcode restarts ComfyUI with your next request."
    )
    assert stopped and agent.comfy_process is None  # started afresh next time: it can crash after running out


def test_a_crash_and_a_full_disk_are_said_plainly(agent, monkeypatch):
    agent.comfy_process = Process(code=-6)  # aborted
    message = media.explain(agent, "ComfyUI closed the websocket")
    assert message.startswith("ComfyUI stopped while it worked (it crashed; its log: ")
    assert message.endswith("lcode starts it again with your next request.") and agent.comfy_process is None
    assert media.explain(agent, "SaveImage failed: [Errno 28] No space left on device").startswith(
        "ComfyUI couldn't save what it made: the disk is full."
    )
    assert media.explain(agent, "KSampler failed: bad input") == "KSampler failed: bad input"
    vram = media.explain(agent, "CLIPTextEncode failed: VRAM grow failed: 777912320 bytes")  # ComfyUI 0.38's words
    assert vram.startswith("The GPU ran out of memory (CLIPTextEncode).")
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(media.shutil, "disk_usage", lambda path: Usage(500e9, 499.7e9, 0.3e9))
    with pytest.raises(media.MediaError, match=r"The disk is nearly full \(0\.3 GB free at "):
        media.generate_image(agent, "a fox")
    assert agent.standin.prompts == []  # refused before ComfyUI got it


def test_programs_on_the_gpu_have_short_names():
    comfy_python = "/home/u/comfy/ComfyUI/.venv/bin/python"
    assert hardware.name_from([comfy_python, "main.py", "--port", "8188"]) == "ComfyUI"
    assert hardware.name_from([comfy_python, "/tmp/hog.py", "11"]) == "hog.py"  # ComfyUI's Python, another script
    assert hardware.name_from(["python3", "-u", "render.py"]) == "render.py"
    assert hardware.name_from(["/usr/lib/ollama/llama-server", "--model", "x"]) == "Ollama"
    assert hardware.name_from(["/usr/bin/ffmpeg", "-i", "a.mp4"]) == "ffmpeg"
    assert hardware.program_name("999999999", "/usr/bin/python3") == "python3"  # gone: nvidia-smi's name
