"""Mutable project identity attached to one active conversation binding."""

from __future__ import annotations


class ProjectMemoryScope:
    """Hold the current SessionProject identity for one conversation only.

    The scope is changed only by the session lifecycle owner. Workspace paths
    never derive or replace this identity.
    """

    __slots__ = ("_project_id",)

    def __init__(self, project_id: str | None = None) -> None:
        self._project_id: str | None = None
        self.set_project_id(project_id)

    @property
    def project_id(self) -> str | None:
        return self._project_id

    def set_project_id(self, project_id: str | None) -> None:
        if project_id is not None and (
            not isinstance(project_id, str)
            or not project_id.strip()
            or "\x00" in project_id
            or len(project_id.encode("utf-8")) > 128
        ):
            raise ValueError("project memory scope identity is invalid")
        self._project_id = project_id


__all__ = ["ProjectMemoryScope"]
