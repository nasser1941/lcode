import base64

import pytest

from conftest import FakeOllama, call, output, reply
from lcode import vision
from lcode.mcp.protocol import result_text

PNG = base64.b64decode(  # a 1x1 transparent PNG
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def seeing(agent, *models):
    for model in models:
        agent.ollama.capabilities[model] = ["completion", "vision", "tools"]
    agent._vision = False  # decide again
    return agent


def test_which_model_looks():
    ollama = FakeOllama()
    assert vision.pick_model(ollama, "lcode-qwen3.6-35b", "auto") is None
    ollama.capabilities["qwen3.6:35b-a3b-coding"] = ["vision"]
    assert vision.pick_model(ollama, "lcode-qwen3.6-35b", "auto") == "qwen3.6:35b-a3b-coding"  # the base model
    ollama.capabilities["qwen3.5:9b"] = ["vision"]
    assert vision.pick_model(ollama, "qwen3.5:9b", "auto") == "qwen3.5:9b"  # the model itself
    assert vision.pick_model(ollama, "qwen3.5:9b", "off") is None
    assert vision.pick_model(ollama, "custom:7b", "qwen3-vl:8b") == "qwen3-vl:8b"


def test_images_are_checked(tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(PNG)
    (tmp_path / "notes.txt").write_text("x")
    assert vision.read_image(tmp_path / "a.png") == PNG
    with pytest.raises(vision.VisionError, match="isn't an image"):
        vision.read_image(tmp_path / "notes.txt")
    with pytest.raises(vision.VisionError, match="doesn't exist"):
        vision.read_image(tmp_path / "missing.png")
    monkeypatch.setattr(vision, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(vision.VisionError, match="up to 20 MB"):
        vision.read_image(tmp_path / "a.png")


def test_the_model_can_look_at_screenshots(make_agent, repo):
    (repo / "screenshot.png").write_bytes(PNG)
    agent = make_agent(
        [reply(tool_calls=[call("view_image", path="screenshot.png", question="what does the button say?")]),
         reply("The button says 'Sign in'.")]
    )  # fmt: skip
    assert "view_image" not in {s["function"]["name"] for s in agent.tool_schemas()}  # nothing can see yet
    seeing(agent, "qwen3.6:35b-a3b-coding")
    assert "view_image" in {s["function"]["name"] for s in agent.tool_schemas()}
    agent.run_turn("check the login screenshot")
    result = next(m["content"] for m in agent.messages if m.get("tool_name") == "view_image")
    assert result.startswith("[screenshot.png, as described by qwen3.6:35b-a3b-coding]") and "red 'Sign in'" in result
    request = agent.ollama.chats[0]
    assert request["model"] == "qwen3.6:35b-a3b-coding" and request["think"] is False
    assert request["messages"][0]["images"] == [base64.b64encode(PNG).decode()]
    assert "what does the button say?" in request["messages"][0]["content"]
    assert request["options"] == {"num_ctx": vision.DESCRIBE_CONTEXT}


def test_a_model_that_sees_keeps_its_settings(make_agent, repo):
    (repo / "a.png").write_bytes(PNG)
    agent = seeing(make_agent(model="qwen3.5:9b", context=65536, num_batch=512), "qwen3.5:9b")
    agent.look(repo / "a.png")
    assert agent.ollama.chats[0]["options"] == {"num_ctx": 65536, "num_batch": 512}  # no reload


def test_attached_images_are_described_with_the_question(make_agent, repo):
    (repo / "bug.png").write_bytes(PNG)
    agent = seeing(make_agent([reply("Fixed.")]), "qwen3.6:35b-a3b-coding")
    agent.run_turn("why is the layout broken in @bug.png ?")
    sent = agent.messages[1]["content"]
    assert '<image path="bug.png" described_by="qwen3.6:35b-a3b-coding">' in sent and "red 'Sign in'" in sent
    assert "why is the layout broken in  ?" in agent.ollama.chats[0]["messages"][0]["content"]
    assert "looked at bug.png" in output(agent)


def test_without_a_vision_model_lcode_says_how_to_get_one(make_agent, repo):
    (repo / "bug.png").write_bytes(PNG)
    agent = make_agent([reply("ok")])
    agent.run_turn("look at @bug.png")
    assert "ollama pull qwen3-vl:8b" in agent.messages[1]["content"]
    assert agent.tools.run("read_file", {"path": "bug.png"}) == "Error: bug.png is an image; look at it with view_image"


def test_images_from_mcp_tools_are_described(make_agent):
    agent = seeing(make_agent(), "qwen3.6:35b-a3b-coding")
    image = {"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()}
    shot = {"content": [{"type": "text", "text": "Took a screenshot"}, image]}
    text = result_text(shot, agent.describe_image_data)
    assert text.startswith("Took a screenshot\n[image/png image, as described by qwen3.6:35b-a3b-coding]")
    assert "red 'Sign in'" in text
    blind = make_agent()
    assert "not shown" in result_text(shot, blind.describe_image_data)


def test_view_image_respects_the_sandbox(make_agent, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "x.png"
    outside.write_bytes(PNG)
    agent = seeing(make_agent(sandbox="docker"), "qwen3.6:35b-a3b-coding")
    assert agent.tools.run("view_image", {"path": str(outside)}).startswith("Error:")
