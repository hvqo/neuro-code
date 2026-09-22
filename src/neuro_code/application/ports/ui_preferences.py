"""Canonical UI-preferences port.

定义规范的 UI 偏好端口."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.shared.ui_theme import UiTheme

if TYPE_CHECKING:
    # The preference value object is only needed for annotations. Importing it
    # at runtime would close an application import cycle:
    # runtime.verification -> sessions.requirements -> application.sessions
    # -> ports.session_history -> application.ports -> ports.ui_preferences
    # -> ports.agent_preferences -> runtime.verification.
    #
    # 该偏好值对象只用于注解;在运行时导入会闭合应用层导入环:
    # runtime.verification -> sessions.requirements -> application.sessions
    # -> ports.session_history -> application.ports -> ports.ui_preferences
    # -> ports.agent_preferences -> runtime.verification.
    from neuro_code.application.ports.agent_preferences import AgentPreferences


class UiPreferencesStore(Protocol):
    """Persist interactive user preferences outside provider and project config.

    在 Provider 配置和项目配置之外持久化交互式用户偏好."""

    async def load_agent_preferences(self, workspace: Path | None = None) -> AgentPreferences: ...

    async def save_agent_preferences(
        self, preferences: AgentPreferences, workspace: Path | None = None
    ) -> None: ...

    async def load_effective_agent_preferences(self, workspace: Path) -> AgentPreferences: ...

    async def load_theme(self) -> UiTheme: ...

    async def save_theme(self, theme: UiTheme) -> None: ...

    async def load_language(self) -> UiLanguage: ...

    async def save_language(self, language: UiLanguage) -> None: ...

    async def load_reasoning_effort(self) -> ReasoningEffort: ...

    async def save_reasoning_effort(self, effort: ReasoningEffort) -> None: ...

    async def load_interaction_mode(self) -> InteractionMode: ...

    async def save_interaction_mode(self, mode: InteractionMode) -> None: ...


__all__ = ["UiPreferencesStore"]
