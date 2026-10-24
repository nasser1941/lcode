"""Image mode's edits: by instruction with references, inpainting through a mask, outpainting, upscaling and
removing the background; as workflow changes, and through the REPL against a stand-in ComfyUI."""

import json
import struct

import pytest

from comfy_standin import FLUX, StandIn, tiny_png
from conftest import output
from lcode import comfy, edits, media
from lcode.hardware import Hardware
from lcode.repl import handle_command, respond

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
TOOLS = {
    **FLUX,
    "upscale_models": ["RealESRGAN_x4plus.safetensors"],
    "background_removal": ["birefnet.safetensors"],
}


def text(agent):
    return " ".join(output(agent).split())


def flux_graph() -> dict:
    files = {"model": "m", "text_encoder": "t", "vae": "v"}
    values = {"width": 1024, "height": 1024, "steps": 4, "batch": 1, "seed": 1, "prompt": "make it night", **files}
    return comfy.fill(comfy.load_workflow("flux2-klein-4b-t2i").graph, values)


@pytest.fixture
def standin():
    with StandIn(models=TOOLS) as server:
        yield server


@pytest.fixture
def image_agent(make_agent, standin, repo):
    agent = make_agent(comfyui_url=standin.url)
    agent.settings.mode = "image"
    agent.interactive = False
    (repo / "photo.png").write_bytes(tiny_png())
    (repo / "style.png").write_bytes(tiny_png())
    return agent


# ----------------------------------------------------------------------------- the workflow changes


def test_references_go_to_both_conditionings_and_set_the_size():
    graph = flux_graph()
    links = edits.load_images(graph, ["photo.png", "style.png"])
    first = edits.add_references(graph, links, 1.0, size_from_first=True)
    guider = graph["6"]["inputs"]
    pos, neg = graph[guider["positive"][0]], graph[guider["negative"][0]]
    assert pos["class_type"] == neg["class_type"] == "ReferenceLatent"
    inner = graph[pos["inputs"]["conditioning"][0]]
    assert inner["class_type"] == "ReferenceLatent" and inner["inputs"]["conditioning"] == ["4", 0]  # two, chained
    assert graph[first[0]]["class_type"] == "ImageScaleToTotalPixels"
    for node in ("8", "9"):  # the scheduler and the latent take the first image's size
        assert graph[node]["inputs"]["width"] == [edits.SIZE_NODE, 0]
        assert graph[node]["inputs"]["height"] == [edits.SIZE_NODE, 1]


def test_inpainting_samples_inside_the_mask_and_pastes_into_the_original():
    graph = flux_graph()
    first = edits.add_references(graph, edits.load_images(graph, ["photo.png"]), 1.0, size_from_first=True)
    edits.inpaint(graph, first, "mask.png")
    latent = graph[graph["11"]["inputs"]["latent_image"][0]]
    assert latent["class_type"] == "SetLatentNoiseMask"
    paste = graph[graph["13"]["inputs"]["images"][0]]
    assert paste["class_type"] == "ImageCompositeMasked" and paste["inputs"]["destination"] == first
    assert paste["inputs"]["source"] == ["12", 0]  # the decoded edit, only where the mask is white


def test_outpainting_paints_only_the_border():
    graph = flux_graph()
    image = edits.load_images(graph, ["photo.png"])[0]
    canvas, border = edits.pad_canvas(graph, image, (128, 0, 128, 0))
    assert graph[canvas[0]]["class_type"] == "ImageCompositeMasked"  # the picture on a blurred, stretched copy
    first = edits.add_references(graph, [canvas], 1.0, size_from_first=True)
    edits.outpaint(graph, first, border, 1.0)
    paste = graph[graph["13"]["inputs"]["images"][0]]
    assert paste["inputs"]["source"] == first and paste["inputs"]["destination"] == ["12", 0]
    assert graph[paste["inputs"]["mask"][0]]["class_type"] == "InvertMask"  # the original part comes back as it was


def test_a_workflow_without_a_guider_cant_take_references():
    graph = comfy.load_workflow("upscale-esrgan").graph
    with pytest.raises(edits.EditError, match="can't take reference images"):
        edits.add_references(graph, [["1", 0]], 1.0, size_from_first=True)


# ----------------------------------------------------------------------------- sizes and arguments


def test_image_sizes_from_headers(tmp_path):
    (tmp_path / "a.png").write_bytes(tiny_png())
    assert edits.image_size(tmp_path / "a.png") == (1, 1)
    jpeg = b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9
    jpeg += b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 600, 800) + b"\0" * 10
    (tmp_path / "b.jpg").write_bytes(jpeg)
    assert edits.image_size(tmp_path / "b.jpg") == (800, 600)
    webp = b"RIFF" + b"\0" * 4 + b"WEBPVP8X" + b"\0" * 8 + (1919).to_bytes(3, "little") + (1079).to_bytes(3, "little")
    (tmp_path / "c.webp").write_bytes(webp)
    assert edits.image_size(tmp_path / "c.webp") == (1920, 1080)
    (tmp_path / "d.gif").write_bytes(b"GIF89a")
    with pytest.raises(edits.EditError, match="PNG, JPEG or WebP"):
        edits.image_size(tmp_path / "d.gif")


