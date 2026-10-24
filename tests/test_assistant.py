"""The prompt assistant: fuller prompts, follow-ups read as changes, results checked, all against a fake Ollama."""

import json

import pytest

from comfy_standin import FLUX, StandIn, tiny_png
from conftest import output
from lcode import assistant, media

WAN = {
    "diffusion_models": [*FLUX["diffusion_models"], "wan2.2_ti2v_5B_fp16.safetensors"],
    "text_encoders": [*FLUX["text_encoders"], "umt5_xxl_fp8_e4m3fn_scaled.safetensors"],
    "vae": [*FLUX["vae"], "wan2.2_vae.safetensors"],
}


def text(agent):
    return " ".join(output(agent).split())


@pytest.fixture
def standin():
    with StandIn(models=WAN) as server:
        yield server


@pytest.fixture
def agent(make_agent, standin, repo, monkeypatch):
    agent = make_agent(comfyui_url=standin.url, prompt_assistant="all")
    agent.settings.mode = "image"
    agent.interactive = False
    monkeypatch.setattr(agent.ollama, "installed_names", lambda: {"qwen3.5:4b", "lcode-qwen3.6-35b"})
    (repo / "photo.png").write_bytes(tiny_png())
    return agent


def prompt_sent(standin):
    return next(n["inputs"]["text"] for n in standin.prompts[-1].values() if n["class_type"] == "CLIPTextEncode")


def test_which_model_helps():
    class Ollama:
        def installed_names(self):
            return {"qwen3.5:9b", "qwen3.5:4b"}

    assert assistant.pick(Ollama(), "video") == "qwen3.5:4b"  # the smallest that does the job
    assert assistant.pick(Ollama(), "all") == assistant.pick(Ollama(), "auto") == "qwen3.5:4b"
    assert assistant.pick(Ollama(), "qwen3.5:9b") == "qwen3.5:9b"
    assert assistant.pick(Ollama(), "llama9:70b") is None and assistant.pick(Ollama(), "off") is None


def test_a_short_request_becomes_a_fuller_prompt(agent, standin):
    agent.ollama.chat_replies = ['Prompt: "A red fox sitting in fresh snow at dusk, soft blue light, photo."']
    media.request(agent, "fox snow")
    assert prompt_sent(standin) == "A red fox sitting in fresh snow at dusk, soft blue light, photo."
    chat = agent.ollama.chats[0]
    assert chat["model"] == "qwen3.5:4b" and chat["keep_alive"] == 0 and chat["think"] is False
    assert "image generation model" in chat["messages"][0]["content"]
    assert "✎ A red fox sitting in fresh snow" in text(agent)
    entry = agent.media_results[-1]
    assert entry["prompt"] == "fox snow"  # the gallery keeps what you typed
    sidecar = json.loads(media.Path(entry["path"]).with_suffix(".json").read_text())
    assert sidecar["full_prompt"].startswith("A red fox sitting")


def test_raw_and_off_send_the_prompt_as_typed(agent, standin):
    media.request(agent, "fox snow --raw")
    assert prompt_sent(standin) == "fox snow" and agent.ollama.chats == []
    agent.settings.prompt_assistant = "off"
    agent._assistant_model = None
    media.request(agent, "a cat")
    assert prompt_sent(standin) == "a cat" and agent.ollama.chats == []


def test_a_video_prompt_looks_at_its_start_picture(agent, standin):
    agent.settings.mode = "video"
    agent.ollama.chat_replies = ["A lighthouse on a cliff in a storm; waves crash below as the shot slowly zooms in."]
    media.request(agent, "@photo.png waves crash")
    chat = agent.ollama.chats[0]
    assert (
        "video generation model" in chat["messages"][0]["content"]
        and "starts from the attached picture" in chat["messages"][0]["content"]
    )
    assert chat["messages"][1]["images"]  # it sees the picture
    assert prompt_sent(standin).startswith("A lighthouse on a cliff")
    entry = agent.media_results[-1]
    assert entry["prompt"] == "waves crash" and entry["path"].endswith("-waves-crash.mp4")  # what you typed


def test_a_follow_up_changes_the_last_picture(agent, standin):
    agent.ollama.chat_replies = [
        "A red fox in the snow, photo.",
        '{"change": true, "instruction": "Make the whole picture darker, like late dusk."}',
        '{"change": false, "instruction": ""}',
        "A lighthouse in a storm, dramatic light.",
    ]
    media.request(agent, "a red fox in the snow")
    media.request(agent, "darker")
    assert agent.media_results[-1]["kind"] == "edit" and "A change to #1" in text(agent)
    assert prompt_sent(standin) == "Make the whole picture darker, like late dusk."
    media.request(agent, "a lighthouse in a storm")  # a new picture: asked, and told so
    assert agent.media_results[-1]["kind"] == "image" and len(agent.ollama.chats) == 4


def test_results_are_checked_when_asked(agent, standin):
    agent.settings.check_results = True
    agent.ollama.chat_replies = [
        "A red fox in deep snow, photo.",
        '{"matches": false, "missing": "There is no snow.", "better_prompt": "a red fox standing in deep snow"}',
        '{"change": false}',  # a short request after a picture: is it a change to it? no
        "A red fox in deep snow, photo.",
        '{"matches": true}',
    ]
    media.request(agent, "a fox in snow")
    assert "⚠ There is no snow. Try: a red fox standing in deep snow" in text(agent)
    assert agent.ollama.chats[1]["messages"][1]["images"] and agent.ollama.chats[1]["format"] == "json"
    media.request(agent, "a fox in snow --n 1")
    assert "✓ It shows what you asked for" in text(agent)


def test_when_the_assistant_fails_the_prompt_goes_as_typed(agent, standin, monkeypatch):
    def broken(payload, timeout=900):
        raise ConnectionError("Ollama is gone")

    monkeypatch.setattr(agent.ollama, "chat", broken)
    media.request(agent, "a fox")
    assert prompt_sent(standin) == "a fox" and "the prompt goes as typed" in text(agent)


def test_an_assistant_that_isnt_installed_is_named(agent, standin):
    agent.settings.prompt_assistant = "llama9:70b"
    media.request(agent, "a fox")
    assert "ollama pull llama9:70b" in text(agent) and prompt_sent(standin) == "a fox"


def test_cleaning_what_the_model_wrote():
    assert assistant.clean('Here is the prompt: "A fox."') == "A fox."
    assert assistant.clean("<think>hmm</think>**Prompt:** A fox in snow") == "A fox in snow"
    assert assistant.parse_json('Sure! {"change": true, "instruction": "x"} done') == {
        "change": True,
        "instruction": "x",
    }
    assert assistant.parse_json("no json") == {}


def test_by_default_only_video_prompts_are_written(agent, standin):
    agent.settings.prompt_assistant = "video"
    media.request(agent, "fox snow")
    media.request(agent, "darker")  # no follow-up reading either
    assert prompt_sent(standin) == "darker" and agent.ollama.chats == []
    agent.settings.mode = "video"
    agent.ollama.chat_replies = ["A red fox trots through deep snow as the shot tracks beside it."]
    media.request(agent, "fox snow")
    assert prompt_sent(standin).startswith("A red fox trots") and len(agent.ollama.chats) == 1


def test_checking_results_works_without_prompts(agent, standin):
    agent.settings.prompt_assistant = "off"
    agent.settings.check_results = True
    agent.ollama.chat_replies = ['{"matches": true}']
    media.request(agent, "a fox")
    assert prompt_sent(standin) == "a fox" and "✓ It shows what you asked for" in text(agent)
