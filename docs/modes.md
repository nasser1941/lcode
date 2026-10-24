# Modes: code, image and video

lcode makes three kinds of things, each in its own mode with its own models:

| Mode | What it makes | The model doing the work |
|---|---|---|
| **code** (default) | Code: it reads, writes, runs and checks your project | A coding language model in Ollama (or another server) |
| **image** | Images from a description, edits of your images (references, inpainting, outpainting), upscales and cut-outs | An image model, run by ComfyUI on your GPU |
| **video** | Videos from a description, a start image or references | A video model, run by ComfyUI on your GPU |

```text
❯ /mode image
Mode: image (local image generation).
Unloaded qwen3.6-35b to free the GPU for images.
Starting ComfyUI (~/comfy/ComfyUI) in the background.
Describe an image and press Enter: FLUX.2 klein 4B makes it on your GPU, into .lcode/media.
image ❯ a red fox sitting in fresh snow at dusk
◆ #1 .lcode/media/20261023-184531-a-red-fox-sitting-in-fresh-snow-at-dusk.png  1024×1024 · FLUX.2 klein 4B · seed 3714779554 · 8.0 s
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
involved: lcode fills in its own ComfyUI workflow for the image model and follows the job itself,
with the image model you choose (see [Image models](#image-models)).

```text
image ❯ a red fox in fresh snow --n 2 --ar 16:9 --seed 7
◆ #1 .lcode/media/20261023-194019-a-red-fox-in-fresh-snow.png  1360×768 · FLUX.2 klein 4B · seed 7 · 4.9 s
▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀   (the picture, in colour)
◆ #2 .lcode/media/20261023-194024-a-red-fox-in-fresh-snow.png  1360×768 · FLUX.2 klein 4B · seed 8 · 2.7 s
image ❯ /vary 1 --n 2
Variations of #1: a red fox in fresh snow
◆ #3 …
```

### Options

Add them to a request, or set them for the session with `/options` (`/options n=4 ar=16:9
quality=best style=photo`). A flag in a request wins over `/options` for that request. `/options`
alone shows them.

| Flag | `/options` | What it does |
|---|---|---|
| `--n 4` | `n=4` | How many images: 1 to 8. Each gets its own seed. |
| `--ar 16:9` | `ar=16:9` | The shape: a ratio (`3:2`, `9:16`, up to 4:1), a name (`square`, `portrait`, `landscape`, `wide`, `tall`, `photo`, `cinema`), or pixels (`--size 1344x768`). |
| `--seed 42` | `seed=42` | The same seed and settings make the same image. With `--n`, the seeds count up (42, 43, …). `seed=random` goes back to a new one each time. |
| `--quality best` | `quality=best` | `fast` (about 0.5 megapixels, fewer steps for models that allow it), `balanced` (1 megapixel, the default), `best` (2 megapixels, more steps where they help). |
| `--style photo` | `style=photo` | Adds a style to the description: `photo`, `cinematic`, `anime`, `watercolor`, `oil`, `pixel`, `3d`, `sketch`, `comic`, `flat`. `style=none` takes it off. |
| `--no "text, people"` | `no="text"` | A negative prompt, for models that use one. FLUX.2 klein and Z-Image Turbo don't (they're distilled), and lcode says so: describe what you want instead. |

### Results

- **Numbered:** every image in the session gets a number (`#3`), shown with its file, size, model,
  seed and time.
- **Shown in the terminal:** in kitty, Ghostty, iTerm2 and WezTerm the picture itself appears
  under its line. Other terminals get a small colour preview drawn with half blocks. The
  `preview` setting chooses: `auto`, `kitty`, `iterm`, `blocks` or `off`. Inside tmux or screen,
  lcode draws the half-block preview.
- **Follow-ups:**

