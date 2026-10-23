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
Starting ComfyUI (~/comfy/ComfyUI) in the background.
Describe an image and press Enter: FLUX.2 klein 4B makes it on your GPU, into .lcode/media.
image ❯ a red fox sitting in fresh snow at dusk
◆ .lcode/media/20261023-184531-a-red-fox-sitting-in-fresh-snow-at-dusk-1.png  1024×1024 · FLUX.2 klein 4B · seed 3714779554 · 8.0 s
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

## Image mode

In image mode, what you type is a description, and lcode makes the picture. No language model is
involved: lcode fills in its own ComfyUI workflow for the image model and follows the job itself.
For now that's one image per request, 1024×1024, with the image model you choose (see
[Image models](#image-models)). Sizes, seeds and variations come next (#151).

- **Progress:** the status line says what ComfyUI is doing (loading the model, reading the
  prompt, drawing, decoding), the step, the time left and the time so far. If other jobs are ahead
  in ComfyUI's queue, it says how many.
- **Cancel** with Esc or Ctrl+C: lcode cancels the job in ComfyUI too, so it stops using the GPU.
- **Type ahead:** what you type while an image is being made waits in the next prompt.
- **Files:** each image goes to `.lcode/media/` in the project (the `media_dir` setting), named
  after the time and the prompt, with a `.json` next to it holding the prompt, model, seed, size and
  steps. PNGs carry the same in their own metadata (an `lcode` text chunk), and ComfyUI's workflow
  as well. `.lcode/media/` keeps itself out of git; a folder of your own (`lcode config set
  media_dir assets/generated`) doesn't.
- **Nothing is lost:** lcode writes each job down while it runs. If lcode crashes or the terminal
  closes before an image arrives, the next request in image mode saves it.
- **From scripts:** `lcode --mode image -p "a red fox in the snow"` makes one image and exits;
  with `--output json` it prints a result with the file paths, model, seed, size and time.
- **No Ollama needed:** `lcode --mode image` starts even when Ollama isn't running or the coding
  model isn't installed; `/mode code` then says what's missing.

## ComfyUI

ComfyUI runs the image and video models; lcode drives it through its API.

- **One that's running** at `comfyui_url` (by default `http://127.0.0.1:8188`) is used as it is.
- **Otherwise lcode starts one** in the background when you switch to image mode, so the first
  image waits less, and stops it when you quit or go back to code mode, or when the terminal
  closes. It looks in the `comfyui_path` setting, then the folder the `comfy` CLI uses, then
  `~/comfy/ComfyUI`, `~/ComfyUI`, `~/comfyui` and `~/Documents/ComfyUI`. It uses that install's
  own Python (its `.venv` or `venv`), and logs to `comfyui.log` in lcode's state folder.
- **With none,** your first image request offers to install ComfyUI into lcode's state folder with
  [comfy-cli](https://github.com/Comfy-Org/comfy-cli) and the PyTorch build for your GPU (NVIDIA,
  Apple Silicon or CPU). That downloads several GB. To install it yourself:

  ```bash
  uvx --from comfy-cli comfy install
  ```

- **`lcode doctor`** shows whether ComfyUI is running or found, and whether the image model is
  installed.

## Image models

lcode's media catalog lists the image models it can run, with what each needs and whether it fits
your machine. `/models` in image mode shows them, numbered; pick one with its number, or `/model
z-image-turbo`. `lcode config set image_model z-image-turbo` makes one the default.

```text
image ❯ /models
  1. ● FLUX.2 klein 4B (flux2-klein-4b)  text-to-image · 12.4 GB · installed · fits (~7 GB of 12 GB GPU memory)
        1024×1024 in 2.6 s (8 s with loading), RTX 4080 Laptop 12 GB
  2.   Z-Image Turbo (z-image-turbo)  text-to-image · 14.6 GB · 6.5 GB to download · fits (~9 GB of 12 GB GPU memory)
  Switch to which model? (number or name, Enter to keep FLUX.2 klein 4B): 2
Image model: Z-Image Turbo.
Not installed yet: 6.5 GB download with your next image (you're asked first).
```

| Model | Made by | License | Steps | Good at |
|---|---|---|---|---|
| **FLUX.2 klein 4B** (default) | Black Forest Labs | Apache 2.0 | 4 | Fast and small |
| **Z-Image Turbo** | Tongyi-MAI (Alibaba) | Apache 2.0 | 8 | Photorealism, text in images |

Measured speeds are in the list (`/models`, `lcode media`) and in [Measured](#measured).

- **Installing:** a model that isn't installed downloads with your next image, after you say yes.
  You can also install it ahead with `lcode media install z-image-turbo`. lcode downloads only
  the files ComfyUI doesn't have: models share text encoders and VAEs, so Z-Image Turbo needs
  6.5 GB once FLUX.2 klein is there. Files go into ComfyUI's model folders, and the folders of
  `extra_model_paths.yaml` count when ComfyUI is running.
- **Safe downloads:** lcode checks the disk first and keeps 2 GB free. The progress shows the
  speed and the time left. Each file is checked against Hugging Face's SHA-256 before it's used.
- **Interrupted?** Ctrl+C pauses a download, and so does a dropped connection. Run the same install
  again and it continues where it stopped (a `.part` file holds what arrived).
- **Gated models:** for a model whose license you must accept on Hugging Face, accept it there
  and set `HF_TOKEN`. lcode sends the token to huggingface.co only.
- **Will it fit?** On NVIDIA GPUs the list compares the model's peak GPU memory with your GPU.
  The peak is measured for tested models and estimated for the others: ComfyUI loads the text
  encoder, the model and the VAE one after another, so the largest file sets it. A model too big
  for the GPU still runs, slower: ComfyUI keeps part of it in RAM. On Apple Silicon, the list
  compares it with unified memory.

```bash
lcode media                        # image and video models, what fits, what's installed
lcode media install z-image-turbo  # download what's missing (asks first; --yes doesn't)
```

### Measured

On an RTX 4080 Laptop GPU (12 GB) with 32 GB of RAM, ComfyUI 0.38, 1024×1024:

| Model | First image (loading included) | Next images | Peak GPU memory |
|---|---|---|---|
| FLUX.2 klein 4B | — | — | — |
| Z-Image Turbo | — | — | — |

## One model on the GPU at a time

Image and video modes don't need the big coding model: the image or video model does the work. A
small optional model will help with prompts later. Image and video models need the GPU to
themselves:

- **Into image or video mode:** lcode unloads its coding model from Ollama, and checks again
  before each image.
- **Back to code mode:** lcode stops the ComfyUI it started, or asks yours to free its memory,
  then loads the coding model again in the background.

## Coming next

Video mode doesn't generate yet: a request there says what's coming instead of going anywhere.

- **#150 Media model catalog** (image models done; video models come with video generation).
- **#151, #152 Image mode:** generating, variations, sizes and seeds, a gallery, editing,
  references, inpainting, upscaling.
- **#153, #154 Video mode:** from text or a start image, first and last frames, several
  references, camera moves, longer clips, sound.
- **#155 Movies:** storyboards with a consistent cast, shots stitched into one film.
- **#156 Prompt assistant:** a small local model for prompts and follow-ups.
- **#157 Cloud generation**, with costs shown (Grok Imagine first).

The permission mode (`ask`, `plan`, `auto-edit`, `yolo`) is a different setting: `/permissions`
sets it, and Shift+Tab cycles it as before.
