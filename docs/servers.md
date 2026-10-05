# Other model servers

lcode is built around [Ollama](https://ollama.com), but it also works with any server that has an
OpenAI-compatible API: [LM Studio](https://lmstudio.ai),
[llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server`, [vLLM](https://docs.vllm.ai)
and the [MLX](https://github.com/ml-explore/mlx-lm) server. Tool calls, streaming answers, reasoning
and all of lcode's tools work the same.

```bash
lcode config set backend lmstudio      # or llama.cpp, vllm, mlx
lcode doctor                           # checks the server, the model and its context window
lcode
```

| `backend` | Server | Default address |
|---|---|---|
| `ollama` (default) | Ollama | `ollama_host`, `http://localhost:11434` |
| `lmstudio` | LM Studio | `http://localhost:1234/v1` |
| `llama.cpp` | llama-server | `http://localhost:8080/v1` |
| `vllm` | vLLM | `http://localhost:8000/v1` |
| `mlx` | `mlx_lm.server` | `http://localhost:8080/v1` |
| `openai` | Any other OpenAI-compatible API | Set `base_url` |

- **Another address:** `lcode config set base_url http://gpu-box:8000/v1`.
- **A server that needs a key:** put it in `LCODE_API_KEY` (or `OPENAI_API_KEY`). lcode sends it as a
  bearer token and never writes it to the config file.
- **For one run:** `LCODE_BACKEND=llama.cpp lcode`.

Your prompts and the code the model reads go to that server, so use one you run yourself or trust.

## Choosing the model

The `model` setting names a model the server serves. `lcode models` lists them:

```text
$ lcode models
Models served by LM Studio at http://localhost:1234/v1
  google/gemma-4-12b
  qwen/qwen3.6-35b-a3b  current
```

Part of the name is enough when only one model matches (`lcode -m gemma`). A server with a single
model, like llama-server, uses it whatever the setting says. `lcode setup` only installs models
into Ollama: download models in the server itself.

## The context window

With Ollama, lcode chooses the context window and loads the model with it. Other servers load the
model themselves, with their own window, so lcode reads its size from the server:

- llama-server: the `-c` option;
- vLLM: `--max-model-len`;
- LM Studio: the context length the model was loaded with.

lcode then summarizes the conversation before it outgrows that window. The MLX server and Ollama's
own OpenAI endpoint don't report a size, so lcode assumes 32K; set the real size with
`lcode config set context 64k`. A smaller value than the server's is fine (`/ctx 16k`): lcode just
summarizes sooner.

lcode's instructions and tool definitions take about 6K tokens, so give the model at least 32K.

## Setting up each server

### LM Studio

1. Download a model that supports tool use, such as Qwen3.6 35B A3B or Gemma 4.
2. Load it with a context length of 32K or more. LM Studio's default of 4K is too small for a
   coding agent.
3. Start the server: in the Developer tab, or with `lms server start`.
4. Point lcode at it:

   ```bash
   lcode config set backend lmstudio
   lcode models                           # the ids LM Studio serves
   lcode config set model qwen/qwen3.6-35b-a3b
   ```

On a Mac, LM Studio's MLX models are the fastest route. Load the model before you start lcode: a
model that LM Studio loads on request gets LM Studio's default context length.

### llama.cpp (llama-server)

```bash
llama-server -m Qwen3.6-35B-A3B-Q4_K_M.gguf --alias qwen3.6-35b --jinja -c 65536 -ngl 99 --n-cpu-moe 20
lcode config set backend llama.cpp
```

- `--jinja` is required for tool calls.
- `-c` sets the context window, which lcode reads.
- `--alias` gives the model a short name; otherwise its name is the file's path.
- On a GPU too small for the whole model, `--n-cpu-moe N` (or `--cpu-moe`) keeps a mixture-of-experts
  model's expert layers in RAM.

With Docker and an NVIDIA GPU:

```bash
docker run -d --gpus all -p 127.0.0.1:8080:8080 -v ~/models:/models ghcr.io/ggml-org/llama.cpp:server-cuda \
  -m /models/Qwen3.6-35B-A3B-Q4_K_M.gguf --alias qwen3.6-35b --jinja -c 65536 -ngl 99 --n-cpu-moe 20 \
  --host 0.0.0.0
```

### vLLM

```bash
vllm serve Qwen/Qwen3-32B --max-model-len 32768 \
  --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3
lcode config set backend vllm
```

vLLM needs `--enable-auto-tool-choice` and the tool-call parser for the model's family (see vLLM's
[tool calling](https://docs.vllm.ai/en/latest/features/tool_calling.html) page). A reasoning parser
keeps the model's thinking out of its answers.

### MLX (Apple silicon)

```bash
pip install mlx-lm
mlx_lm.server --model mlx-community/Qwen3-30B-A3B-4bit
lcode config set backend mlx
lcode config set context 32k
```

## What's different from Ollama

| | Ollama | Other servers |
|---|---|---|
| Context window | lcode chooses it, and falls back to a smaller one when the GPU runs out of memory | The server's; lcode reads it |
| Reasoning on and off (`/think`, `--no-think`) | Yes | Yes, for models whose chat template has a switch (Qwen and others) on llama-server and vLLM |
| Thinking shown separately | Yes | Yes. When a server sends the thinking as part of the answer, lcode moves it out |
| Images | The model's own vision, or `vision_model` | Set `vision_model` to a model the server serves that can see images |
| [Code search](search.md#semantic-code-search) | Any installed embedding model | Needs the server to serve an embedding model too, as LM Studio can |
| Freeing the GPU for MCP tools that need it | Yes | No: the server decides what stays loaded |
| `lcode bench` memory columns | Measured | Empty: the server doesn't report them |

`lcode bench` works the same way with any backend, so you can compare the same model on two
servers: `LCODE_BACKEND=llama.cpp lcode bench`.