| Command | What it does |
|---|---|
| `/again` | The last request again with new seeds; flags change it (`/again --n 4 --ar 9:16`) |
| `/vary 3` | Four variations of image #3: its prompt and options, new seeds (`/vary 3 --n 2`; the last image without a number) |
| `/gallery` | The session's images with their prompt, size, model and seed; `/gallery all` lists the latest 50 in the media folder |
| `/open 3` | Opens image #3 in your image viewer (the last one without a number) |

- **Sessions:** the gallery and `/options` are saved with the session, so `lcode -c` or `/resume`
  brings them back. An image session is listed under its first prompt.

### While it works

- **Progress:** the status line says what ComfyUI is doing (loading the model, reading the
  prompt, drawing, decoding), the step, the time left and the time so far. If other jobs are ahead
  in ComfyUI's queue, it says how many.
- **Cancel** with Esc or Ctrl+C: lcode cancels the job in ComfyUI too, so it stops using the GPU.
- **Type ahead:** what you type while an image is being made waits in the next prompt.
- **Files:** each image goes to `.lcode/media/` in the project (the `media_dir` setting), named
  after the time and the prompt, with a `.json` next to it holding the prompt, model, seed, size,
  steps, quality and style. PNGs carry the same in their own metadata (an `lcode` text chunk), and ComfyUI's workflow
  as well. `.lcode/media/` keeps itself out of git; a folder of your own (`lcode config set
  media_dir assets/generated`) doesn't.
- **Nothing is lost:** lcode writes each job down while it runs. If lcode crashes or the terminal
  closes before an image arrives, the next request in image mode saves it.
- **From scripts:** `lcode --mode image -p "a red fox in the snow --n 2"` makes the images and
  exits; with `--output json` it prints a result with the file paths, the model, the seeds, the size
  and the time.
- **No Ollama needed:** `lcode --mode image` starts even when Ollama isn't running or the coding
  model isn't installed; `/mode code` then says what's missing.

### Editing

FLUX.2 klein 4B edits pictures as well as making them: it takes images as references and an
instruction as the prompt. When the chosen model can't edit (Z-Image Turbo), lcode hands edits to
FLUX.2 klein and says so.

```text
image ❯ /edit 1 make the mug matte black with a small gold star
◆ #2 .lcode/media/20261024-100504-make-the-mug-matte-black-with-a-small-go.png  1024×1024 · FLUX.2 klein 4B · seed 1798225303 · 6.1 s
image ❯ /extend 1 --ar 16:9
Extending #1 from 1024×1024 to 1824×1024 before scaling.
◆ #4 .lcode/media/…-extended.png  1360×768 · FLUX.2 klein 4B · seed 2994584653 · 6.4 s
image ❯ /nobg 2
◆ #6 .lcode/media/…-nobg.png  1024×1024 · BiRefNet · 0.7 s
```

| What | How | What happens |
|---|---|---|
| **Edit by instruction** | `/edit 3 make the sky purple` (the last image without a number), or `@photo.png make the sky purple` | The image goes in as a reference, the instruction is the prompt. The result keeps the image's shape unless you give one (`--ar`). |
| **References** | `@photo.png @#3 put the mug from image 1 on the table in image 2`, or `/edit 3 … @style.png` | Several images, in order ("image 1", "image 2" in the instruction): a subject, a style, a place. `@"a file with spaces.png"` works too. |
| **Keep following an image** | `/use 3` (or `/use photo.png`), then plain requests; `/use off` | Every request is an edit of those images until you stop it: handy for one subject in many scenes. |
| **Inpaint** | `@photo.png --mask mask.png make the sky purple` | Only where the mask image is white changes (with a soft edge); the rest stays exactly as it was. |
| **Outpaint** | `/extend 3 --ar 16:9`, or `/extend 3 --left 256 --top 128`, plus words about the new part | The canvas grows, the model paints only the new border, and the original is pasted back as it was. |
| **Upscale** | `/upscale 3` (2×) or `/upscale 3 4` | Real-ESRGAN, a few seconds: a 1024×1024 image becomes 2048×2048. |
| **Remove the background** | `/nobg 3` | BiRefNet cuts out the subject: a PNG with a transparent background. |

