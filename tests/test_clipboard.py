import os
import stat

import pytest

from lcode import clipboard, config, repl

PNG = b"\x89PNG\r\n\x1a\nfake"


def tool(bin_dir, name, script):
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """No clipboard tools and no desktop session, until a test adds them."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("sh", "cat", "printf"):
        os.symlink(f"/bin/{name}" if os.path.exists(f"/bin/{name}") else f"/usr/bin/{name}", bin_dir / name)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    (tmp_path / "shot.png").write_bytes(PNG)
    return bin_dir


def test_an_image_on_wayland_is_saved(desktop, tmp_path, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    tool(
        desktop,
        "wl-paste",
        f'if [ "$1" = "--list-types" ]; then printf "text/plain\\nimage/png\\n"; '
        f'elif [ "$2" = "--type" ] && [ "$3" = "image/png" ]; then cat {tmp_path / "shot.png"}; '
        "else exit 1; fi\n",
    )
    got = clipboard.paste()
    assert got.image is not None and got.image.read_bytes() == PNG
    assert got.image.parent == tmp_path / "state" / "pasted" and got.image.suffix == ".png"
    assert clipboard.paste().image != got.image  # a second paste doesn't overwrite the first


def test_text_on_x11_is_pasted_as_text(desktop, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    tool(
        desktop,
        "xclip",
        'if [ "$4" = "TARGETS" ]; then printf "UTF8_STRING\\nTARGETS\\n"; else printf "hello world"; fi\n',
    )
    got = clipboard.paste()
    assert got.image is None and got.text == "hello world"
    assert clipboard.status() == ("Ctrl+V pastes images (with xclip)", True)


def test_missing_tools_say_what_to_install(desktop, monkeypatch):
    assert "no clipboard here" in clipboard.paste().problem
    monkeypatch.setenv("DISPLAY", ":0")
    assert "install xclip" in clipboard.paste().problem
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert "install wl-clipboard" in clipboard.paste().problem and clipboard.status()[1] is None
    tool(desktop, "wl-paste", "exit 1\n")
    assert clipboard.paste().problem == "The clipboard is empty."


def test_an_image_on_a_mac(desktop, tmp_path, monkeypatch):
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    tool(desktop, "pngpaste", f'[ "$1" = "-" ] && cat {tmp_path / "shot.png"}\n')
    assert clipboard.paste().image.read_bytes() == PNG


def test_a_mac_without_pngpaste_asks_osascript(desktop, tmp_path, monkeypatch):
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    target = tmp_path / "state" / "pasted" / ".clipboard.png"
    tool(desktop, "osascript", f"cat {tmp_path / 'shot.png'} > {target}\n")
    got = clipboard.paste()
    assert got.image.read_bytes() == PNG and not target.exists()
    tool(desktop, "osascript", "exit 0\n")
    tool(desktop, "pbpaste", "printf 'some text'\n")
    assert clipboard.paste().text == "some text"


def test_old_pastes_are_cleaned_up(desktop, monkeypatch):
    monkeypatch.setattr(clipboard, "KEEP", 3)
    paths = [clipboard.save(PNG, ".png") for _ in range(5)]
    left = sorted((config.STATE_DIR / "pasted").glob("pasted-*"))
    assert len(left) == 3 and paths[-1] in left and paths[0] not in left


def test_a_pasted_image_goes_into_the_prompt_as_a_mention(tmp_path, monkeypatch):
    monkeypatch.setattr(repl.Path, "home", lambda: tmp_path)
    assert repl.mention(tmp_path / "state" / "pasted-1.png") == "@~/state/pasted-1.png"
    assert repl.mention(tmp_path / "My Shots" / "a.png") == '@"~/My Shots/a.png"'
    assert repl.mention(tmp_path.parent / "x.png") == f"@{tmp_path.parent / 'x.png'}"


def test_ctrl_v_puts_the_image_or_text_in_the_prompt(make_agent, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.keys import Keys

    monkeypatch.setattr(repl, "STATE_DIR", tmp_path)
    session = repl.build_session(make_agent())
    (binding,) = session.key_bindings.get_bindings_for_keys((Keys.ControlV,))
    buffer = Buffer(multiline=True)
    buffer.insert_text("why is this broken?")
    shot = tmp_path / "pasted-1.png"
    monkeypatch.setattr(repl.clipboard, "paste", lambda: repl.clipboard.Paste(image=shot))
    binding.handler(SimpleNamespace(current_buffer=buffer))
    assert buffer.text == f"why is this broken? {repl.mention(shot)} "
    monkeypatch.setattr(repl.clipboard, "paste", lambda: repl.clipboard.Paste(text="line 1\r\nline 2"))
    binding.handler(SimpleNamespace(current_buffer=buffer))
    assert buffer.text.endswith(" line 1\nline 2")
