"""Image mode's requests: options (count, size, seed, quality, style), numbered results, previews, follow-ups."""

import json
import struct
import zlib

import pytest

from comfy_standin import StandIn
from conftest import output
from lcode import cli, config, imageopts, media, preview
from lcode.hardware import Hardware
from lcode.repl import handle_command, respond

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)


def text(agent):
    return " ".join(output(agent).split())


@pytest.fixture
def standin():
    with StandIn() as server:
        yield server


@pytest.fixture
def image_agent(make_agent, standin):
    agent = make_agent(comfyui_url=standin.url)
    agent.settings.mode = "image"
    agent.interactive = False
    return agent


# ----------------------------------------------------------------------------- options


def test_flags_in_a_request():
    base = imageopts.Options()
    prompt, o = imageopts.parse("a red fox --n 4 --ar 16:9 --seed 42 --quality best --style photo", base)
    assert prompt == "a red fox" and (o.n, o.size, o.seed, o.quality, o.style) == (4, "16:9", 42, "best", "photo")
    prompt, o = imageopts.parse('a fox --no "people, text" --size wide', base)
    assert prompt == "a fox" and o.negative == "people, text" and o.size == "wide"
    prompt, o = imageopts.parse("--n 2 a dog on a beach", base)
    assert prompt == "a dog on a beach" and o.n == 2
    assert imageopts.parse("a well-known landmark", base) == ("a well-known landmark", base)
    bad_ones = [
        ("x --n 20", "1 to 8"),
        ("x --ar 1:9", "ratio"),
        ("x --seed abc", "whole number"),
        ("x --quality ultra", "fast, balanced, best"),
        ("x --style gothic", "photo"),
        ("x --n", "needs"),
    ]
    for bad, why in bad_ones:
        with pytest.raises(imageopts.OptionError, match=why):
            imageopts.parse(bad, base)


def test_sizes_become_pixels():
    assert imageopts.dimensions("1:1", 1.0) == (1024, 1024)
    assert imageopts.dimensions("16:9", 1.0) == (1360, 768)
    assert imageopts.dimensions("portrait", 0.5) == (624, 832)
    assert imageopts.dimensions("1344x768", 2.0) == (1344, 768)  # pixels stay as given
    w, h = imageopts.dimensions("21:9", 2.0)
    assert w % 16 == 0 and h % 16 == 0 and abs(w * h / (2 * 1024 * 1024) - 1) < 0.05


def test_session_options_and_what_survives_a_save():
    o = imageopts.set_values(imageopts.Options(), imageopts.parse_assignments('n=3 ar=3:2 seed=7 no="blur, text"'))
    assert (o.n, o.size, o.seed, o.negative) == (3, "3:2", 7, "blur, text")
    assert imageopts.Options.from_json(o.to_json()) == o
    assert imageopts.Options.from_json({"n": 99, "junk": 1}) == imageopts.Options()  # a bad saved value: defaults
    with pytest.raises(imageopts.OptionError, match="name=value"):
        imageopts.parse_assignments("n 4")


# ----------------------------------------------------------------------------- previews


def png(width: int, height: int, color: int, rows: list[bytes], palette: bytes = b"") -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    extra = chunk(b"PLTE", palette) if palette else b""
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + extra + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )  # fmt: skip


def filtered(kind: int, line: bytes, previous: bytes, bpp: int) -> bytes:
    """Encode one scanline with a PNG filter, to check the decoder undoes each of them."""
    out = bytearray()
    for x, value in enumerate(line):
        left = line[x - bpp] if x >= bpp else 0
        up = previous[x]
        corner = previous[x - bpp] if x >= bpp else 0
        if kind == 0:
            predictor = 0
        elif kind == 1:
            predictor = left
        elif kind == 2:
            predictor = up
        elif kind == 3:
            predictor = (left + up) // 2
        else:
            p = left + up - corner
            pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
            predictor = left if pa <= pb and pa <= pc else up if pb <= pc else corner
        out.append((value - predictor) & 0xFF)
    return bytes([kind]) + bytes(out)


