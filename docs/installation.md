# Installation

lcode needs three things: a GPU driver (Linux) or an Apple Silicon Mac, [Ollama](https://ollama.com)
to run the model, and lcode itself. The installer handles lcode; `lcode setup` then picks and
downloads a model that fits your machine.

!!! info "Requirements"

    - **Linux** (Ubuntu 22.04 / 24.04 tested) with an NVIDIA GPU, or **macOS** on Apple Silicon (M1–M4).
      Windows works through WSL2. A GPU is strongly recommended; CPU-only works with small models but is slow.
    - **Ollama 0.30 or newer.**
    - **Disk space** for the model: 23 GB for the default, 4–7 GB for the small models.

## 1. Prepare the machine

=== "Ubuntu (NVIDIA)"

    Install the NVIDIA driver if `nvidia-smi` doesn't work yet, then reboot:

    ```bash
    sudo ubuntu-drivers install && sudo reboot
    ```

    Install curl and ripgrep (lcode uses ripgrep for fast code search):

    ```bash
    sudo apt update && sudo apt install -y curl ripgrep git
    ```

    Install Ollama. It runs as a background service and uses the GPU automatically:

    ```bash
    curl -fsSL https://ollama.com/install.sh | sh
    ```

=== "macOS (Apple Silicon)"

    Install [Homebrew](https://brew.sh) if you don't have it, then Ollama and ripgrep:

    ```bash
    brew install ollama ripgrep
    brew services start ollama
    ```

    You can also use the [Ollama desktop app](https://ollama.com/download) instead of Homebrew; it
    starts the server when you open it. Ollama uses the Mac's GPU (Metal) automatically.

## 2. Install lcode

```bash
curl -fsSL https://nasser1941.github.io/lcode/install.sh | bash
```

The [installer](https://github.com/nasser1941/lcode/blob/main/install.sh) installs
[uv](https://docs.astral.sh/uv/) if needed, installs the latest release of lcode from
[PyPI](https://pypi.org/project/lcode-cli/) with its own Python into `~/.local/bin`, checks that
Ollama is running, and starts `lcode setup`.

!!! tip "Homebrew"

    On a Mac (or with Homebrew on Linux) you can install lcode with Homebrew instead:

    ```bash
    brew install nasser1941/tap/lcode
    brew install ollama && brew services start ollama   # if you don't have Ollama yet
    lcode setup
    ```

    Upgrade with `brew upgrade lcode`. New releases reach the
    [tap](https://github.com/nasser1941/homebrew-tap) about a day after they're published.

??? note "Prefer to do it by hand?"

    lcode is published on PyPI as `lcode-cli` (the command is `lcode`). With
    [uv](https://docs.astral.sh/uv/getting-started/installation/):

    ```bash
    uv tool install lcode-cli
    ```

    Or with [pipx](https://pipx.pypa.io):

    ```bash
    pipx install lcode-cli
    ```

    To try the latest unreleased changes from `main`:

    ```bash
    uv tool install git+https://github.com/nasser1941/lcode
    ```

    lcode needs Python 3.10 or newer. The macOS system Python is too old, which is why uv (it
    brings its own Python) is the recommended route.

## 3. Pick and download a model

```bash
lcode setup
```

`lcode setup` detects your GPU or Mac, recommends the best model and the largest context window
that fits, downloads it (the default model is 23 GB, so this takes a while), and saves your choice.
To choose yourself:

```bash
lcode models                              # the catalog and how each model fits this machine
lcode setup qwen3.5-9b                    # a specific model
lcode setup qwen3.6-35b --context 128k    # a specific context window
```

## 4. Check the installation

```bash
lcode doctor
```

```text
✓ Hardware   NVIDIA GeForce RTX 4080 Laptop GPU (12 GB VRAM), 31 GB RAM
✓ Config     ~/.config/lcode/config.toml
✓ Ollama     http://localhost:11434 · version 0.32.15
✓ Model      qwen3.6-35b → lcode-qwen3.6-35b
✓ Context    256K tokens · ~28 GB needed, ~35 GB available
✓ ripgrep    found
✓ git        found
```

You're ready: `cd` into a project and run `lcode`. Continue with the [quickstart](quickstart.md).

## Other setups

### Ollama in Docker

lcode talks to Ollama over HTTP, so a container works the same way. With the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
installed:

```bash
docker run -d --gpus all --restart unless-stopped -p 11434:11434 \
  -v ollama:/root/.ollama --name ollama ollama/ollama:latest
```

### Ollama on another machine

Run the model on a GPU server and lcode on your laptop:

```bash
lcode config set ollama_host http://gpu-server:11434
```

Your prompts and the code the model reads are then sent to that server. Ollama has no
authentication; only expose it on a network you trust (or through an SSH tunnel).

### LM Studio, llama.cpp, vLLM or MLX instead of Ollama

lcode also works with any server that has an OpenAI-compatible API: `lcode config set backend
lmstudio` (or `llama.cpp`, `vllm`, `mlx`). See [Other model servers](servers.md).

### Windows

Use [WSL2](https://learn.microsoft.com/windows/wsl/install) with Ubuntu and follow the Ubuntu
steps. NVIDIA GPUs work inside WSL2 with the regular Windows driver.

## Updating

```bash
lcode update            # update lcode, and Ollama if it runs in Docker
lcode update --check    # only show which versions are out
```

- **lcode** updates the way you installed it: `uv tool upgrade`, `pipx upgrade`, `brew upgrade`
  or `pip`. If it runs from a git checkout, `git pull` (on `main`, without local changes).
- **Ollama in a Docker container** is updated to the newest release on Docker Hub. lcode looks
  that up instead of trusting a local `latest` tag, which can be months old, and skips release
  candidates. It pulls the version's own tag, such as `ollama/ollama:0.35.1`, so `docker ps` shows
  which version runs. A ROCm image stays ROCm.
  - **A container from Docker Compose:** lcode changes the `image:` line in its compose file and
    runs `docker compose up -d` for that service, so Compose keeps managing it.
  - **Any other container** is recreated with the same name, ports, volumes, environment, GPUs,
    network and restart policy. The old one is kept until the new one answers with the new
    version, and is put back if it doesn't.
  - Your models stay: they're in the container's volume.
- **Ollama installed without Docker** gets the command that updates it: on Linux,
  `curl -fsSL https://ollama.com/install.sh | sh`; on macOS, `brew upgrade ollama` or the app
  itself.

lcode shows what it will do and asks first. `lcode update lcode` or `lcode update ollama` updates
only one; `-y` doesn't ask.

## Uninstall

```bash
uv tool uninstall lcode-cli      # remove lcode (pipx: pipx uninstall lcode-cli; Homebrew: brew uninstall lcode)
rm -rf ~/.config/lcode ~/.local/state/lcode    # remove settings and saved sessions
ollama rm lcode-qwen3.6-35b qwen3.6:35b-a3b-coding   # remove downloaded models
```
