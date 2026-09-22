"""Validated, non-secret defaults for interactive agent sessions.

交互式 Agent 会话使用的非秘密默认配置。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from neuro_code.application.ports.configuration import AppConfig
from neuro_code.application.ports.web_fetch import WebFetchMode
from neuro_code.application.ports.web_search import WebSearchMode
from neuro_code.domain.permissions import validate_verification_command


@dataclass(frozen=True, slots=True)
class AgentPreferences:
    """None inherits configuration; explicit values are user-level TUI overrides."""

    enter_behavior: str | None = None
    prompt_soft_wrap: bool | None = None
    notify_completed: bool | None = None
    notify_failed: bool | None = None
    wake_max_per_session: int | None = None
    wake_cooldown_seconds: int | None = None
    compaction_recent_items: int | None = None
    compaction_summary_tokens: int | None = None
    execution_profile: str | None = None
    max_steps: int | None = None
    failover: bool | None = None
    timeout_seconds: int | None = None
    max_output_tokens: int | None = None
    web_search_mode: str | None = None
    web_fetch_mode: str | None = None
    lsp_enabled: bool | None = None
    show_tool_intent: bool | None = None
    verification_command: str | None = None

    def __post_init__(self) -> None:
        for name, maximum in (
            ("wake_max_per_session", 100),
            ("wake_cooldown_seconds", 86400),
            ("compaction_recent_items", 1000),
            ("compaction_summary_tokens", 4096),
            ("max_steps", 10000),
            ("timeout_seconds", 3600),
            ("max_output_tokens", 1000000),
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 1 <= value <= maximum):
                raise ValueError(f"{name}: 1-{maximum}")
        for name in (
            "failover",
            "lsp_enabled",
            "prompt_soft_wrap",
            "notify_completed",
            "notify_failed",
            "show_tool_intent",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name}: expected boolean")
        for value, enum in (
            (self.web_search_mode, WebSearchMode),
            (self.web_fetch_mode, WebFetchMode),
        ):
            if value is not None:
                enum(value)
        if self.enter_behavior not in (None, "send", "newline"):
            raise ValueError("enter_behavior must be send or newline")
        # This port may import only other ports and the domain, so it validates the
        # shape of the execution profile and leaves the named-profile vocabulary to
        # the application boundary that consumes it (``bootstrap`` builds
        # ``ExecutionProfile``).  The verification-command contract is domain-owned
        # and therefore validated here.
        #
        # 该端口只能导入其他端口与域层,因此这里只校验执行档位的形状,具名档位词汇留给消费它的
        # 应用边界(bootstrap 构造 ``ExecutionProfile``);验证命令契约归域层所有,因此在此校验.
        if self.execution_profile is not None and not isinstance(self.execution_profile, str):
            raise ValueError("execution_profile: expected text")
        if self.verification_command is not None:
            validate_verification_command(self.verification_command)

    def apply_config(self, config: AppConfig) -> AppConfig:
        """Apply only operational preferences, preserving endpoints and credentials."""
        providers = {
            name: replace(
                profile,
                timeout_seconds=self.timeout_seconds
                if self.timeout_seconds is not None
                else profile.timeout_seconds,
                max_output_tokens=self.max_output_tokens
                if self.max_output_tokens is not None
                else profile.max_output_tokens,
            )
            for name, profile in config.providers.items()
        }
        return replace(
            config,
            providers=providers,
            web_search_mode=WebSearchMode(self.web_search_mode)
            if self.web_search_mode is not None
            else config.web_search_mode,
            web_fetch_mode=WebFetchMode(self.web_fetch_mode)
            if self.web_fetch_mode is not None
            else config.web_fetch_mode,
            language_servers={
                name: replace(profile, enabled=self.lsp_enabled)
                if self.lsp_enabled is not None
                else profile
                for name, profile in config.language_servers.items()
            },
        )