def test_the_png_decoder_undoes_every_filter():
    pixels = [bytes((x * 40 + y * 7) % 256 for x in range(4) for _ in range(3)) for y in range(5)]
    rows, previous = [], bytes(12)
    for y, line in enumerate(pixels):
        rows.append(filtered(y % 5, line, previous, 3))
        previous = line
    width, height, decoded = preview.decode_png(png(4, 5, 2, rows))
    assert (width, height) == (4, 5)
    assert [bytes(c for px in row for c in px) for row in decoded] == pixels
    grey = png(2, 1, 0, [b"\x00\x10\xf0"])
    assert preview.decode_png(grey)[2] == [[(16, 16, 16), (240, 240, 240)]]
    pal = png(2, 1, 3, [b"\x00\x00\x01"], palette=b"\xff\x00\x00\x00\x00\xff")
    assert preview.decode_png(pal)[2] == [[(255, 0, 0), (0, 0, 255)]]
    rgba = png(1, 1, 6, [b"\x00\x01\x02\x03\x04"])
    assert preview.decode_png(rgba)[2] == [[(1, 2, 3)]]
    with pytest.raises(preview.PngError):
        preview.decode_png(b"GIF89a")


def test_half_blocks_and_image_protocols():
    rows = [b"\x00" + bytes([255, 0, 0] * 4), b"\x00" + bytes([0, 0, 255] * 4)]
    text = preview.blocks(png(4, 2, 2, rows), columns=4)
    assert text.plain == "▀▀▀▀" and "#ff0000 on #0000ff" in str(text.spans[0].style)
    seq = preview.kitty(b"x" * 5000)
    assert seq.startswith("\x1b_Ga=T,f=100,c=40,q=2,m=1;") and seq.count("\x1b_G") == 2 and "\x1b_Gm=0;" in seq
    assert preview.iterm(b"abc").startswith("\x1b]1337;File=inline=1;size=3;width=40;preserveAspectRatio=1:YWJj")
    assert preview.protocol("auto", {"TERM": "xterm-kitty"}) == "kitty"
    assert preview.protocol("auto", {"TERM_PROGRAM": "ghostty"}) == "kitty"
    assert preview.protocol("auto", {"TERM_PROGRAM": "WezTerm"}) == "iterm"
    assert preview.protocol("auto", {"TERM_PROGRAM": "iTerm.app", "TMUX": "/tmp/t"}) == "blocks"
    assert preview.protocol("auto", {"TERM": "xterm-256color"}) == "blocks"
    assert preview.protocol("off", {"TERM": "xterm-kitty"}) == "off"


# ----------------------------------------------------------------------------- requests


def test_a_request_with_flags_makes_numbered_images(image_agent, standin):
    made = media.request(image_agent, "a red fox --n 3 --seed 7 --ar 16:9 --quality fast --style watercolor")
    assert [m.seed for m in made] == [7, 8, 9] and [m.numbers for m in made] == [[1], [2], [3]]
    graph = standin.prompts[0]
    assert graph["4"]["inputs"]["text"].startswith("a red fox, watercolor painting")
    assert (graph["9"]["inputs"]["width"], graph["9"]["inputs"]["height"]) == (960, 544)  # 16:9 at 0.5 MP
    assert graph["9002"]["class_type"] == "PreviewImage" and graph["9001"]["inputs"]["width"] == 64
    listing = text(image_agent)
    assert "#1 .lcode/media/" in listing and "#3 .lcode/media/" in listing and "(3 of 3)" not in listing
    sidecar = json.loads(made[0].files[0].with_suffix(".json").read_text())
    assert sidecar["quality"] == "fast" and sidecar["style"] == "watercolor" and sidecar["prompt"] == "a red fox"
    assert made[0].thumbnail.startswith(b"\x89PNG")


def test_a_negative_prompt_with_a_model_that_has_none(image_agent):
    media.request(image_agent, "a quiet street --no cars")
    assert "FLUX.2 klein 4B doesn't use a negative prompt" in text(image_agent)


