"""Atomic user-scoped UI preference persistence.

提供用户范围 UI 偏好的原子持久化."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from neuro_code.application.ports.agent_preferences import AgentPreferences
from neuro_code.domain.conversation.interaction_mode import InteractionMode
from neuro_code.domain.conversation.reasoning import ReasoningEffort
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.ui_language import UiLanguage
from neuro_code.shared.ui_theme import UiTheme

_SCHEMA_VERSION = 1


class JsonUiPreferencesStore:
    """Small atomic store for non-secret, user-scoped interface preferences.

    提供用于非秘密用户范围界面偏好的小型原子存储."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._write_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _read_payload(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": _SCHEMA_VERSION}
        if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
            raise ValueError("Unsupported UI preferences format")
        return payload

    @staticmethod
    def _workspace_key(workspace: Path) -> str:
        return hashlib.sha256(os.path.normcase(str(workspace.resolve())).encode()).hexdigest()

    def _load_agent_preferences(self, workspace: Path | None = None) -> AgentPreferences:
        payload = self._read_payload()
        if workspace is None:
            values = payload.get("agent", {})
        else:
            projects = payload.get("projects", {})
            if not isinstance(projects, dict):
                raise ValueError("Invalid project preferences")
            values = projects.get(self._workspace_key(workspace), {})
        if not isinstance(values, dict):
            raise ValueError("Invalid agent preferences")
        names = {field.name for field in fields(AgentPreferences)}
        return AgentPreferences(**{key: value for key, value in values.items() if key in names})

    async def load_agent_preferences(self, workspace: Path | None = None) -> AgentPreferences:
        return await run_blocking(self._load_agent_preferences, workspace)

    async def load_effective_agent_preferences(self, workspace: Path) -> AgentPreferences:
        async with self._write_lock:
            user = await self.load_agent_preferences()
            project = await self.load_agent_preferences(workspace)
        values = asdict(user)
        values.update({key: value for key, value in asdict(project).items() if value is not None})
        return AgentPreferences(**values)

    async def save_agent_preferences(
        self, preferences: AgentPreferences, workspace: Path | None = None
    ) -> None:
        async with self._write_lock:
            await run_blocking(self._save_agent_preferences, preferences, workspace)

    def _save_agent_preferences(
        self, preferences: AgentPreferences, workspace: Path | None
    ) -> None:
        language, effort, mode, theme = self._load_preferences()
        self._save_preferences(
            language, effort, mode, theme, agent=preferences, workspace=workspace
        )

    async def load_language(self) -> UiLanguage:
        language, _, _, _ = await run_blocking(self._load_preferences)
        return language

    async def load_reasoning_effort(self) -> ReasoningEffort:
        _, effort, _, _ = await run_blocking(self._load_preferences)
        return effort

    async def load_interaction_mode(self) -> InteractionMode:
        _, _, mode, _ = await run_blocking(self._load_preferences)
        return mode

    def _load_preferences(self) -> tuple[UiLanguage, ReasoningEffort, InteractionMode, UiTheme]:
        try:
            payload: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return (
                UiLanguage.ENGLISH,
                ReasoningEffort.HIGH,
                InteractionMode.NORMAL,
                UiTheme.PORCELAIN,
            )
        if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
            return (
                UiLanguage.ENGLISH,
                ReasoningEffort.HIGH,
                InteractionMode.NORMAL,
                UiTheme.PORCELAIN,
            )
        raw_language = payload.get("language")
        try:
            language = (
                UiLanguage(raw_language) if isinstance(raw_language, str) else UiLanguage.ENGLISH
            )
        except ValueError:
            language = UiLanguage.ENGLISH
        raw_effort = payload.get("reasoning_effort")
        try:
            effort = (
                ReasoningEffort(raw_effort) if isinstance(raw_effort, str) else ReasoningEffort.HIGH
            )
        except ValueError:
            effort = ReasoningEffort.HIGH
        raw_mode = payload.get("interaction_mode")
        try:
            mode = (
                InteractionMode(raw_mode) if isinstance(raw_mode, str) else InteractionMode.NORMAL
            )
        except ValueError:
            mode = InteractionMode.NORMAL
        try:
            theme = UiTheme(payload.get("theme", UiTheme.PORCELAIN))
        except (ValueError, TypeError):
            theme = UiTheme.PORCELAIN
        return language, effort, mode, theme

    async def save_language(self, language: UiLanguage) -> None:
        async with self._write_lock:
            await run_blocking(self._save_language, language)

    def _save_language(self, language: UiLanguage) -> None:
        _, effort, mode, theme = self._load_preferences()
        self._save_preferences(language, effort, mode, theme)

    async def save_reasoning_effort(self, effort: ReasoningEffort) -> None:
        async with self._write_lock:
            await run_blocking(self._save_reasoning_effort, effort)

    def _save_reasoning_effort(self, effort: ReasoningEffort) -> None:
        language, _, mode, theme = self._load_preferences()
        self._save_preferences(language, effort, mode, theme)

    async def save_interaction_mode(self, mode: InteractionMode) -> None:
        async with self._write_lock:
            await run_blocking(self._save_interaction_mode, mode)

    def _save_interaction_mode(self, mode: InteractionMode) -> None:
        language, effort, _, theme = self._load_preferences()
        self._save_preferences(language, effort, mode, theme)

    async def load_theme(self) -> UiTheme:
        _, _, _, theme = await run_blocking(self._load_preferences)
        return theme

    async def save_theme(self, theme: UiTheme) -> None:
        async with self._write_lock:
            await run_blocking(self._save_theme, theme)

    def _save_theme(self, theme: UiTheme) -> None:
        language, effort, mode, _ = self._load_preferences()
        self._save_preferences(language, effort, mode, theme)

    def _save_preferences(
        self,
        language: UiLanguage,
        effort: ReasoningEffort,
        mode: InteractionMode,
        theme: UiTheme,
        *,
        agent: AgentPreferences | None = None,
        workspace: Path | None = None,
    ) -> None:
        payload = self._read_payload()
        if agent is not None:
            if workspace is None:
                payload["agent"] = asdict(agent)
            else:
                projects = payload.setdefault("projects", {})
                if not isinstance(projects, dict):
                    raise ValueError("Invalid project preferences")
                projects[self._workspace_key(workspace)] = asdict(agent)
        payload.update(
            version=_SCHEMA_VERSION,
            language=language.value,
            reasoning_effort=effort.value,
            interaction_mode=mode.value,
            theme=theme.value,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(
                    payload,
                    temporary,
                    ensure_ascii=False,
                    indent=2,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


__all__ = ["JsonUiPreferencesStore"]
