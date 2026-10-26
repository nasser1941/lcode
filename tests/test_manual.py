"""Asking lcode how to do something: answers from its own manual, checked against its own commands and parsers."""

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from conftest import output
from lcode import manual, repl

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def book() -> manual.Manual:
    return manual.load()


def test_the_manual_is_the_docs_and_ships_inside_the_package(tmp_path, monkeypatch):
    assert manual.folder() == ROOT / "docs"  # a checkout: the docs themselves
    wheel = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["sources"]["docs"] == "lcode/docs" and "docs" in wheel["only-include"]
    installed = tmp_path / "site-packages" / "lcode"
    (installed / "docs").mkdir(parents=True)
    (installed / "docs" / "modes.md").write_text("# Modes\n\n## Video mode\n\nType a description of a clip.\n")
    monkeypatch.setattr(manual, "__file__", str(installed / "manual.py"))
    assert manual.folder() == installed / "docs"  # an installed package: its own copy, for its version
    manual._load.cache_clear()
    try:
        found = [p for p in manual.load().search("how do I make a clip?", "video") if p.page == "modes.md"]
        assert found[0].heading == "Modes › Video mode" and found[0].url.endswith("/modes/#video-mode")
    finally:
        manual._load.cache_clear()


@pytest.mark.parametrize(
    ("question", "mode", "needed"),
    [
        ("how to create a 25 seconds video with start and end reference image", "video", "@start.png @end.png"),
        ("how do I make the picture bigger?", "image", "/upscale"),
        ("how do I remove the background of a photo", "image", "/nobg"),
        ("how can I add music to my clip", "video", "/sound"),
        ("how do I make a short film with the same characters", "video", "/film"),
        ("how do I switch to video mode", "code", "/mode video"),
        ("how do I make the camera zoom in", "video", "--camera"),
        ("how do I continue a clip", "video", "/extend"),
        ("can I use grok to make videos?", "video", "grok-video"),
        ("where are my images saved?", "image", ".lcode/media"),
        ("how do I make 4 pictures at once in 16:9", "image", "--n 4"),
        ("how do I change the context window", "code", "/context"),
    ],
)
def test_the_search_finds_what_answers_everyday_questions(book, question, mode, needed):
    assert needed in book.excerpts(book.search(question, mode))  # what the answering model reads


def test_words_fold_forms_and_synonyms():
    assert manual.words("Pictures saved") == manual.words("image saves") == ["image", "save"]
    assert manual.words("making a clip from the first frame") == ["make", "video", "start", "fram"]
    assert manual.words("--seconds /film 16:9") == ["--seconds", "seconds", "/film", "film", "ratio"]


@pytest.mark.parametrize(
    "text",
    [
        "how do I make a 25 seconds video with start and end reference image",
        "How can I add music to a clip",
        "is there a way to make clips longer?",
        "what does /vary do?",
        "can lcode remove backgrounds",
        "where are my pictures?",
        "which command upscales a clip",
    ],
)
def test_questions_about_lcode_are_recognized(text):
    assert manual.is_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "a red fox in fresh snow --n 4",
        "what if cats ruled the world",
        "how the west was won, cinematic poster",
        "waves rolling onto a beach at sunset",
        "imagine a castle in the clouds?",
    ],
)
def test_prompts_are_not_questions(text):
    assert not manual.is_question(text)


def test_what_wouldnt_work_is_found_by_lcodes_own_parsers(book):
    answer = """```text
/mode video
@start.png @end.png the robot walks --seconds 25
/image a fox --n 4 --ar 16:9
/sound 4 waves --seconds 10
/film a fox --shots 3 --length 4
mode image
a fox --n 12
/vary 3 --n 2
◆ #1 .lcode/media/clip.mp4
lcode config set mode video
```
Or try /imagine."""
    assert manual.problems(answer, "code", book) == [
        "lcode has no /image",
        "lcode has no /imagine",
        "lcode has no --length",
        "@start.png @end.png the robot walks --seconds 25: seconds is how long a video is: 1 to 8",
        "/sound 4 waves --seconds 10: /sound takes no --options (--seconds would be read as words)",
        "/film a fox --shots 3 --length 4: /film has no --length (it has --shots, --seconds, --quality, "
        "--transition, --sound)",
        "mode image: a command starts with a slash (/mode image)",
        "a fox --n 12: n is how many images: 1 to 8",
    ]
    wrong_place = "```text\n/upscale start.png 4\n/mode video --model grok-video\n/animate 3 waves\n/sound 2 rain\n```"
    assert manual.problems(wrong_place, "image", book) == [
        "/upscale start.png 4: /upscale takes a result's number from the gallery (/upscale 3), not start.png",
        "/mode video --model grok-video: /mode takes one word: code, image or video",
        "/animate 3 waves: /animate works in image mode, not in video mode",
    ]
    last = "```text\n/edit make the sky purple\n/vary --n 2\n/animate the waves roll in\n/upscale\n```"
    assert manual.problems(last, "image", book) == []  # without a number: the last result
    assert manual.problems("```text\n/sound 2 rain\n```", "image", book) == [
        "/sound 2 rain: /sound works in video mode, not in image mode"
    ]
    good = "```text\n/mode video\n@start.png @end.png the flower opens --seconds 8 --camera zoom-in\n/extend 1\n```"
    assert manual.problems(good, "image", book) == []
    assert manual.suggestion(good, "video") == "@start.png @end.png the flower opens --seconds 8 --camera zoom-in"
    assert manual.suggestion(good, "image") == "/mode video"
    assert manual.suggestion("```\nlcode config set mode video\n```", "code") == ""  # a shell command
    assert manual.suggestion("```\n.lcode/media/\n```", "image") == ""  # a folder: not a picture's description