- **Follow-ups work on edits too:** `/vary` on an edit makes new edits of the same images with the
  same instruction, and `/again` repeats the last request, edit or not.
- **The tools install on first use,** after you agree: Real-ESRGAN (0.07 GB) and BiRefNet (0.44 GB).
  `lcode media` lists them under "Tools for images".
- **Text in images:** FLUX.2 klein and Z-Image Turbo write short text you put in quotes
  (`a café sign that says "OPEN"`).
- **Measured** on the RTX 4080 Laptop GPU (12 GB): an edit takes 5 to 7 s, two references 10 s,
  inpainting and outpainting 6 to 12 s, a 2× upscale 6 s, and a background cut-out under a second
  once the tools are loaded.

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
  1. ● FLUX.2 klein 4B (flux2-klein-4b)  text-to-image · 12.4 GB · installed · fits (~9 GB of 12 GB GPU memory)
        1024×1024: 3.5 s per image, 14 s for the first with ComfyUI starting (RTX 4080 Laptop, 12 GB)
  2.   Z-Image Turbo (z-image-turbo)  text-to-image · 14.6 GB · 6.5 GB to download · fits (~9 GB of 12 GB GPU memory)
        1024×1024: 17 s per image (1.9 s per step), 23 s for the first (RTX 4080 Laptop, 12 GB)
  Switch to which model? (number or name, Enter to keep FLUX.2 klein 4B): 2
Image model: Z-Image Turbo.
Not installed yet: 6.5 GB download with your next image (you're asked first).
```

| Model | Made by | License | Steps | Good at |
|---|---|---|---|---|
| **FLUX.2 klein 4B** (default) | Black Forest Labs | Apache 2.0 | 4 | Speed: a few seconds per image |
| **Z-Image Turbo** | Tongyi-MAI (Alibaba) | Apache 2.0 | 8 | Photorealism, text in images; slower |

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
- **Will it fit?** On NVIDIA GPUs the list compares an estimate of the GPU memory the model needs
  with your GPU: the largest of its files plus 1.5 GB. ComfyUI loads the text encoder, the model and
  the VAE one after another, so the largest file sets the need. A model too big for the GPU still
  runs, slower, because ComfyUI keeps part of it in RAM. On Apple Silicon, the list compares it
  with unified memory.

```bash
lcode media                        # image and video models, what fits, what's installed
lcode media install z-image-turbo  # download what's missing (asks first; --yes doesn't)
```

### Measured

On an RTX 4080 Laptop GPU (12 GB), FLUX.2 klein 4B makes a 1360×768 image in 2.6 to 2.8 s and a
`--quality fast` image (720×720) in 1.3 to 2.0 s once it's loaded.


On an RTX 4080 Laptop GPU (12 GB) with 32 GB of RAM, ComfyUI 0.38, 1024×1024, four different
prompts each:

| Model | First image (ComfyUI starting, model loading) | Each next image | Per step |
|---|---|---|---|
| FLUX.2 klein 4B (fp8) | 14.3 s | 3.5 s | 0.6 s (4 steps) |
| Z-Image Turbo (int8) | 22.9 s | 17.4 s | 1.9 s (8 steps) |

ComfyUI uses whatever GPU memory is free to keep models loaded (11.3 to 11.5 GB here), so the
memory a model *needs* is lower than what `nvidia-smi` shows while it runs.

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

- **#153, #154 Video mode:** with video models in the catalog, from text or a start image, first and last frames, several
  references, camera moves, longer clips, sound.
- **#155 Movies:** storyboards with a consistent cast, shots stitched into one film.
- **#156 Prompt assistant:** a small local model for prompts and follow-ups.
- **#157 Cloud generation**, with costs shown (Grok Imagine first).

The permission mode (`ask`, `plan`, `auto-edit`, `yolo`) is a different setting: `/permissions`
sets it, and Shift+Tab cycles it as before.
