# Modes: code, image and video

lcode makes three kinds of things, each in its own mode with its own models:

| Mode | What it makes | The model doing the work |
|---|---|---|
| **code** (default) | Code: it reads, writes, runs and checks your project | A coding language model in Ollama (or another server) |
| **image** | Images from a description and reference images | An image model, run by ComfyUI on your GPU |
| **video** | Videos from a description, a start image or references | A video model, run by ComfyUI on your GPU |

```text
❯ /mode image
Mode: image (local image generation).
Unloaded qwen3.6-35b to free the GPU for images.
image ❯
```

- **Switching:** `/mode code`, `/mode image` and `/mode video` switch in a session. `lcode --mode image`
  starts in one, and `lcode config set mode video` makes it the default. `/mode` alone shows
  the current one.
- **Where it shows:** the banner, the status bar and the prompt (`image ❯`) show the mode.
- **Sessions:** a session keeps the mode it was made in, and resuming it brings that mode back.

## Code mode makes code only

Code mode is the coding agent, as before, and it no longer generates images or videos. The MCP
servers that did (`comfyui` and `xai`) are gone from the MCP catalog, and lcode doesn't load them
in code mode even if they're still in your `mcp.json`. It says so once.

## One model on the GPU at a time

Image and video modes don't need the big coding model: the image or video model does the work. A
small optional model will help with prompts later. Image and video models need the GPU to
themselves:

- **Into image or video mode:** lcode unloads its coding model from Ollama.
- **Back to code mode:** lcode asks ComfyUI to free its memory (at `comfyui_url`, by default
  `http://127.0.0.1:8188`), then loads the coding model again in the background.

## Coming next

Image and video modes don't generate yet. Until the items below arrive, a request in those modes
says what's coming instead of going anywhere.

- **#149 Local generation engine:** lcode drives ComfyUI itself: finding or installing it, jobs,
  progress, cancelling, and saving outputs with their settings.
- **#150 Media model catalog:** image and video models that fit your machine, installed on
  demand.
- **#151, #152 Image mode:** generating, variations, sizes and seeds, a gallery, editing,
  references, inpainting, upscaling.
- **#153, #154 Video mode:** from text or a start image, first and last frames, several
  references, camera moves, longer clips, sound.
- **#155 Movies:** storyboards with a consistent cast, shots stitched into one film.
- **#156 Prompt assistant:** a small local model for prompts and follow-ups.
- **#157 Cloud generation**, with costs shown (Grok Imagine first).

The permission mode (`ask`, `plan`, `auto-edit`, `yolo`) is a different setting: `/permissions`
sets it, and Shift+Tab cycles it as before.