def test_answers_keep_only_what_to_type():
    text = "<think>hm</think>Do this:\n```text\nvideo ❯ /mode video\n◆ #1 .lcode/media/a.mp4\na fox --n 2\n```\nDone."
    assert manual.clean(text) == "Do this:\n```text\n/mode video\na fox --n 2\n```\nDone."


def test_the_basics_name_only_real_commands_and_options(book):
    named = [name for name in book.unknown(manual.BASICS)]
    assert named == ["/image", "/imagine", "/generate", "/video"]  # "there is no /image…": said on purpose


def test_an_answer_that_wouldnt_work_is_asked_for_again_then_marked(book):
    asked = []

    def chat(replies):
        def reply(messages):
            asked.append(messages)
            return replies.pop(0)

        return reply

    fixed = manual.answer(
        "how do I make a 25 second clip from two pictures?",
        "video",
        chat(["```text\n@a.png @b.png it opens --seconds 25\n```", "Clips are 1 to 8 s.\n```text\n/film opens\n```"]),
        "small",
        book,
    )
    assert fixed.text.startswith("Clips are 1 to 8 s.") and fixed.problems == [] and fixed.model == "small"
    assert "seconds is how long a video is: 1 to 8" in asked[1][-1]["content"]
    assert "Manual excerpts" in asked[0][0]["content"] and "Video mode" in asked[0][0]["content"]
    stuck = manual.answer("how do I upscale?", "image", chat(["```text\n/enlarge 3\n```"] * 2), "small", book)
    assert stuck.problems == ["lcode has no /enlarge"] and len(asked) == 4
    without = manual.answer("how do I upscale a picture?", "image", None, None, book)
    assert without.model is None and "/upscale" in without.text  # no model: the manual's own words


class Small:
    """A FakeOllama with the prompt assistant's models installed."""

    def __init__(self, ollama, replies):
        ollama.installed_names = lambda: {"qwen3.5:4b", "qwen3.5:9b", "lcode-qwen3.6-35b"}
        ollama.chat_replies = list(replies)


def test_a_question_in_video_mode_is_answered_and_the_first_step_typed_in(make_agent, monkeypatch):
    agent = make_agent()
    agent.settings.mode = "video"
    agent.interactive = False
    made = []
    monkeypatch.setattr(repl, "make_image", lambda agent, prompt="", **kw: made.append(prompt))
    Small(agent.ollama, ["Clips are 1 to 8 seconds.\n```text\n@start.png @end.png the flower opens --seconds 8\n```"])
    repl.respond(agent, "how do I make a clip from a start and an end picture?")
    assert made == [] and agent.ollama.chats[0]["model"] == "qwen3.5:9b"  # the larger small model answers
    assert agent.ollama.chats[0]["keep_alive"] == 0 and agent.ollama.chats[0]["options"]["num_ctx"] == 8192
    text = output(agent)
    assert "lcode help" in text and "Clips are 1 to 8 seconds." in text and "nasser1941.github.io/lcode/modes/" in text
    assert "add --raw" in text
    assert agent.unsent == ""  # not interactive: nothing to type into
    agent.interactive = True
    Small(agent.ollama, ["```text\n/upscale 3 4\n```"])
    repl.handle_command(agent, "/help how do I make a picture bigger", None)
    assert agent.unsent == "/upscale 3 4" and "The first step is in the prompt below" in output(agent)
    repl.respond(agent, "how do I make a clip longer --raw")  # a prompt that only looks like a question
    assert made == ["how do I make a clip longer --raw"]


def test_without_a_small_model_the_manual_answers(make_agent):
    agent = make_agent()
    agent.settings.mode = "image"
    agent.ollama.installed_names = lambda: {"lcode-qwen3.6-35b"}
    repl.handle_command(agent, "/help how do I remove the background", None)
    text = output(agent)
    assert "From lcode's manual" in text and "/nobg" in text and "ollama pull qwen3.5:4b" in text
    assert agent.ollama.chats == []


def test_code_mode_asks_the_coding_model_and_gives_it_the_manual_as_a_tool(make_agent):
    agent = make_agent()
    agent.ollama.chat_replies = ["```text\n/mode video\n```"]
    repl.handle_command(agent, "/help how do I switch to video mode", None)
    assert agent.ollama.chats[0]["model"] == "lcode-qwen3.6-35b"
    assert agent.ollama.chats[0]["options"]["num_ctx"] == 65536  # as loaded: no reload for an answer
    assert "/mode video" in output(agent)
    assert "lcode_help" in [s["function"]["name"] for s in agent.tool_schemas()]
    result = agent.tools.run("lcode_help", {"question": "how do I make a clip from two pictures"})
    assert result.startswith("How lcode") and "A clip is 1 to 8 seconds" in result and "@start.png @end.png" in result
    assert "https://nasser1941.github.io/lcode/modes/#video-mode" in result


def test_a_model_that_fails_leaves_the_manuals_words(make_agent):
    agent = make_agent()

    def broken(payload, timeout=900):
        raise RuntimeError("connection refused")

    agent.ollama.chat = broken
    repl.handle_command(agent, "/help how do I undo the last change", None)
    text = output(agent)
    assert "didn't answer (connection refused)" in text and "From lcode's manual" in text and "/undo" in text
