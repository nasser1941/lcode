# Changelog

All notable changes to lcode are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Clips up to 60 seconds** (#185), also from a start to an end picture: `@start.png @end.png the
  flower opens --seconds 25`. A clip longer than the 8 s a video model makes at once is made in
  parts, each starting on the last frame of the one before (the last one ending on the end
  picture), and joined into one clip with ffmpeg, without the frame each join would repeat.
  `/extend 4 --seconds 20` works the same way; Esc keeps the parts made so far as a clip.
- **Clips through several pictures:** `@bud.png @half-open.png @open.png the tulip opens
  --seconds 25` goes to each picture in turn, in an equal share of the clip.

### Fixed

- **`lcode update` ending in `ModuleNotFoundError: No module named 'rich._unicode_data…'`.**
  The update itself had worked; only its last message failed, and the Ollama update after it
  didn't run. Updating lcode can replace the files the running lcode is made of, so lcode now
  prepares what it will say before that and updates Ollama first. A git checkout is reinstalled
  on the Python lcode already runs on. Before, uv picked one from the folder you were in, such
  as a nearby `.venv`, and built a new environment when it was another version. An update runs
  the old version's code, so the update to this version can still show the error once. lcode is
  updated anyway: run `lcode update ollama` for the rest.

## [0.50.0] - 2026-10-26

### Added

- **Ask lcode how** (#184): `/help how do I make a clip from two pictures?` answers from lcode's
  own manual, which now ships inside the package (the docs' pages, for the installed version,
  offline). The answer says what to type, filled in with your details, and the first step is
  typed into the next prompt for you. In image and video mode, a question is answered instead of
  being made into a picture (`--raw` makes it anyway); in code mode, the coding model gets a
  `lcode_help` tool that searches the manual. The prompt assistant's small model answers in image
  and video mode (`qwen3.5:4b`); every command and option in an answer must exist in lcode,
  and the lines to type go through lcode's own option parser (`--seconds 90` is caught), with one
  retry before what's still wrong is marked.

## [0.49.1] - 2026-10-26

### Fixed

- **Image and video jobs no longer run out of GPU memory right after switching modes.** Switching
  to video mode in the first half minute after lcode starts unloaded the coding model while it was
  still loading; the load then finished and kept the GPU (10.8 GB of 12 here), and the next job
  sent to ComfyUI ran out of memory. A load that ends outside code mode now unloads itself, a job
  waits for a load still going, and unloading waits until Ollama has really freed the memory.
- **Running out of GPU memory says what holds it** (`Other programs hold GPU memory: obs
  (1.6 GB)…`) instead of ComfyUI's whole error, and lcode starts its ComfyUI afresh: after running
  out of memory ComfyUI crashed on the next job. A crash now says it was one, with the log's path,
  instead of "ComfyUI closed the websocket".
- **A nearly full disk stops a job before it starts** (under 1 GB free), and ComfyUI's "No space
  left on device" is said plainly: a film used to stop at shot 6 on it.

## [0.49.0] - 2026-10-24

### Added

- **Cloud generation in image and video mode** (#157): xAI's Grok Imagine as cloud models,
  `grok-image` (pictures, edits of one picture) and `grok-video` (clips from text or a start
  picture), chosen with `/model` and marked *cloud* in `/models` and `lcode media`. Before each
  request lcode shows what goes where and about what it costs, and sends nothing until you say yes;
  the session's total is shown after. The key comes from `XAI_API_KEY` only and goes to xAI's API
  only. What a cloud model can't do here (masks, several pictures, clips between two frames…) is
  made by a local model. See [In the cloud](https://nasser1941.github.io/lcode/modes/#in-the-cloud-grok-imagine).

### Changed

- The README no longer says code mode makes images through MCP servers (it hasn't since 0.40.0).

### Fixed

- **Films:** things and places no longer become cast members. A kite in the cast got a "character" picture
  (a person behind a kite) and walked on legs in every shot. The storyboard now keeps the cast to living
  characters and describes things in the shots: in 12 storyboards for ideas with props, things in the cast
  went from 6 to 1 (a plant).

## [0.48.0] - 2026-10-24

### Added

- **Films** (#155): `/film a fox finds a lost kitten --shots 3` makes a short film with the same
  characters in every shot. The prompt assistant writes the storyboard (saved, editable); each
  character gets a reference picture; each shot starts from a keyframe made with the cast as
  references and is animated; the shots are joined with cuts or fades, with an optional sound
  track. `/film shot 2` remakes one shot, `/film stitch` joins again. A real 3-shot film took 4
  minutes on a 12 GB laptop GPU. See [Films](https://nasser1941.github.io/lcode/modes/#films).

## [0.47.0] - 2026-10-24

### Added

- **The prompt assistant** (#156): a small local model (`qwen3.5:4b` when installed) writes fuller
  prompts in English for image and video mode, reads a start picture, takes follow-ups ("darker")
  as changes, and checks pictures against the request (`check_results`). See
  [The prompt assistant](https://nasser1941.github.io/lcode/modes/#the-prompt-assistant).
  - **Measured:** video prompts get much better (a near-black "city at night rain" became a neon
    street scene), picture prompts don't (FLUX.2 klein already reads short and non-English prompts
    well), so by default it writes video prompts only (`prompt_assistant = video`); `all` adds
    pictures and follow-ups. `--raw` sends one request as typed.
  - It's loaded for each job and unloaded right after, and your own words stay in the gallery.

### Fixed

- The modes page has its "ComfyUI" heading back (it had slipped under video mode).

## [0.46.0] - 2026-10-24

### Added

- **Video mode: references, restyling, smoother and bigger clips, sound** (#154, part 2). See
  [Video mode](https://nasser1941.github.io/lcode/modes/#video-mode).
  - **`@cat.png @hat.png --ref …`** makes a clip that follows reference pictures, and
    **`/restyle 4 …`** makes a clip again in a new look following its outlines (Wan 2.1 VACE 1.3B).
  - **`/smooth 4`** doubles (or quadruples) the frames with FILM, **`/upscale 4`** now works on clips,
    and **`/sound 4 …`** adds sound made by Stable Audio Open to a clip.

### Fixed

- Extending a clip keeps its pace: a 16 fps camera clip extended with the 24 fps model played too fast.

## [0.45.0] - 2026-10-24

### Added

- **Video mode: first and last frames, camera moves, longer clips** (#154, part 1). See
  [Video mode](https://nasser1941.github.io/lcode/modes/#video-mode).
  - **`@start.png @end.png …`** makes a clip from one picture to the other (Wan 2.2 Fun InP 5B).
  - **`--camera pan-left`** and twelve other moves (pan, tilt, zoom, dolly, rotate), from a
    description or a start picture (Wan 2.1 Fun Camera 1.3B, 16 fps).
  - **`/extend 4 --seconds 3 …`** in video mode continues a clip from its last frame into one
    longer clip.
  - lcode picks the model a job needs, installs it when you agree, and says so.

## [0.44.0] - 2026-10-24

### Added

- **Video mode makes videos** (#153). Describe a clip, or start it from a picture (`@photo.png`),
  and Wan 2.2 TI2V 5B makes it on your GPU. See [Video mode](https://nasser1941.github.io/lcode/modes/#video-mode).
  - **Options:** `--seconds` (1 to 8, 5 by default, 24 fps) plus the image options. Quality
    presets: 864×480 in 12 or 20 steps, or 1280×704 in 30. A 5-second clip takes 100 to 150 s on a
    12 GB laptop GPU. Clips are 16:9 (9:16 from an upright start image) unless you give a shape.
  - **Results:** an MP4 with its settings in a JSON file next to it, in the session's gallery; the
    first frame shows in the terminal, `/play` opens it, and `/vary`/`/again` make new clips.
  - **`/animate 3`** in image mode makes a clip from image #3. `lcode media` lists video models, and
    `/models` in video mode picks one (the `video_model` setting).
  - With `notify` on, a long render ends with a desktop notification.

## [0.43.0] - 2026-10-24

### Added

- **Image mode edits pictures** (#152). See [Editing](https://nasser1941.github.io/lcode/modes/#editing).
  - **Edit by instruction** (`/edit 3 make the sky purple`, or `@photo.png make the sky purple`)
    and **references** (`@photo.png @#3 put the mug from image 1 on the table in image 2`), with
    FLUX.2 klein 4B. `/use 3` makes every next request follow an image until `/use off`.
  - **Inpainting** (`--mask mask.png`: only the white part changes) and **outpainting** (`/extend 3
    --ar 16:9`, or by sides: only the new border is painted, the original stays as it was).
  - **`/upscale 3`** (Real-ESRGAN, 2× or 4×) and **`/nobg 3`** (BiRefNet, a transparent PNG), installed
    on first use. `/vary` on an edit makes new edits of the same images.
  - ComfyUI's reasons for refusing a workflow now include its explanation, not just the input's name.

## [0.42.0] - 2026-10-24

### Added

- **Image mode's options, results and follow-ups** (#151). See
  [Image mode](https://nasser1941.github.io/lcode/modes/#image-mode).
  - **Options in a request** (`a red fox --n 4 --ar 16:9 --seed 42 --quality best --style photo`)
    or for the session (`/options n=4 ar=16:9`): how many images, the shape, the seed (counting up
    over several images), a quality preset (0.5, 1 or 2 megapixels), ten style presets, and a
    negative prompt for models that use one.
  - **Numbered results, shown in the terminal:** the picture itself in kitty, Ghostty, iTerm2 and
    WezTerm, and a half-block colour preview elsewhere (the `preview` setting).
  - **Follow-ups:** `/again` (new seeds), `/vary 3` (four variations of image #3), `/gallery` and
    `/open 3`. The gallery and options are saved with the session.
  - `lcode --mode image -p "… --n 2"` reports every image's file and seed.
  - One image is now named `<time>-<prompt>.png` (no `-1`).

## [0.41.0] - 2026-10-23

### Added

- **Image models to choose from, installed by lcode** (#150). lcode's media catalog lists the image
  models it can run, with their size, license, measured speed and whether they fit your GPU.
  **Z-Image Turbo** (photorealistic) joins FLUX.2 klein 4B (fast): 17 s and 3.5 s per 1024×1024
  image on a 12 GB laptop GPU. See
  [Image models](https://nasser1941.github.io/lcode/modes/#image-models).
  - **Choose one:** `/models` in image mode (numbered) or `/model z-image-turbo` switches; the
    `image_model` setting is the default, and sessions keep theirs.
  - **Installed on demand:** a model that isn't installed downloads with your next image, after you
    say yes, or ahead with `lcode media install <model>`. lcode downloads only the missing files
    (models share text encoders and VAEs) into ComfyUI's model folders, checks the disk first,
    verifies each file's SHA-256, and resumes interrupted downloads. `HF_TOKEN` works for gated
    models.
  - **`lcode media`** lists image and video models with what fits and what's installed, and
    `lcode doctor` says how to install the image model when it's missing.

## [0.40.0] - 2026-10-23

### Added

- **Image mode makes images** (#149). In image mode, what you type becomes a picture, made by
  FLUX.2 klein 4B on your GPU in about 8 seconds, with no language model involved. lcode drives
  ComfyUI itself. See [Modes](https://nasser1941.github.io/lcode/modes/#image-mode).
  - **ComfyUI is found or started:** lcode uses a running ComfyUI as it is. Otherwise it starts
    yours in the background when you enter image mode (`comfyui_path`, the `comfy` CLI's folder or
    a usual one). It stops that ComfyUI when you quit, go back to code mode or close the terminal.
    With none, it offers to install ComfyUI with comfy-cli.
  - **Progress, cancel and type-ahead:** the status line shows the step, the time left and your
    place in ComfyUI's queue. Esc or Ctrl+C cancels the job in ComfyUI too. What you type
    meanwhile waits in the next prompt.
  - **Files with their settings:** images go to `.lcode/media/` (the `media_dir` setting), each
    with a JSON file of its prompt, model, seed and size, also stored in the PNG itself. Images
    that finish after a crash are saved the next time.
  - `lcode --mode image -p "…"` makes an image from a script, and `--output json` gives the paths.
    `lcode doctor` reports ComfyUI and the image model.
  - **No Ollama needed for images:** lcode starts in image mode without Ollama or the coding
    model; `/mode code` says what's missing.

### Fixed

- **lcode cleans up when its terminal closes.** A closed terminal or a `kill` used to end lcode
  without stopping what it had started. Now it saves the session and stops its background jobs and
  servers. On Linux, a ComfyUI lcode started also stops if lcode is killed outright.

## [0.39.0] - 2026-10-23

### Added

- **Modes: code, image and video** (#148). `/mode image`, `/mode video` and `/mode code` switch
  what lcode makes. `lcode --mode` starts in one, and the `mode` setting is the default. The
  banner, status bar and prompt show the mode, and sessions keep theirs.
  - **The GPU goes to one model at a time:** switching to image or video mode unloads the coding
    model, and switching back asks ComfyUI to free its memory.
  - **Image and video mode don't generate yet:** they say what's coming (#149–#157) instead of
    sending your words to the coding model. See [Modes](https://nasser1941.github.io/lcode/modes/).

### Changed

- **Code mode makes code only.** The MCP servers that generated images and videos (`comfyui`,
  `xai`) are out of the MCP catalog, and lcode doesn't load them in code mode even if they're
  still in `mcp.json` (it says so once). Image and video generation are moving to their own modes.
- **`/mode` switches code, image and video.** The permission mode is set with `/permissions` now;
  `/mode yolo` and the like still work, with a note. Shift+Tab cycles the permission mode as
  before.

## [0.38.0] - 2026-10-21

### Added

- The screenshot tool can click first. For a Python Qt program, `actions` run before the picture,
  the way a user would: `double_click Camera 1` (a widget by its text) or `double_click 300,200`,
  `click`, `right_click`, `key Escape`, `key ctrl+s`, `type …`, `wait 2`. The result says what each
  step hit. Bugs that only show after a click were out of reach before.
- Qt's warnings are listed at the top of a screenshot's report, taken from everything the program
  printed. In a real run the cause (`setLayout: … which already has a layout`) was there, among 30
  lines of the program's logs, and the model looked past it.

### Fixed

From a real session that "fixed" a double-click feature four times without once looking at the
window, while the user saw a blank screen each time:

- **Windows it never looked at** are sent back once before the answer counts. When a request
  changed a Python file that draws a window (PyQt, PySide, tkinter, Kivy, wx, GTK) and the model
  never took a screenshot or looked at an image, it's told to look (with clicks first, if that's
  where the change shows) or to say plainly that it's unchecked. The real cause was in what Qt
  printed: `setLayout: … which already has a layout`.
- **A rerun that only took a different time** counts as a repeat: the model ran the same tests four
  times in a row (`75 passed in 0.14s`, then `0.12s`…) without the repeat check noticing.
- **A reply lcode drops** for going round in circles is saved, and the message says where, so you
  can read what the model was thinking instead of taking the stop on trust.
- **The checks use the project's own Python.** They ran `python -m …` from PATH, which doesn't have
  the project's dependencies, so a project with a `.venv` "failed" its checks on imports. Now
  `.venv/bin/python` (or `venv`, `env`) runs them.
- **Tests written for pytest get pytest** when the project has no pytest configuration: the tests
  import it, or a requirements file lists it. They used to get `unittest`, which can't run them.

## [0.37.0] - 2026-10-20

### Added

- Ollama's cloud models (tags ending in `cloud`, such as `minimax-m3.1-flash:cloud`) work as the
  main model or as the stronger model when stuck, and lcode says what they are: what it sends goes
  to ollama.com and is billed per token. The stronger-model panel said "free, on this machine" for
  them; it now says ollama.com and shows the cost. A request Ollama refuses because it isn't signed
  in says to run `ollama signin` (in Docker, with your container's name), and lcode skips the
  local loading and GPU checks for them. The docs explain the setup and list the cloud models that
  call tools, with prices and published coding scores: see
  [Cloud models through Ollama](https://nasser1941.github.io/lcode/models/#cloud-models-through-ollama).

## [0.36.0] - 2026-10-18

### Changed

- `/models` and `/model` show a numbered list of models, and you switch by typing a number: no
  second `/model <name>`. `/model 2` switches straight away, and a name still works. The list ends
  with the other Ollama models you've installed that can call tools, and a model that isn't
  installed says how to get it.
- `/context` says why it stops at the model's largest window (256K for qwen3.6-35b): Ollama caps the
  window at what the model was trained on, whatever the memory, so 512K or 1M would only look larger.

## [0.35.1] - 2026-10-15

### Fixed

Found in a real session that spent an hour on an Android build whose downloads this machine
couldn't reach, then ended without a word:

- **A site this machine can't reach** now stops the guessing. lcode tracks, per site, the calls that
  failed to reach it (HTTP errors, names that don't resolve, refused or timed-out connections) and
  the ones that got an answer. After three different failed calls and none that worked, the model is
  told to check one address that must exist and, if that fails too, to tell you what fails and what
  you could do. Two more and lcode stops the request (status `blocked`). Before, every new address
  looked like a new attempt, so nothing stepped in.
- **Thinking in circles** is stopped as it streams. A model that repeats the same few paragraphs
  used to think until the context was full (here 70,000 characters, about ten minutes behind the
  spinner). Now the reply is dropped once its sentences keep repeating, and the model is asked for
  its next step or what's blocking it.
- **A reply with no answer** no longer ends the request as if it were done. An empty reply, or one
  that filled the context before it said anything, gets a second chance; the second time lcode stops
  (status `loop`) and offers the stronger model first, when one is set. An answer the context cut off
  midway now says so. OpenAI-compatible servers report a cut-off reply too.
- **A `cd` out of the project** no longer moves lcode there. A model that ran `cd /tmp` took the
  session, the project checks and the memory notes with it, and the session was saved under `/tmp`,
  so `lcode -c` in the project couldn't find it. Now a `cd` within the project is kept, and one out
  of it isn't: the next command starts in the project again. Sessions are saved under the folder
  lcode works in.
- **Scripts it never ran** are sent back once before the answer counts: a model that wrote
  `build_android.sh` and called the job done never ran it, and its first line failed. It now runs it
  and fixes what fails, or says plainly that it's untested and why.

## [0.35.0] - 2026-10-14

### Added

- A stronger model when stuck: with `escalate_model` set (a cloud model with your API key, such as
  `anthropic/…`, `openai/…` or `xai/…`, or a bigger local one, `ollama/qwen3.8-27b`), lcode
  offers to ask it for advice when the loop breaker would stop a request or the checks fail twice
  in a row, and `/escalate` asks at any time. Nothing is sent without asking, in any permission
  mode: you see why, what goes (your requests, the task sheet, the failure, the last steps and the
  files in question, never the whole session), where, and the cost (`escalate_price`), and can
  read it all first. Keys come only from environment variables and go only to their provider.
  `lcode -p --escalate` allows it for one run, and `lcode bench --retry --escalate MODEL`
  measures it. Measured on a 12 GB laptop with qwen3.6-35b and the bigger local qwen3.8-27b as
  the advisor, on `runtime-bug` with a second try: 1 of 3 passed with its advice, as without it
  (6 of 9 hidden-test problems fixed against 4), at about 9 minutes per question. A local advisor
  helps a little; a cloud model is the one to try. See
  [A stronger model when stuck](https://nasser1941.github.io/lcode/usage/#a-stronger-model-when-stuck).

### Fixed

- Long command output can't fill the disk any more. lcode keeps the full text of long outputs so the
  model can read parts of them, but a command that printed gigabytes was saved whole, and the
  saved outputs had no total limit, only an age (7 days). Now one is kept to its first 20 MB (the
  model still gets the end and the error lines), and all of them together to 200 MB, oldest first.

- A screenshot of a web app no longer starts its server a second time when one already answers at
  the URL (one the model started itself, say), which used to fail on the busy port and put the
  error in the result. A page that answers also counts when the server command itself exits.

## [0.34.0] - 2026-10-14

### Added

- Screenshots without a screen: a `screenshot` tool runs a desktop program or opens a web page the
  way you would see it, saves the shot in `.lcode/screenshots/`, and returns what it shows with the
  sizes behind it. For a Python Qt program, that's each window against the screen, the size its
  layout needs, and the widgets that stick out or overlap. For a web page or a local server
  (headless Chrome), it's the page's size, what scrolls sideways, and the console errors and
  failed requests. Other desktop programs run on a virtual display (Xvfb). See
  [Screenshots](https://nasser1941.github.io/lcode/usage/#screenshots).

### Fixed

- In a project with a virtual environment (`.venv`, `venv` or `env`), the model is told to run its
  Python (`.venv/bin/python`) rather than plain `python`, which lacks the project's packages. Found
  when the model checked a PySide6 app with the system Python, failed, and stopped checking.

## [0.33.1] - 2026-10-14

### Changed

- A new logo, on GitHub, PyPI and the docs site (header, favicon, home page and link previews),
  with a light version for dark backgrounds. `scripts/make_logos.py` makes every size from the
  original.

### Fixed

- The README's images and links work on PyPI: the logo, the demo and the links to the license
  and contributing guide were relative, so PyPI showed them broken.
- The source package is half the size (0.6 MB): it no longer carries the docs' images.

## [0.33.0] - 2026-10-14

### Added

- Steer while it works. Type while a request runs and press Enter: the line goes to the model at
  its next step, as a note from you, without interrupting anything. The spinner shows what you're
  typing and the notes waiting. Esc pauses after the current step, so you can look, then go on with
  or without a note. Notes count as your requests for summaries and the review, and anything that
  never reached the model goes back into the prompt. A command that runs for a while now shows a
  spinner with its elapsed time. See
  [Steer while it works](https://nasser1941.github.io/lcode/usage/#steer-while-it-works).

### Fixed

- "Where you left off" (`lcode -c`) and session titles showed lcode's own messages to the model,
  such as a failed check, as if you had written them. They show your last request now.

## [0.32.0] - 2026-10-14

### Added

- A review before the answer. After a request that changed files, the model checks its change
  against your request, from the diff and its own answer, and real problems (part of the request
  not done, an untested claim, a leftover debug print) go back to it once. One line says what the
  review found. It's off by default: measured with qwen3.6-35b on a 12 GB laptop, it made requests
  73% slower for one real catch. Turn it on with `lcode config set review auto`. `lcode -p --output
  json` reports it in `review`, and `lcode bench --review` compares. See
  [A review before the answer](https://nasser1941.github.io/lcode/usage/#a-review-before-the-answer).

## [0.31.0] - 2026-10-14

### Added

- A task sheet that survives long sessions. The plan (the todo list), the model's notes (decisions,
  findings, open questions, kept in `todo_write`'s new `notes`, or inside a todo item, where small
  models tend to put them), the files changed and the last check are pinned next to the summary and
  next to your request when old output is removed. It's saved with the session, so `lcode -c`
  brings it back (the todo list used to be lost), and `/status` shows it. See
  [Context management](https://nasser1941.github.io/lcode/how-it-works/#context-management).

## [0.30.0] - 2026-10-13

### Added

- Edits that the model copied imperfectly land where they belong instead of failing: line numbers
  copied from `read_file`, stray escapes, a block at the wrong indentation or spaces for tabs (the new
  text is re-indented), dropped blank lines, and very close matches. A close match changes only the
  lines the model meant to change, so an earlier edit its copy doesn't show survives. Ambiguous or
  weak matches are refused, and the model is shown the closest lines with their numbers. See
  [Imperfect edits](https://nasser1941.github.io/lcode/usage/#imperfect-edits).
- `apply_patch`: the model can apply a unified diff to several files at once. Hunks are found by
  their text, so wrong `@@` line numbers don't matter, and a file changes only if every hunk fits.
- Common tool slips get a useful answer instead of an error: a `grep` pattern that isn't a valid regex
  (`def add(`) is searched as plain text, a path that doesn't exist comes back with the closest
  existing ones (`Did you mean: src/pkg/math_utils.py?`), `read_file` on a folder lists it, and
  `list_dir` on a file says to read it.
- `lcode bench --json` breaks failed tool calls down by tool (`errors_by_tool`).

## [0.29.0] - 2026-10-13

### Added

- lcode runs the project's checks before calling work done. After a request that changed files, it
  runs the tests (pytest, unittest, npm/pnpm/yarn/bun test, cargo test, go test or make test, found
  from the project, or the command in `.lcode/settings.toml` or the `checks` setting). When they
  fail, the model gets the output and keeps working, twice at most, and then says what still fails.
  Checks the model already ran after its last change aren't repeated. `lcode -p --output json`
  reports them in `checks`, and `lcode bench --checks off` compares. See
  [Checks before done](https://nasser1941.github.io/lcode/usage/#checks-before-done).

### Fixed

- A tool call that Ollama couldn't read because its XML was out of order (`expected element type
  <function> but have <parameter>`) ended the request with an error. Like other malformed calls,
  it's now sent back to the model to make again.

## [0.28.1] - 2026-10-13

### Fixed

Found by testing lcode's tools on real files and commands (#118):

- Some commands that change things ran without asking, because they were taken for read-only:
  `sort -o`, `uniq in out`, `tree -o`, `git branch -D` and other branch changes, `git remote
  add/remove`, `git log/diff --output`, `rg --pre` (which runs a program), `find -fls`, `date -s`,
  and `nvidia-smi` settings. Arguments are now checked. `2>&1` and `>/dev/null` no longer make a
  read-only command ask.
- "Always allow" for a command also covered chains that started with it: allowing `npm test` let
  `npm test && rm -rf build` run without asking. Now a chain needs its own "always".
- A JSON tool call that the model only showed as an example in its answer was run. Text is now
  taken as a call only when the whole message is one.
- A change you made to a file while lcode asked to edit it was overwritten. lcode now notices and
  doesn't apply the edit, and the model redoes it on the current content.
- `replace_all: "false"`, and other values sent as text, were taken as true. They now get the
  type the tool declares.
- The shell timeout wasn't enforced while a command kept printing, so a watcher started without
  `background` hung the request. Long output is no longer held in memory either: 400 MB of output
  used to cost 2.5 GB, and now it goes to a file, keeping its start, end and error lines.
- One tool result could overflow the context window: `read_file` returned up to ~40K tokens,
  however small the window, and compaction only looked at the previous request. A result now takes
  at most a quarter of the window (`read_file` says where to read on), and lcode makes room based on
  the size of the next request.
- Sessions were saved only when a request ended, so a crash lost the whole request. They're now
  saved after every step, and atomically, so a crash while writing can't corrupt them.
- Editing a file with Windows line endings turned the whole file to Unix line endings, and editing a
  file that isn't UTF-8 (Windows-1252 or Latin-1) replaced every accented letter in it with `�`,
  not only in the edited line. Files now keep their encoding, line endings and byte order mark. A
  character the encoding can't hold is refused instead of written wrong.

## [0.28.0] - 2026-10-13

### Added

- lcode says what's happening under the hood, in a line, only when it happens:
  - loading the model and then reading the conversation are named in the status line instead of
    "Thinking";
  - after a load, how long it took and how much of the model is on the GPU, with a warning when less
    is than usual;
  - at startup and on `/model`, a warning when a dense model won't fit on the GPU and will be slow,
    with what to use instead;
  - the vision model making the main model step aside, and the reload after it;
  - an image tool taking the GPU;
  - clearer explanations when Ollama runs out of memory.

  The status bar shows when the model is still loading at startup. See
  [What's happening under the hood](https://nasser1941.github.io/lcode/usage/#whats-happening-under-the-hood).

### Fixed

- After `/model`, images were still described by the vision model chosen for the previous model.
- The tests no longer write into your lcode state folder; one of them had left about a hundred
  entries in `extensions-approved.json`.

## [0.27.0] - 2026-10-10

### Changed

- Long sessions are summarized earlier: once the conversation reaches 64K tokens (`compact_at`),
  even when the window is larger. Before, a 256K window was summarized only at about 220K, far past
  where small models work well. lcode still removes old tool output first, and summarizes only if
  that's not enough. `lcode config set compact_at 0` keeps only the 85% rule, and
  `lcode bench --compact-at` compares ceilings. Measured with qwen3.6-35b: making room earlier
  doesn't cost tasks (21 of 24 either way) and saved 17% of the time. See
  [Context management](https://nasser1941.github.io/lcode/how-it-works/#context-management).
- Summaries keep your latest requests (up to eight) word for word, so the goal survives the
  retelling. `/context` shows where the session makes room.

### Fixed

- After lcode removed an earlier file read to make room, the model sometimes answered questions
  about that file from memory, and got them wrong, instead of reading it again. The files whose
  only copy went are now named next to the request it's working on, and the note in their place
  says plainly that the content is gone. A conversation that was just summarized isn't summarized
  again until it has grown by a quarter of `compact_at`, which must be at least 16K (or 0).

## [0.26.0] - 2026-10-10

### Added

- lcode checks for secrets before the model runs `git commit` or `git push`: passwords, keys and
  tokens in code or configuration, credentials in URLs (`rtsp://user:pass@…`), private keys,
  tokens with a known form, and `.env` files, in the lines a commit adds or the commits a push
  sends. In `ask` mode you decide; in `auto-edit` and `yolo` the command doesn't run, and the model
  is told to move the secret out of the code. `/commit` and `/pr` show the findings before you
  confirm, and the GitHub Action doesn't commit at all. `lcode: allow-secret` in a comment allows a
  line; `secret_check = false` turns the check off. See
  [Secrets in commits](https://nasser1941.github.io/lcode/usage/#secrets-in-commits).

## [0.25.0] - 2026-10-10

### Changed

- The model is told what counts as checking its work: run what it changed the way it will be used,
  reproduce each reported problem before fixing it, check what it can't see (GUIs, web pages,
  layouts) without a screen, for example with Qt's `offscreen` platform, a headless browser or a
  rendered image, and never call something tested or done without having run it. See
  [Checking its own work](https://nasser1941.github.io/lcode/how-it-works/#checking-its-own-work).

### Added

- `lcode bench --tasks runtime-bug`: a bug that shows only when the code runs, a camera wall whose
  window grows with every video frame. Reading the code suggests band-aids that fail its hidden
  test. It isn't in the default set.

## [0.24.0] - 2026-10-10

### Added

- Ctrl+V in lcode's prompt pastes a screenshot: lcode reads the clipboard (with `wl-paste` on
  Wayland, `xclip` on X11, `pngpaste` or `osascript` on macOS), saves the image and attaches it as
  `@path`. Other clipboard content is pasted as text. `lcode doctor` shows whether it works here.
- Image paths in a message are attached without `@`: absolute and `~` paths, quoted paths with
  spaces, and `file://` paths dragged into the terminal. `@"path with spaces"` works for any file.

### Fixed

- An image path that doesn't exist is no longer ignored: lcode says so, and tells the model it can't
  see that image, so it asks instead of guessing what the picture shows. See
  [Images](https://nasser1941.github.io/lcode/usage/#images).

## [0.23.0] - 2026-10-10

### Added

- lcode notices when the model goes round in circles, making the same tool call with the same result
  again and again, as small models sometimes do. The third time in a request (the second, for a call
  that failed) the result tells the model so; two more, and lcode ends the request (status `loop`,
  exit code `4` with `lcode -p`). Results that change, and checks run after an edit, don't count.
  `repeat_limit` changes the threshold; `0` turns it off. See
  [When the model repeats itself](https://nasser1941.github.io/lcode/how-it-works/#when-the-model-repeats-itself).

### Fixed

- A tool call Ollama can't parse because its XML is broken (`XML syntax error … unexpected EOF`, from
  models such as Qwen3.6 that write their calls in XML) is now sent back to the model to make again,
  like a call with broken JSON, instead of ending the request with an error.

## [0.22.0] - 2026-10-06

### Added

- `lcode mcp add xai`: images, image edits, videos and speech with xAI's Grok Imagine and text to
  speech APIs, in the cloud and billed to your xAI account. It asks for your xAI API key, and runs
  `lcode-mcp-xai`, a new MCP server that ships with lcode, with the tools `generate_image`,
  `edit_image`, `generate_video` (waits for the video and downloads it), `video_status` and
  `text_to_speech`. Files are saved in `generated/` in the project or where you ask. See
  [Images, videos and speech with xAI](https://nasser1941.github.io/lcode/mcp/#images-videos-and-speech-with-xai).

## [0.21.0] - 2026-10-06

### Added

- `lcode update` updates lcode the way it was installed (uv tool, pipx, Homebrew, pip, or
  `git pull` for a checkout), and Ollama when it runs in Docker. The newest Ollama release comes
  from Docker Hub, not a local `latest` tag, and is pulled by its version tag (such as
  `ollama/ollama:0.35.1`) so `docker ps` shows the version. A container from Docker Compose gets
  the new tag in its compose file and is recreated with `docker compose up`; any other container is
  recreated with the same settings, and the old one comes back if the new one doesn't start.
  `lcode update --check` only shows which versions are out. See
  [Updating](https://nasser1941.github.io/lcode/installation/#updating).

## [0.20.0] - 2026-10-06

### Added

- Background commands: the model starts a dev server, a watcher or another process that keeps
  running with `bash` and `background: true`, reads its new output with `bash_output` and stops it
  with `bash_stop`. `/jobs` lists them, and they all stop when the session ends.
- Desktop notifications (`notify-send` on Linux, `osascript` on macOS, else the terminal bell)
  when a request that ran longer than 30 seconds is done, or lcode waits for your answer during
  one. Settings: `notify` and `notify_after`.

### Fixed

- A command that starts something with `&` no longer hangs until its timeout while that process
  holds the output open. What it left running is stopped when it ends, and the model is told to
  use a background command instead.

## [0.19.0] - 2026-10-06

### Added

- `lcode acp`: lcode inside editors that speak the Agent Client Protocol (Zed, JetBrains IDEs,
  Neovim, Emacs and others). The editor shows the answers and thinking as they stream, each tool
  call with the files it touches and a diff for edits, and asks permission questions and plan
  approvals in its own dialog. lcode's permission modes are the session's modes, the todo list is
  the editor's plan, and saved sessions reopen with their conversation. The model reads the
  editor's buffers (unsaved changes included) and edits through them, and MCP servers the editor
  passes join lcode's own. See [Editors](https://nasser1941.github.io/lcode/editors/) for setup.

### Fixed

- Cancelling a request whose answer was cut off mid-stream no longer ends it as if it were done.

## [0.18.0] - 2026-10-06

### Added

- `lcode -p "…" --output json` prints one result object (status, answer, tool calls, files
  changed, token usage); `--output stream-json` prints an event per model step and tool result,
  then the result. `--max-steps` and `--allowed-tools` limit a run, and the exit code says how it
  ended. Without a person to ask, anything that needs permission is refused.
- A Python API: `from lcode import Session`, with `run()`, `stream()` and an `approve` callback
  for permission requests. See `examples/api/fix_tests.py`.
- A GitHub Action for self-hosted runners (`uses: nasser1941/lcode@v0.18.0`). It answers `@lcode`
  in issues and pull requests from the repository's owners, members and collaborators, proposes
  changes as a pull request (or pushes them to the pull request's branch), and can review pull
  requests. See [Automation](https://nasser1941.github.io/lcode/automation/).

## [0.17.0] - 2026-10-06

### Added

- `/commit` writes a commit message in the repository's style, from the diff, the conversation
  and recent commits, and commits once you approve it. It commits what you staged, or every
  change except files the model thinks are unrelated and files that may hold secrets.
- `/review [base]` reviews the uncommitted changes, or the branch against a base, and reports
  findings with `path:line`. The model can read but not change anything during a review.
- `/pr [base]` pushes the branch and opens a GitHub pull request with `gh` after you approve the
  title and description; commits made on `main` itself go to a new branch.
- A command of your own called `commit`, `review` or `pr` replaces the built-in one.
- `lcode --worktree [NAME]` (`-w`) runs the session in a git worktree on its own branch, so
  several sessions can work on one repository at once. It's removed at the end if nothing
  changed. See [Git workflow](https://nasser1941.github.io/lcode/git/).

### Fixed

- With another model server, `/ctx` no longer says the model reloads: it changes how much of the
  server's context window lcode uses.

## [0.16.0] - 2026-10-05

### Added

- Other model servers: LM Studio, llama.cpp's llama-server, vLLM, MLX's server, or any other
  server with an OpenAI-compatible API, instead of Ollama. `lcode config set backend lmstudio` (or
  `llama.cpp`, `vllm`, `mlx`, `openai` with `base_url`); a key can come from `LCODE_API_KEY`. Tool
  calls, streaming, reasoning (on and off), images and structured answers work as with Ollama.
  lcode reads the context window from the server, matches the model setting to the models it
  serves, and moves thinking that a server sends as part of the answer out of it. `lcode doctor`,
  `lcode models`, `lcode bench` and `lcode index` work with every backend. See
  [Other model servers](https://nasser1941.github.io/lcode/servers/), with setup for each.

## [0.15.0] - 2026-10-05

### Added

- Hooks: shell commands that run on lcode's events (`before_tool`, `after_tool`, `after_request`,
  `session_start`, `notification`), filtered by tool and file, with the event as JSON on stdin. A
  `before_tool` hook that exits with code 2 blocks the call and tells the model why; an
  `after_tool` hook's output reaches the model when it fails (a linter) or with `feedback = true`.
  Notification hooks run when lcode waits for you and when a long request is done.
- Permission rules: `[permissions]` allow and deny lists for shell commands, file edits, web
  domains and MCP tools, checked before lcode asks. Deny always wins, even in `yolo` mode and for
  read-only commands, and is enforced for subagents too.
- Both go in `config.toml` or a repository's `.lcode/settings.toml`, which is used only once
  approved (and approved again after any change). `lcode doctor` lists them. See
  [Hooks and permission rules](https://nasser1941.github.io/lcode/hooks/), with recipes for ruff,
  prettier, blocking force-pushes, running tests and desktop notifications.

### Fixed

- `lcode config set` no longer drops tables you added to `config.toml` by hand.

## [0.14.0] - 2026-10-05

### Added

- A repository map: the important files with their classes and functions, signatures and line
  numbers, ranked by how much of the code uses them and cut to a token budget. Small repositories
  (up to 300 source files) get it in the model's instructions; larger ones get a `repo_map` tool,
  which can zoom into a folder. Python is read with its own parser; TypeScript, JavaScript, Go,
  Rust, Java and Kotlin by their declarations. Measured with qwen3.6-35b, it halved the steps the
  model took to find code and cut the tokens it read by 39%. The `repo_map` setting turns it off.
- Semantic code search: with an embedding model in Ollama (qwen3-embedding, nomic-embed-text, …),
  `lcode index` indexes the repository, and the model gets a `search_code` tool that finds code by
  what it does. Changed files are embedded again before each search. During a session the
  embedding model runs on the CPU, so it never pushes lcode's model off the GPU. The
  `embed_model` setting picks the model or turns it off.
- `lcode bench --tasks find-concept` (a task in a larger repository), `--repo-map` and
  `--code-search`. See [Repository map and code search](https://nasser1941.github.io/lcode/search/).

## [0.13.0] - 2026-10-05

### Added

- Code intelligence from language servers. With one installed (basedpyright or pyright for Python,
  typescript-language-server or TypeScript 7's `tsc --lsp`, gopls, rust-analyzer, clangd), the model
  gets an `lsp` tool for a symbol's definition, references and type, and a file's or the workspace's
  symbols; and after every edit to a file in that language, the errors the edit introduced are
  added to the result, so the model fixes them right away. Servers start on demand, one per
  language; a project's own `node_modules/.bin` comes first. Without any server installed nothing
  changes. `lcode doctor` shows what it found; the `lsp` setting and `~/.config/lcode/lsp.json`
  configure it. See [Code intelligence](https://nasser1941.github.io/lcode/lsp/).

## [0.12.0] - 2026-10-05

### Added

- `/context` shows what uses the context window, by category: the system prompt and its parts
  (`AGENTS.md`, memory notes, skills, MCP servers), tool definitions, your messages, attached files,
  the model's replies and each tool's results.
- `lcode bench --session` runs all tasks in one conversation, to test long sessions and context
  management; `--rounds N` repeats the tasks and `--no-prune` turns the removal of old tool output
  off, to compare.

### Changed

- Smarter context management, which matters most at 32K. When the window is 85% full, lcode first
  replaces old tool output (file contents, command output, search results from before your last
  two requests, and earlier copies of files that were read again) with one-line notes saying what
  was there, then the output of your previous request if needed; it summarizes the conversation
  only if that doesn't free enough room. Measured on 24-task sessions at 32K: fewer summaries, with
  about as many tasks solved. The `prune` setting turns it off.
- Long command and MCP output keeps its beginning, its end and the lines that look like errors;
  the full output is saved to a file the model can read.

## [0.11.1] - 2026-10-05

### Fixed

- `lcode mcp add encord` failed with "needs the Encord SDK" when lcode itself was installed with
  `uv tool install`: `uvx` reused that installation, which doesn't have the SDK. The Encord and
  Valohai presets now run `uvx --isolated`. If you added either server with 0.11.0, add it again
  (`lcode mcp add encord`) or put `--isolated` first in its `args` in `mcp.json`.

## [0.11.0] - 2026-10-05

### Added

- Ready-made MCP servers for data and ML platforms:
  - `lcode mcp add metabase` connects to the MCP server built into Metabase 60 and later, with browser
    sign-in, so the model sees your data with your Metabase permissions.
  - `lcode mcp add encord` and `lcode mcp add valohai`: those platforms have no MCP server of their own,
    so lcode now ships `lcode-mcp-encord` (projects, datasets, ontologies, labeling progress, tasks and
    labels, built on the Encord SDK) and `lcode-mcp-valohai` (executions with their logs, metrics and
    outputs, pipelines). Both are read-only unless started with `--allow-writes`, which adds
    assigning tasks and setting priorities (Encord) or starting and stopping executions (Valohai), and
    both work with other MCP clients too. See [MCP servers](https://nasser1941.github.io/lcode/mcp/#encord).
- Catalog questions that ask for an address now accept it without `https://`.

## [0.10.0] - 2026-10-05

### Added

- Custom commands: prompt templates in `.lcode/commands/<name>.md` or
  `~/.config/lcode/commands/`, run as `/name args`, with `$ARGUMENTS` and `$1`…`$9`, and an
  optional frontmatter for the description, an argument hint and the tools allowed for the
  request.
- Skills in the [Agent Skills](https://agentskills.io) format, from `.lcode/skills/`,
  `.agents/skills/` and `.claude/skills/` (in the repository and in your home folder). Only their
  names and descriptions are in the system prompt; the model loads a skill's instructions with the
  new `skill` tool when a task matches, and its scripts and reference files when the instructions
  call for them. `/name` starts a skill yourself. `/help` lists commands and skills and where each
  comes from. The `skills` setting limits them to lcode's own folders (`lcode`) or turns them off.
  An example command and skill are in `examples/`. See
  [Commands and skills](https://nasser1941.github.io/lcode/commands/).

### Changed

- A repository's own commands, skills and agents are used only after you approve them; lcode asks
  the first time and again when they change, like a project's `.mcp.json`. Custom agents from a
  repository (new in 0.8.0) used to load without asking.

## [0.9.0] - 2026-10-05

### Added

- Plan mode: a `plan` permission mode (Shift+Tab from `ask`, `/mode plan`, `lcode --plan`, or
  `/plan <request>`) in which the model can read, search, run read-only commands, use the web and
  read-only subagents, but can't change anything. It presents a plan with the new `present_plan`
  tool, and you approve it (in `ask` or `auto-edit` mode), edit it, save it to `.lcode/plans/`, or
  send it back with feedback. An approved plan fills the todo list and is kept through compaction;
  `/plan` shows it. See [Plan mode](https://nasser1941.github.io/lcode/usage/#plan-mode).

### Fixed

- `lcode mcp add` shows the default value in its questions again, such as `AWS region
  [us-east-1]`; the terminal formatting swallowed it.
- The end-of-session "Worth remembering?" list shows each note's type (`[feedback]`, `[project]`, …),
  which the same problem hid.

## [0.8.0] - 2026-10-05

### Added

- Subagents: the model can hand a task to a subagent with its own fresh context through the new
  `agent` tool, and only the subagent's report comes back, so exploring a large codebase or reading
  long output doesn't fill the main context. Built-in types: `explore` and `plan` (read-only) and
  `worker` (all tools). Custom agents are markdown files in `.lcode/agents/` or
  `~/.config/lcode/agents/`; `@name` asks for one. Subagents use the session's model and context
  window (no reload), permissions (prompts are labelled with the agent), sandbox and checkpoints
  (`/undo` covers their changes). With `max_parallel_agents` above 1 and Ollama's
  `OLLAMA_NUM_PARALLEL`, several run at the same time; workers that do get their own git worktree
  and their changes come back as a diff to approve. `/agents` shows the types and what each
  subagent did; `lcode doctor` estimates how many could run at once. See
  [Subagents](https://nasser1941.github.io/lcode/agents/).

### Changed

- A shell command that starts with `cd <folder> &&` counts as read-only when the rest of it does,
  so `cd src && grep -n foo *.py` runs without asking. A `cd` with `$(…)` or backticks still asks.

## [0.7.0] - 2026-10-05

### Added

- Memory across sessions: lcode keeps short notes that later sessions load, only key knowledge and
  not whole conversations. The model saves them with its new `memory` tool (and updates or deletes
  them), you add them with `/remember`, and when a session ends (on quit, `/clear` or before
  compaction) one quick question asks the model what from it is worth remembering. Notes are
  per repository, shared by its git worktrees, plus a few for every repository; they're plain
  markdown files, managed with `/memory`. The index in the system prompt is capped at about 2,000
  tokens. Notes that look like they contain secrets are never saved. The `memory` setting is `ask`
  (confirm each note, the default), `auto` or `off`; `--no-memory` turns it off for a session.
  See [Memory](https://nasser1941.github.io/lcode/memory/).

## [0.6.1] - 2026-10-01

### Fixed

- Tools marked `free_gpu` (such as ComfyUI's `run_workflow`) now always run to completion
  (`wait: true`): a job left running in the background competed with lcode's reloading model for
  the GPU.

## [0.6.0] - 2026-10-01

### Added

- lcode can look at images: attach a screenshot, mockup or diagram with `@path`, and the model can
  open images itself with the new `view_image` tool. A model that can see describes the image in
  detail (all text transcribed, with your question in mind): your model itself if it can see, or
  the original model behind lcode's text-only variant, which `lcode setup` already downloaded
  (`vision_model` setting; `lcode doctor` shows which). Screenshots returned by MCP tools, such as
  Playwright's, are described too.
- `lcode mcp add comfyui`: generate and edit images with models you run locally in ComfyUI (FLUX,
  SDXL, Stable Diffusion 1.5, Qwen-Image), through ComfyUI's official MCP server, with only its local
  tools enabled.
- MCP servers can mark tools that need the GPU to themselves (`free_gpu` in `mcp.json`): lcode
  unloads its own model before they run and reloads it afterwards. On for ComfyUI's generation tools,
  so image models work on a 12 GB GPU next to a large coding model.

## [0.5.0] - 2026-10-01

### Added

- An optional sandbox for shell commands: with `lcode --sandbox` (or `sandbox = "docker"` /
  `"podman"`), the model's commands run in a container that only sees the project folder, as your
  user, with no network access unless you allow it (`/sandbox network on`, `sandbox_network`), no
  Linux capabilities and a process limit. The file tools are limited to the project too. lcode builds
  a default image with Python, Node.js, git and build tools on first use (`sandbox_image` for your
  own). In `auto-edit` mode, sandboxed commands run without asking. If the sandbox can't start, lcode
  stops instead of running commands without it.

## [0.4.1] - 2026-10-01

### Added

- Homebrew: `brew install nasser1941/tap/lcode` on macOS (Apple Silicon and Intel) and Linux. The
  [tap](https://github.com/nasser1941/homebrew-tap) tests the formula on both and updates it
  automatically for each release, about a day after it's published.
- Python 3.14 is supported and tested.

## [0.4.0] - 2026-10-01

### Added

- MCP (Model Context Protocol) servers: the model can use tools from Jira, GitHub, AWS, databases and
  more, with your approval for every call.
  - `lcode mcp add <name>` sets up a ready-made server and tests it: `atlassian`, `aws`,
    `aws-knowledge`, `google-drive`, `grafana`, `gcp`, `github`, `playwright`, `context7`, `sentry`,
    `postgres`, `kubernetes`, `linear` and `notion` (`lcode mcp catalog` lists them).
  - Any other server works too, local (stdio) or remote (Streamable HTTP), configured in
    `~/.config/lcode/mcp.json` in the standard `mcpServers` format, or per project in `.mcp.json`
    (used only after you approve it).
  - Browser sign-in (OAuth 2.1 with PKCE and dynamic client registration) with automatic token
    refresh; `lcode mcp login/logout`.
  - Speaks both the current MCP protocol (2026-07-28) and the earlier `initialize`-based versions.
  - When the tool definitions would take more than 15% of the context window, the model finds tools
    on demand instead (`mcp_tools` setting). `/mcp` shows servers, tools and their context cost;
    `lcode --no-mcp` starts without them.

### Fixed

- `lcode bench`: stopping a run with Ctrl+C no longer leaves the running task's temporary folder
  behind, and the summary marks the model as stopped.
- `lcode bench` no longer lists the model it's about to benchmark as "already loaded".

## [0.3.1] - 2026-09-30

### Added

- `lcode bench` scores models on this machine with eight small coding tasks: fixing a bug, finding
  code, writing a script from a spec, renaming across files, a one-line edit in a long file,
  recovering from failing commands, fixing a function to match its spec and adding a feature across
  files. Each task runs in a temporary folder and has an automatic check, some with hidden tests.
  It reports tasks passed, time, generation and prompt speed, tool-call errors and memory use.
  `lcode bench model-a model-b` compares models, `--json` saves the results (a documented, versioned
  format) and `--markdown` prints a table for a model test report.

## [0.3.0] - 2026-09-30

### Added

- `/undo` takes back the file changes of the last request: lcode saves a checkpoint before the model
  first changes files in a request, and restores edited and deleted files and removes new ones,
  including changes made by shell commands. `/undo` again goes further back, `/rewind N` goes back
  to before request N (optionally removing those requests from the conversation too), and
  `/checkpoints` lists them. Checkpoints live in a separate git repository in lcode's state folder,
  so your own repository is never touched, and work in folders that aren't git repositories.
  Turn them off with `lcode config set checkpoints false`.

## [0.2.2] - 2026-09-30

### Added

- `/context` now lets you change the context window mid-session: it lists the sizes the model
  supports with the memory each needs and whether it fits on the GPU, and you pick one (or type
  `/context 128k`). If the conversation is too long for a smaller size, lcode summarizes it first.
  `/ctx` is a shortcut for the same command.

## [0.2.1] - 2026-09-29

### Fixed

- File edits and writes failed with "IndexError: list index out of range" in the default `ask`
  permission mode, a regression in 0.2.0. `auto-edit` and `yolo` modes were not affected.
- Slow models no longer look stuck: the status line keeps counting the elapsed time while the model
  thinks, answers or silently writes a long file into a tool call, and reminds you that Ctrl+C stops it.

## [0.2.0] - 2026-09-29

### Added

- Web access for the model: `web_search` finds current information (latest releases, docs, error
  messages) through Ollama web search, Brave Search, Tavily or a self-hosted SearXNG, and `web_fetch`
  reads pages as clean text. On by default; `lcode config set web ask|off` or `lcode --no-web` limits
  it. Search API keys are read from environment variables only. Closes #7.

## [0.1.2] - 2026-09-29

### Added

- All eight catalog models are now tested end to end (`qwen3.8-27b`, `qwen3.6-27b`, `laguna-xs-2.1`,
  `nemotron-3.5-lightning` and `gpt-oss-20b` joined the three tested before), with a results table
  in the models guide. `laguna-xs-2.1`'s cache size is now measured rather than estimated.
- When a model fails to load because its context doesn't fit in GPU memory, lcode retries with half the
  context and remembers the size that worked for that model (`~/.local/state/lcode/limits.json`, shown
  by `lcode doctor`), so later sessions don't fail first. Found with `nemotron-3.5-lightning`, whose 1M
  context doesn't fit on a 12 GB GPU.
- Named sessions and a session picker: `/rename <name>` names the current session, `/resume` lists
  saved sessions and resumes the one you pick (by number, name, id or title), `/resume all` shows
  every folder, and `lcode --resume [SESSION]` does the same from the shell. Resuming shows a short
  recap of where you left off.

### Changed

- `lcode setup` recommends `gpt-oss-20b` at 64K (instead of `qwen3.5-4b`) for 8 GB GPUs with 16 GB of
  RAM, and prefers `qwen3.5-9b` over `gpt-oss-20b` where both fit.
- lcode is on PyPI as `lcode-cli`; the installer now installs the latest release from PyPI instead of
  the `main` branch.

### Fixed

- `qwen3.6-35b` could crash Ollama with "CUDA error: an illegal memory access" on 12 GB GPUs: its
  prompt batch of 1024 left too little VRAM for the model's speculative-decoding context. The default
  is now Ollama's 512 (1024 remains available with `lcode config set num_batch 1024`), and when the GPU
  runs out of memory before answering, lcode retries automatically with a batch of 512.
- A tool call with invalid JSON arguments no longer aborts the request: lcode tells the model what
  was wrong and lets it try again. Unknown or missing tool arguments are reported with the list of
  valid arguments.
- `lcode -c` continues the most recently *used* session in the folder, not the most recently created
  one. Empty sessions are no longer saved.
- `/rename` saves the session right away, so a session named before its first request shows up in
  `/resume`.
- Typing `exit` or `quit` (without a slash) quits instead of being sent to the model as a request.
- Compacted sessions keep their original title instead of showing the summary header.

## [0.1.1] - 2026-09-28

### Added

- `qwen3.5-9b` and `qwen3.5-4b` are now tested end to end, with measured speed and memory on a 12 GB
  GPU in the docs. `lcode setup` now recommends only tested models on common machines.
- Social preview image for the repository and link previews for the docs site.
- Release workflow that publishes to PyPI with Trusted Publishing.

### Changed

- Memory estimates use ~1 GB of runtime overhead instead of 1.5 GB, matching measurements, so small
  models get larger context windows (e.g. `qwen3.5-9b` at 128K on a 12 GB GPU).

### Fixed

- `lcode --model X` no longer reuses the context window saved for the default model; it picks the
  largest window that fits X. `/model` does the same inside a session.
- `lcode models` fits in 80-column terminals.

## [0.1.0] - 2026-09-28

First public release.

### Added

- Interactive terminal agent with streaming markdown output, reasoning display, `@file` attachments,
  slash commands, a status bar and resumable sessions (`lcode -c`).
- One-shot mode for scripts: `lcode -p "..."`.
- Tools for the model: `read_file`, `write_file`, `edit_file` (with diff previews and a
  whitespace-tolerant fallback), `list_dir`, `glob`, `grep` (ripgrep with a pure-Python fallback),
  `bash` (live output, timeouts, persistent working directory) and `todo_write`.
- Permission modes `ask`, `auto-edit` and `yolo` (Shift+Tab cycles), with a read-only command
  allowlist and "always allow" per command.
- Model catalog with eight open-weight models, hardware detection for Linux + NVIDIA and Apple
  Silicon Macs, and memory-fit estimates that pick the largest context window that fits.
- `lcode setup` (recommend, download and configure a model), `lcode models`, `lcode doctor` and
  `lcode config`.
- Choice of context window per run (`--context 128k`), per session (`/ctx`) or in the config file.
- Text-only model variants that drop unused vision projectors to free GPU memory.
- Automatic conversation compaction when the context window is 85% full, plus `/compact`.
- `AGENTS.md` project instructions and `/init` to generate them.
- One-line installer for Linux and macOS.

[Unreleased]: https://github.com/nasser1941/lcode/compare/v0.50.0...HEAD
[0.50.0]: https://github.com/nasser1941/lcode/compare/v0.49.1...v0.50.0
[0.49.1]: https://github.com/nasser1941/lcode/compare/v0.49.0...v0.49.1
[0.49.0]: https://github.com/nasser1941/lcode/compare/v0.48.0...v0.49.0
[0.48.0]: https://github.com/nasser1941/lcode/compare/v0.47.0...v0.48.0
[0.47.0]: https://github.com/nasser1941/lcode/compare/v0.46.0...v0.47.0
[0.46.0]: https://github.com/nasser1941/lcode/compare/v0.45.0...v0.46.0
[0.45.0]: https://github.com/nasser1941/lcode/compare/v0.44.0...v0.45.0
[0.44.0]: https://github.com/nasser1941/lcode/compare/v0.43.0...v0.44.0
[0.43.0]: https://github.com/nasser1941/lcode/compare/v0.42.0...v0.43.0
[0.42.0]: https://github.com/nasser1941/lcode/compare/v0.41.0...v0.42.0
[0.41.0]: https://github.com/nasser1941/lcode/compare/v0.40.0...v0.41.0
[0.40.0]: https://github.com/nasser1941/lcode/compare/v0.39.0...v0.40.0
[0.39.0]: https://github.com/nasser1941/lcode/compare/v0.38.0...v0.39.0
[0.38.0]: https://github.com/nasser1941/lcode/compare/v0.37.0...v0.38.0
[0.37.0]: https://github.com/nasser1941/lcode/compare/v0.36.0...v0.37.0
[0.36.0]: https://github.com/nasser1941/lcode/compare/v0.35.1...v0.36.0
[0.35.1]: https://github.com/nasser1941/lcode/compare/v0.35.0...v0.35.1
[0.35.0]: https://github.com/nasser1941/lcode/compare/v0.34.0...v0.35.0
[0.34.0]: https://github.com/nasser1941/lcode/compare/v0.33.1...v0.34.0
[0.33.1]: https://github.com/nasser1941/lcode/compare/v0.33.0...v0.33.1
[0.33.0]: https://github.com/nasser1941/lcode/compare/v0.32.0...v0.33.0
[0.32.0]: https://github.com/nasser1941/lcode/compare/v0.31.0...v0.32.0
[0.31.0]: https://github.com/nasser1941/lcode/compare/v0.30.0...v0.31.0
[0.30.0]: https://github.com/nasser1941/lcode/compare/v0.29.0...v0.30.0
[0.29.0]: https://github.com/nasser1941/lcode/compare/v0.28.1...v0.29.0
[0.28.1]: https://github.com/nasser1941/lcode/compare/v0.28.0...v0.28.1
[0.28.0]: https://github.com/nasser1941/lcode/compare/v0.27.0...v0.28.0
[0.27.0]: https://github.com/nasser1941/lcode/compare/v0.26.0...v0.27.0
[0.26.0]: https://github.com/nasser1941/lcode/compare/v0.25.0...v0.26.0
[0.25.0]: https://github.com/nasser1941/lcode/compare/v0.24.0...v0.25.0
[0.24.0]: https://github.com/nasser1941/lcode/compare/v0.23.0...v0.24.0
[0.23.0]: https://github.com/nasser1941/lcode/compare/v0.22.0...v0.23.0
[0.22.0]: https://github.com/nasser1941/lcode/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/nasser1941/lcode/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/nasser1941/lcode/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/nasser1941/lcode/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/nasser1941/lcode/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/nasser1941/lcode/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/nasser1941/lcode/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/nasser1941/lcode/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/nasser1941/lcode/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/nasser1941/lcode/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/nasser1941/lcode/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/nasser1941/lcode/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/nasser1941/lcode/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/nasser1941/lcode/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/nasser1941/lcode/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/nasser1941/lcode/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/nasser1941/lcode/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/nasser1941/lcode/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/nasser1941/lcode/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/nasser1941/lcode/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/nasser1941/lcode/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/nasser1941/lcode/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/nasser1941/lcode/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/nasser1941/lcode/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/nasser1941/lcode/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/nasser1941/lcode/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/nasser1941/lcode/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/nasser1941/lcode/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nasser1941/lcode/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nasser1941/lcode/releases/tag/v0.1.0