def test_extending_to_a_shape_or_by_sides():
    left, top, right, bottom = edits.pads_for_ratio(1024, 1024, "16:9")
    assert top == bottom == 0 and left % 8 == 0 and right % 8 == 0 and left + right >= 1820 - 1024
    assert edits.pads_for_ratio(1024, 1024, "9:16")[0] == 0
    with pytest.raises(edits.EditError, match="already"):
        edits.pads_for_ratio(1920, 1080, "16:9")
    assert edits.parse_extend("--left 250 a calm sea", 1024, 1024) == ((256, 0, 0, 0), "a calm sea")
    assert edits.parse_extend("--ar wide", 1024, 1024)[0][0] > 0
    with pytest.raises(edits.EditError, match="not both"):
        edits.parse_extend("--ar 16:9 --left 64", 1024, 1024)


def test_references_in_a_request():
    assert edits.split_references('@photo.png @#3 @"my style.png" make it night') == (
        "make it night",
        ["photo.png", "#3", "my style.png"],
    )
    assert edits.split_references("email me at a@b.com") == ("email me at a@b.com", [])


def test_comfyuis_reasons_for_refusing_are_kept_whole():
    class Refused:
        status_code = 400

        def json(self):
            errors = [{"message": "Value bigger than max", "details": "sigma: 12.0 > 10.0"}]
            return {"error": {"message": "Prompt outputs failed validation"},
                    "node_errors": {"8": {"class_type": "ImageBlur", "errors": errors}}}  # fmt: skip

    assert "ImageBlur (8): Value bigger than max: sigma: 12.0 > 10.0" in comfy.describe_rejection(Refused())


# ----------------------------------------------------------------------------- in image mode


def test_a_request_with_a_reference_edits_it(image_agent, standin):
    made = media.request(image_agent, "@photo.png make the sky purple")
    graph = standin.prompts[-1]
    loads = [n["inputs"]["image"] for n in graph.values() if n["class_type"] == "LoadImage"]
    assert loads == ["lcode-0-photo.png"] and standin.uploads["lcode-0-photo.png"] == tiny_png()
    assert graph["4"]["inputs"]["text"] == "make the sky purple"
    entry = image_agent.media_results[-1]
    assert entry["kind"] == "edit" and entry["job"]["sources"] == [str(image_agent.cwd / "photo.png")]
    assert json.loads(made[0].files[0].with_suffix(".json").read_text())["edit_of"] == ["photo.png"]


def test_a_model_that_doesnt_edit_hands_over(image_agent, standin):
    image_agent.settings.image_model = "z-image-turbo"
    standin.models["diffusion_models"] = [*FLUX["diffusion_models"], "z_image_turbo_int8_convrot.safetensors"]
    media.request(image_agent, "@photo.png make it night")
    assert "Z-Image Turbo doesn't edit pictures: FLUX.2 klein 4B makes this one" in text(image_agent)


def test_follow_ups_edit_extend_upscale_and_cut_out(image_agent, standin):
    respond(image_agent, "a white mug on a desk")
    handle_command(image_agent, "/edit 1 make the mug black @style.png", LAPTOP)
    assert image_agent.media_results[-1]["kind"] == "edit"
    loads = [n["inputs"]["image"] for n in standin.prompts[-1].values() if n["class_type"] == "LoadImage"]
    assert len(loads) == 2 and loads[1].endswith("style.png")
    handle_command(image_agent, "/vary 2 --n 2", LAPTOP)  # variations of an edit are edits of the same images
    assert [r["kind"] for r in image_agent.media_results[-2:]] == ["edit", "edit"]
    handle_command(image_agent, "/extend 1 --ar 16:9", LAPTOP)
    assert image_agent.media_results[-1]["kind"] == "extend"
    assert image_agent.media_results[-1]["path"].endswith("-extended.png")
    assert any(n["class_type"] == "ImagePadForOutpaint" for n in standin.prompts[-1].values())
    handle_command(image_agent, "/upscale 1 4", LAPTOP)
    graph = standin.prompts[-1]
    assert (
        graph["2"]["inputs"]["model_name"] == "RealESRGAN_x4plus.safetensors" and graph["4"]["inputs"]["scale_by"] == 1
    )
    assert image_agent.media_results[-1]["path"].endswith("-upscaled-4x.png")
    handle_command(image_agent, "/upscale 1 3", LAPTOP)
    assert "/upscale makes 2 or 4 times the size" in text(image_agent)
    handle_command(image_agent, "/nobg", LAPTOP)  # the last image
    assert standin.prompts[-1]["3"]["class_type"] == "RemoveBackground"
    assert image_agent.media_results[-1]["path"].endswith("-upscaled-4x-nobg.png")
    handle_command(image_agent, "/vary", LAPTOP)
    assert "/vary the image it came from" in text(image_agent)


def test_use_makes_the_next_requests_follow_an_image(image_agent, standin):
    respond(image_agent, "a white mug on a desk")
    handle_command(image_agent, "/use 1", LAPTOP)
    assert "References for the next requests" in text(image_agent)
    respond(image_agent, "the same mug on a beach")
    assert image_agent.media_results[-1]["kind"] == "edit"
    handle_command(image_agent, "/use off", LAPTOP)
    respond(image_agent, "a red fox")
    assert image_agent.media_results[-1]["kind"] == "image" and image_agent.image_refs == []


def test_a_mask_needs_an_image_and_missing_files_are_named(image_agent, repo):
    (repo / "mask.png").write_bytes(tiny_png())
    with pytest.raises(media.MediaError, match="--mask needs the image it masks"):
        media.request(image_agent, "make it night --mask mask.png")
    media.request(image_agent, "@photo.png make the sky purple --mask mask.png")
    assert image_agent.media_results[-1]["kind"] == "inpaint"
    with pytest.raises(media.MediaError, match=r"There's no image nothing\.png"):
        media.request(image_agent, "@nothing.png make it night")
    with pytest.raises(media.MediaError, match="There's no image #7"):
        media.request(image_agent, "@#7 make it night")
