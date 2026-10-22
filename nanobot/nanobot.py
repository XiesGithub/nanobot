"""High-level programmatic interface to nanobot."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.agent.hook import AgentHook, SDKCaptureHook
from nanobot.agent.loop import AgentLoop


@dataclass(slots=True)
class RunResult:
    """Result of a single agent run."""

    content: str
    tools_used: list[str]
    messages: list[dict[str, Any]]


class Nanobot:
    """Programmatic facade for running the nanobot agent.

    Usage::

        bot = Nanobot.from_config()
        result = await bot.run("Summarize this repo", hooks=[MyHook()])
        print(result.content)
    """

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
    ) -> Nanobot:
        """Create a Nanobot instance from a config file.

        Args:
            config_path: Path to ``config.json``.  Defaults to
                ``~/.nanobot/config.json``.
            workspace: Override the workspace directory from config.
        """
        from nanobot.config.loader import load_config, resolve_config_env_vars
        from nanobot.config.schema import Config

        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Config not found: {resolved}")

        config: Config = resolve_config_env_vars(load_config(resolved))
        if workspace is not None:
            config.agents.defaults.workspace = str(
                Path(workspace).expanduser().resolve()
            )

        loop = AgentLoop.from_config(
            config,
            mail_config_path=resolved,
            image_generation_provider_configs={
                "openrouter": config.providers.openrouter,
                "aihubmix": config.providers.aihubmix,
            },
        )
        return cls(loop)

    async def run(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        hooks: list[AgentHook] | None = None,
        principal: str | None = None,
    ) -> RunResult:
        """Run the agent once and return the result.

        Args:
            message: The user message to process.
            session_key: Session identifier for conversation isolation.
                Different keys get independent history.
            hooks: Optional lifecycle hooks for this run.
        """
        capture = SDKCaptureHook()
        prev = self._loop._extra_hooks
        base_hooks = list(hooks) if hooks is not None else list(prev or [])
        self._loop._extra_hooks = [capture, *base_hooks]
        try:
            security = None
            direct_options = {}
            if self._loop.mail_service is not None:
                direct_options.update(channel="sdk", chat_id=session_key)
            if principal is not None and self._loop.mail_service is not None:
                await self._loop.mail_service.start()
                security = self._loop.mail_service.access.authenticate("sdk", principal, private_chat=True)
                direct_options["security"] = security
            response = await self._loop.process_direct(
                message, session_key=session_key, **direct_options,
            )
        finally:
            self._loop._extra_hooks = prev

        guard = nullcontext()
        if self._loop.mail_service is not None:
            from nanobot.knowledge.mail.delivery import delivery
            guard = delivery(self._loop.mail_service, response)
        with guard:
            content = (response.content if response else None) or ""
            return RunResult(content=content, tools_used=capture.tools_used, messages=capture.messages)

    async def close(self) -> None:
        """Release this SDK instance's query resources (never starts a Worker)."""
        await self._loop.close_mcp()
        if self._loop.mail_service is not None:
            await self._loop.mail_service.close()


