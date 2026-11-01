"""lcode as an agent in Harbor, the harness Terminal-Bench 2.x and SWE-bench Verified run on.

    harbor run -d terminal-bench@2.0 -a lcode.harbor:Lcode -m qwen3.6-35b

Harbor imports this module on your machine, so it needs Harbor installed next to lcode; lcode itself
doesn't depend on it. In each task's container, lcode is installed with uv and solves the task as
one request with your local model (see lcode.harbor_bench).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.options import Env, InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field

from lcode import __version__, harbor_bench


class LcodeOptions(InstalledAgentOptions):
    context: Annotated[str | None, Env("LCODE_CONTEXT")] = Field(
        default=None, description="Context window, e.g. 32k (default: lcode's choice for the model)."
    )
    host: Annotated[str | None, Env("OLLAMA_HOST")] = Field(
        default=None, description="Ollama's address as the task's container sees it (default: your machine)."
    )
    wheel: str | None = Field(
        default=None, description="An lcode wheel on this machine, installed instead of lcode-cli from PyPI."
    )


class Lcode(BaseInstalledAgent):
    """lcode in the task's container, solving the task as one request with the model on your machine."""

    options_model = LcodeOptions

    @staticmethod
    def name() -> str:
        return "lcode"

    def get_version_command(self) -> str | None:
        return f"{harbor_bench.ON_PATH} lcode --version"

    def parse_version(self, stdout: str) -> str:
        return stdout.strip().removeprefix("lcode").strip()

    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("curl",))
        wheel = getattr(self.options, "wheel", None)
        if wheel:
            spec = f"/installed-agent/{Path(wheel).name}"
            await environment.upload_file(Path(wheel), spec)
        else:
            spec = f"lcode-cli=={self._version or __version__}"
        await self.exec_as_agent(environment, command=harbor_bench.install_command(spec))

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        env = {**self.resolve_env_vars(), **harbor_bench.run_env(self.model_name)}
        await self.exec_as_agent(environment, command=harbor_bench.run_command(instruction), env=env)

    def populate_context_post_run(self, context: AgentContext) -> None:
        end = harbor_bench.trace_usage(Path(self.logs_dir) / "lcode")
        if end is None:
            return
        context.n_input_tokens = int(end.get("tokens_in") or 0)
        context.n_output_tokens = int(end.get("tokens_out") or 0)
        context.cost_usd = 0.0
        keys = ("status", "seconds", "steps", "model_calls", "tool_calls", "model_seconds", "tool_seconds")
        context.metadata = {**(context.metadata or {}), "lcode": {key: end.get(key) for key in keys}}
