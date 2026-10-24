# Modes: code, image and video

lcode makes three kinds of things, each in its own mode with its own models:

| Mode | What it makes | The model doing the work |
|---|---|---|
| **code** (default) | Code: it reads, writes, runs and checks your project | A coding language model in Ollama (or another server) |
| **image** | Images from a description, edits of your images (references, inpainting, outpainting), upscales and cut-outs | An image model, run by ComfyUI on your GPU, or [in the cloud](#in-the-cloud-grok-imagine) |
| **video** | Video clips from a description or a start image, and [films](#films) | A video model, run by ComfyUI on your GPU, or [in the cloud](#in-the-cloud-grok-imagine) |

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

## Video mode

Video mode works like image mode: what you type describes a clip, and **Wan 2.2 TI2V 5B** makes it on
your GPU. Start it from a picture with `@photo.png` (or `@#3`, an image from this session), and the
clip begins with that frame. From image mode, `/animate 3 the waves roll in` switches to video mode
and animates image #3.

```text
video ❯ waves rolling onto a beach at sunset --seconds 5
◆ #1 .lcode/media/20261024-….mp4  864×480 · Wan 2.2 TI2V 5B · seed 1234 · …
video ❯ /play
```

- **Options:** the image options work here too (`--n`, `--ar`, `--seed`, `--quality`, `--style`,
  `--no`, and `/options`), plus `--seconds` (1 to 8; 5 by default, at 24 frames per second).
  Clips are 16:9, or 9:16 from an upright start picture (cropped to it), unless you give a shape:
  Wan 2.2 knows those two shapes best, and a square or small clip can break up into streaks.
- **Quality:** `fast` makes 864×480 in 12 steps, `balanced` (the default) 864×480 in 20 steps, and
  `best` the model's own 1280×704 in 30 steps. Smaller than 864×480 breaks this model up into
  streaks, so lcode doesn't go lower.
- **Camera moves:** describe them as shots ("slow zoom in", "dolly shot along the shore", "pan
  left"). "The camera moves in" can put a camera in the picture.
- **Negative prompt:** Wan uses one. lcode passes Wan's own default (it avoids static, blurry,
  over-exposed clips) unless you give `--no "…"`.
- **Results:** an MP4 with a JSON file next to it (prompt, seed, size, frames, fps, start image),
  numbered in the session's gallery with the images. The first frame shows in the terminal.
  `/play 4` opens a clip in your player, and `/vary` and `/again` make new clips from the same
  description and start image.
- **Measured** on the RTX 4080 Laptop GPU (12 GB), a 5-second clip (121 frames): `fast` 100 s,
  `balanced` 150 s (the same from a start image), `best` 10.5 minutes.
- **First and last frame:** two pictures, `@start.png @end.png the flower opens`, make a clip that
  goes from the first to the last. Wan 2.2 Fun InP 5B makes these (a 3 s clip in 62 s at `fast`;
  `fast` can change abruptly halfway, `balanced` moves more smoothly).
- **Camera moves:** `--camera pan-left` (also `pan-right`, `pan-up`, `pan-down`, `tilt-up`,
  `tilt-down`, `zoom-in`, `zoom-out`, `dolly-in`, `dolly-out`, `rotate-left`, `rotate-right`,
  `static`), from a description or a start picture. Wan 2.1 Fun Camera 1.3B makes these, at 832×480
  and 16 frames per second: a 3 s move takes about 2 minutes.
- **Extend a clip:** `/extend 4 --seconds 3 it flies away` continues clip #4 from its last frame
  and joins the new part after it, as one longer clip (`…-extended.mp4`; 3 s more in about a
  minute at `fast`).
- **Reference pictures:** `@cat.png @hat.png --ref the cat wears the hat in a garden` makes a clip
  that shows what the pictures show (who, what, where), not starting from them. Wan 2.1 VACE 1.3B
  makes these (832×480, 16 fps; a 3 s clip in about 3 minutes).
- **Restyle a clip:** `/restyle 4 as a watercolor painting --style watercolor` makes clip #4 again in
  a new look, following its outlines (Canny edges, up to its first 81 frames). VACE 1.3B follows them
  loosely: the layout and motion carry over, sizes can change. Naming the style with `--style`
  helps. About 5 minutes for 3 s.
- **Smoother:** `/smooth 4` (or `/smooth 4 4`) adds in-between frames with FILM: 2 or 4 times the
  frames per second, in seconds.
- **Bigger:** `/upscale 4` works on clips too (Real-ESRGAN, 2 or 4 times): 864×480 becomes 1728×960
  in about 2 minutes for 3 s.
- **Sound:** `/sound 4 wings buzzing and birdsong` adds sound effects or ambience made by Stable
  Audio Open 1.0 (up to 47 s; seconds to make), as an AAC track in the MP4 (`…-sound.mp4`). Its
  license is the Stability AI Community License: free under $1M yearly revenue.
- **Which model:** the one you chose (`/models`) makes ordinary clips. For first and last frames or a
  camera move, lcode uses the model that can, installs it when you agree, and says so.
- **Long renders:** progress shows the step and the time left. With `notify` on, lcode sends a
  desktop notification when a clip that took longer than `notify_after` seconds is done.

## Films

From one idea, lcode makes a short film with the same characters in every shot:

```text
video ❯ /film a red fox finds a lost white kitten in a snowy forest and they walk home together --shots 3 --seconds 3 --transition fade --sound "soft wind and footsteps in snow"
The Fox and the Kitten  (.lcode/media/films/the-fox-and-the-kitten)
  look: cinematic realism, soft winter light, cool blues and warm oranges, shallow depth of field
  cast: Fox — a red fox with a white chest and bushy tail
  cast: Kitten — a small white kitten with large ears
  1. The Fox spots the shivering Kitten trapped under a snowbank in a dense pine forest. [Fox, Kitten] — static low angle shot, 3 s
  2. The Fox gently nudges the Kitten free and they stand side by side facing the camera. [Fox, Kitten] — slow push in, 3 s
  3. The pair trots away from the forest into a warm, lit doorway together. [Fox, Kitten] — tracking shot, 3 s
…
◆ The Fox and the Kitten: .lcode/media/films/the-fox-and-the-kitten/film-sound.mp4
```

![The cast's reference pictures (right) and a frame from each of the three shots](assets/film-fox-and-kitten.jpg)

1. **The storyboard:** the [prompt assistant](#the-prompt-assistant) writes it from your idea: the
   look of the whole film, the cast (each living character described once; things like a kite or a car are
   described in the shots) and the shots (what happens,
   the camera, who's in it, how long). It's saved as `storyboard.json` (and a readable
   `storyboard.md`) in the film's folder.
2. **The cast:** each character gets a reference picture, made once, in `cast/`.
3. **The shots:** each starts as a keyframe made with the cast's pictures as references, so the
   characters look the same in every shot, and is then animated by video mode's model (`shots/`).
4. **The film:** the shots are joined into `film.mp4` with cuts or fades (ffmpeg), and
   `--sound "…"` adds a sound track for the whole film (`film-sound.mp4`).

| Command | What it does |
|---|---|
| `/film <idea>` | The storyboard, then the film. Options: `--shots 3` (1 to 8), `--seconds 4` per shot, `--quality fast`, `--transition cut` or `fade`, `--sound "…"` |
| `/film plan <idea>` | Only the storyboard, to read or edit before making it |
| `/film make [name]` | The film from its storyboard (the latest film without a name) |
| `/film shot 2 [name]` | Shot 2 made again, and the film joined again |
| `/film stitch [name]` | The shots joined again (after editing the transition or the sound) |
| `/film show [name]`, `/film list` | One film's storyboard, or all the films here |

- **Needs:** the prompt assistant's model (`ollama pull qwen3.5:4b`), FLUX.2 klein 4B, video
  mode's model, and ffmpeg.
- **Measured** on the RTX 4080 Laptop GPU (12 GB): the 3-shot film above (3 s shots at `fast`, two
  cast members, fades and sound) took 4 minutes, after a 5-second storyboard. The fox and the
  kitten look the same in all three shots.

## The prompt assistant

Image and video models do better with some prompts than others. A small local language model,
the prompt assistant, can write the fuller prompt for you, read a short follow-up as a change to
the last picture, and check a result against what you asked. It runs in Ollama, loaded for each
job and unloaded right after, so the image or video model keeps the GPU.

```text
video ❯ city at night rain
✎ A lone figure walks through a neon-drenched city street under a relentless summer rain. The shot slowly zooms in on their silhouette as water droplets ripple across the pavement…
◆ #1 .lcode/media/…-city-at-night-rain.mp4  864×480 · Wan 2.2 TI2V 5B · seed 2001 · 62 s
```

- **Where it helps:** the `prompt_assistant` setting. `video` (the default) writes video prompts,
  `all` writes picture prompts too and reads follow-ups, `off` sends prompts as typed, and an
  Ollama model's name (`qwen3.5:9b`) uses that model everywhere. Otherwise lcode picks a small
  installed one (`qwen3.5:4b` first); with none installed, prompts go as typed. `--raw` sends one
  request as typed.
- **Your words stay yours:** the gallery and file names keep what you typed; the fuller prompt is
  shown (✎) and saved in the JSON next to the result (`full_prompt`).
- **Any language:** it writes in English from what you type. FLUX.2 klein reads other languages
  well on its own; Wan does better in English.
- **A start picture** is shown to it (it can see), so a video prompt describes what the picture
  shows and then the motion.
- **Follow-ups** (with `all`): a short request after a picture ("darker", "add a hat") is read as a
  change to it and made as an edit; a new description makes a new picture.
- **Checking results:** `lcode config set check_results true` has it look at each picture and say
  whether it shows what you asked, with a better prompt when it doesn't (⚠ … Try: …).

### Measured with the assistant

On the RTX 4080 Laptop GPU (12 GB) with `qwen3.5:4b`, the same seeds with and without the
assistant:

- **Pictures (FLUX.2 klein 4B, six short prompts, two not in English):** no clear gain. "fox snow",
  "a cat astronaut", the panda (asked in Chinese) and the OPEN sign came out about as well either
  way; "cozy cabin" became a twilight exterior instead of an interior, and for a rainy Paris café
  (asked in German) it added a "Bienvenue" sign nobody asked for. It costs about 6 s per picture,
  12 s with the check, against 3.6 s for the picture itself. So pictures go as typed by default.
- **Video (Wan 2.2 TI2V 5B, `fast`):** clearly better. "city at night rain" as typed came out
  almost black; the fuller prompt made a neon street in the rain with a person walking through it.
  "dog running beach" became a more detailed golden retriever on the sand. The few seconds it
  takes are small next to a minute of video. So video prompts are written by default.
- **The check** said every one of the six pictures matched, which they did; it hasn't been
  measured on pictures that miss.

## In the cloud: Grok Imagine

For what a laptop can't run, or when speed matters, image and video mode can use xAI's Grok Imagine
with your own API key. It's chosen like a local model and marked *cloud* in `/models` and `lcode
media`. lcode reads the key from the environment only:

```bash
export XAI_API_KEY=xai-…     # from https://console.x.ai, then start lcode
```

```text
image ❯ /model grok-image
Image model: Grok Imagine Image 2.0.
◆ Grok Imagine Image 2.0 runs at xAI: each request shows what goes there and about what it costs ($0.04 per image), and asks first.
image ❯ a red fox in the snow --n 2
╭─ Make it at xAI? ─────────────────────────────────────────────────────────────────────────────╮
│ Where: Grok Imagine Image 2.0 (grok-imagine-image-2.0) at xAI, api.x.ai                        │
│ What goes: the prompt. Your XAI_API_KEY goes with it, to api.x.ai only.                        │
│ Prompt: a red fox in the snow                                                                  │
│ Cost: about $0.08 (2 images × $0.04), billed to your xAI account; prices as of 2026-10-24, the │
│ real ones: https://docs.x.ai/developers/pricing                                                │
╰────────────────────────────────────────────────────────────────────────────────────────────────╯
  Send it to xAI (about $0.08)? [y/N] y
◆ #1 .lcode/media/20261024-153907-a-red-fox-in-the-snow-1.jpg  … · Grok Imagine Image 2.0 · … s
◆ #2 .lcode/media/20261024-153907-a-red-fox-in-the-snow-2.jpg  … · Grok Imagine Image 2.0 · … s
  about $0.08 at xAI (this session: about $0.08)
```

| Model | `/model` | Makes | Price (xAI's, on 2026-10-24) |
|---|---|---|---|
| **Grok Imagine Image 2.0** | `grok-image` | Pictures from text; edits of one picture | $0.04 per picture, 1k or 2k; $0.01 per picture sent |
| **Grok Imagine Video 1.5** | `grok-video` | Clips from text or a start picture | Per second: $0.08 at 480p, $0.14 at 720p, $0.25 at 1080p; $0.01 per picture sent |

- **What's sent:** the prompt (the [prompt assistant](#the-prompt-assistant)'s fuller one when it
  wrote one, shown above the panel) and the picture you edit or start a clip from. Nothing else:
  not your project, not other pictures, not the conversation.
- **The key** goes in the request's `Authorization` header to xAI's API, and nowhere else. Results
  are downloaded from the address xAI gives, without it. `XAI_API_BASE` sends the requests, key
  included, to another address, such as a proxy you run.
- **What it costs** is shown before every request, from lcode's copy of xAI's prices with the date
  they were checked, and nothing is sent until you say yes. The line under the results adds it to
  the session's total. It's an estimate: xAI bills by its [pricing page](https://docs.x.ai/developers/pricing).
- **Options:** `--quality` picks the resolution: pictures are 1k (`fast`, `balanced`) or 2k
  (`best`), at the same price; clips are 480p, 720p or 1080p. `--ar` picks the nearest shape xAI
  offers. `--camera` becomes words in the prompt. Seeds and `--no` aren't used.
- **What it doesn't do here** is made by a local model, and lcode says which: masks, several
  pictures at once and extending a picture (FLUX.2 klein); clips between two frames, reference
  pictures, extending and restyling a clip (the Wan models).
- **Stopping:** Esc or Ctrl+C stops waiting for a clip, but xAI may still make it and bill it. The
  message gives the request's id.
- **No terminal, no cloud:** `lcode -p` in image or video mode refuses a cloud model, because
  nobody can say yes to the cost.
- **[Films](#films)** work with cloud models too; each picture and clip asks.
- **Files:** results land in the media folder and the gallery as local ones do. The JSON next to
  each says what was sent, to which model, the estimated cost and xAI's request id for a clip.
  xAI's terms and usage policies apply to what you send.
- **Tested** against a stand-in for xAI's API that answers as its documentation describes; prices
  are from xAI's pricing page.

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

Image and video modes don't need the big coding model: the image or video model does the work, and
the [prompt assistant](#the-prompt-assistant) loads only while it writes. Image and video models
need the GPU to themselves:

- **Into image or video mode:** lcode unloads its coding model from Ollama, and checks again
  before each image.
- **Back to code mode:** lcode stops the ComfyUI it started, or asks yours to free its memory,
  then loads the coding model again in the background.

The permission mode (`ask`, `plan`, `auto-edit`, `yolo`) is a different setting: `/permissions`
sets it, and Shift+Tab cycles it as before.