def test_the_preview_shows_where_the_terminal_can(image_agent, monkeypatch):
    image_agent.settings.preview = "blocks"
    media.request(image_agent, "a red fox")
    assert "▀" in output(image_agent)
    image_agent.settings.preview = "kitty"
    written = []
    monkeypatch.setattr(image_agent.console.file, "write", lambda s: written.append(s) or len(s))
    media.request(image_agent, "a red fox")
    assert any(s.startswith("\x1b_Ga=T,f=100") for s in written)


def test_session_options_follow_ups_and_the_gallery(image_agent, standin, monkeypatch):
    handle_command(image_agent, "/options n=2 ar=3:4 style=anime", LAPTOP)
    assert "n=2 size=3:4 seed=random quality=balanced style=anime" in text(image_agent)
    respond(image_agent, "a robot reading a book")
    assert len(image_agent.media_results) == 2
    respond(image_agent, "a robot reading a book --n 1")  # a flag wins over /options for one request
    assert len(image_agent.media_results) == 3
    handle_command(image_agent, "/again --n 2", LAPTOP)
    assert len(image_agent.media_results) == 5
    assert image_agent.media_results[3]["seed"] != image_agent.media_results[2]["seed"]
    handle_command(image_agent, "/vary 1", LAPTOP)  # four variations of #1
    assert len(image_agent.media_results) == 9 and "Variations of #1: a robot reading a book" in text(image_agent)
    assert all(r["prompt"] == "a robot reading a book" for r in image_agent.media_results)
    handle_command(image_agent, "/vary 42", LAPTOP)
    assert "There's no image #42: this session has #1 to #9" in text(image_agent)
    handle_command(image_agent, "/gallery", LAPTOP)
    assert "#9" in text(image_agent) and "a robot reading a book · 880×1184" in text(image_agent)
    handle_command(image_agent, "/gallery all", LAPTOP)
    assert "9 latest image(s) in .lcode/media" in text(image_agent)
    opened = []
    monkeypatch.setattr(media, "open_file", lambda path: opened.append(path) or "")
    handle_command(image_agent, "/open 2", LAPTOP)
    assert [str(p) for p in opened] == [image_agent.media_results[1]["path"]]
    assert "Opened #2" in text(image_agent)


def test_the_gallery_and_options_come_back_with_the_session(image_agent, make_agent, standin):
    handle_command(image_agent, "/options quality=fast", LAPTOP)
    respond(image_agent, "a lighthouse in a storm")
    from lcode import sessions

    other = make_agent(comfyui_url=standin.url)
    other.load(sessions.list_sessions(image_agent.workspace)[0])
    assert other.media_results[0]["prompt"] == "a lighthouse in a storm" and other.image_options.quality == "fast"
    assert sessions.list_sessions(image_agent.workspace)[0].title == "a lighthouse in a storm"


def test_nothing_to_repeat_yet(image_agent):
    handle_command(image_agent, "/again", LAPTOP)
    handle_command(image_agent, "/open", LAPTOP)
    handle_command(image_agent, "/gallery", LAPTOP)
    assert "Nothing to do again yet" in text(image_agent) and "No images in this session yet" in text(image_agent)


def test_several_images_from_the_command_line(tmp_path, monkeypatch, capsys, standin, repo):
    from conftest import FakeOllama
    from lcode import catalog

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    cli.main(["config", "set", "comfyui_url", standin.url])
    monkeypatch.setattr(cli, "Ollama", lambda host: FakeOllama())
    monkeypatch.setattr(cli, "detect", lambda: LAPTOP)
    monkeypatch.setattr("lcode.api.check_server", lambda *args: "0.35.1")
    monkeypatch.setattr(cli, "resolve_model", lambda ollama, name: (name, catalog.find(name)))
    monkeypatch.setattr(cli, "choose_context", lambda *args: (32768, ""))
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit) as done:
        cli.main(["--mode", "image", "-p", "a red fox --n 2 --seed 100", "--output", "json", "--no-mcp"])
    assert done.value.code == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert len(result["files"]) == 2 and result["seeds"] == [100, 101]


def test_the_preview_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    assert config.load()["preview"] == "auto"
    with pytest.raises(SystemExit):
        cli.main(["config", "set", "preview", "sixel"])
    cli.main(["config", "set", "preview", "blocks"])
    assert config.load()["preview"] == "blocks"
