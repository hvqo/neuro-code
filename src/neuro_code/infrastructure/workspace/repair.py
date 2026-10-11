"""Bounded real workspace facts, using the existing checkpoint projection."""

from __future__ import annotations

import hashlib
from pathlib import Path

from neuro_code.application.ports.workflow_repair import RepairWorkspaceEvidence
from neuro_code.application.ports.workflow_verification import VerificationWorkspaceEvidence
from neuro_code.domain.checkpoints import workspace_projection_fingerprint
from neuro_code.domain.worktree import WorktreeHandle, WorktreeId
from neuro_code.infrastructure.workspace.verification import LocalWorkflowVerificationWorkspace


class LocalWorkflowRepairWorkspace(LocalWorkflowVerificationWorkspace):
    async def snapshot(self, root: Path) -> RepairWorkspaceEvidence:
        snapshot = await self.reader.inspect(root)
        if len(snapshot.projection.entries) > 256:
            raise ValueError("REPAIR workspace projection exceeds bounded evidence limit")
        repository = snapshot.repository
        handle = WorktreeHandle(
            WorktreeId("parent-" + repository.repository_id),
            repository,
            repository.source_worktree,
            repository.head_sha,
            None,
        )
        return RepairWorkspaceEvidence(
            VerificationWorkspaceEvidence(
                repository.repository_id,
                str(repository.source_worktree),
                repository.head_sha,
                workspace_projection_fingerprint(handle, snapshot.projection).value,
            ),
            tuple(
                (
                    entry.path,
                    hashlib.sha256(
                        repr(
                            (entry.present, entry.kind.value, entry.mode, entry.link_target)
                        ).encode()
                        + (entry.content or b"")
                    ).hexdigest(),
                )
                for entry in snapshot.projection.entries
            ),
        )
