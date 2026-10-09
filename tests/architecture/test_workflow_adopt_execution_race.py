"""OS-backed first execution versus conservative recovery arbitration."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

from neuro_code.application.ports.workflow_state import WorkflowStateError
from neuro_code.domain.workflows.activity import WorkflowActivityState as State
from neuro_code.domain.workflows.state import WorkflowStatus
from neuro_code.infrastructure.persistence.sqlite_session import SqliteSessionStore
from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_lock import (
    adoption_execution_lock,
)
from tests.architecture.test_workflow_adopt_adapter import adapter, ready
from tests.architecture.test_workflow_adoption import END
from tests.architecture.test_workflow_interpreter import engine
from tests.test_result_adoption import _RecordingMutation


def snapshot(store):
    """Check busy does not write any accounting, result or journal fact."""
    with closing(store._connect()) as connection:
        return tuple(
            tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall())
            for table in (
                "workflow_runs",
                "workflow_activity_attempts",
                "workflow_activity_results",
                "workflow_activity_events",
                "workflow_transition_journal",
                "workflow_adoption_measurements",
            )
        )


async def reconcile(store, key, root, session):
    return await store.reconcile_workflow_adoption(
        key,
        parent_session_id=session,
        parent_workspace_root=str(root),
        updated_at=END + timedelta(seconds=2),
    )


class DiskMutation(_RecordingMutation):
    def __init__(self, parent, root):
        super().__init__(parent)
        self.root = root

    async def apply(self, request, *, session_id):
        result = await super().apply(request, session_id=session_id)
        path = self.root / request.path
        if request.desired is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(request.desired.content)
        return result


def delayed_terminal(first, observed, release):
    factory = first.factory

    def build(port):
        original = factory(port)

        class HoldReturn:
            parent_session_id = original.parent_session_id

            async def prepare(self, *args, **kwargs):
                return await original.prepare(*args, **kwargs)

            async def adopt(self, *args, **kwargs):
                result = await original.adopt(*args, **kwargs)
                observed.set()
                await asyncio.wait_for(release.wait(), 30)
                return result

        return HoldReturn()

    first.factory = build


@pytest.mark.parametrize("no_changes", [False, True])
@pytest.mark.parametrize("competitors", [1, 4])
async def test_other_stores_and_adapters_cannot_settle_live_terminal(
    tmp_path, no_changes, competitors
):
    f, key = await ready(tmp_path, no_changes=no_changes)
    port = DiskMutation(f.parent, f.binding.workspace_root)
    first = adapter(f, mutation=port)
    observed, release = asyncio.Event(), asyncio.Event()
    delayed_terminal(first, observed, release)
    task = asyncio.create_task(first.run_once(key))
    try:
        await asyncio.wait_for(observed.wait(), 15)
        before = snapshot(f.store)
        stores = [SqliteSessionStore(f.store.database_path) for _ in range(competitors)]
        errors = await asyncio.gather(
            *(
                reconcile(s, key, f.binding.workspace_root, f.binding.runner.session_id)
                for s in stores
            ),
            return_exceptions=True,
        )
        assert all(
            isinstance(e, WorkflowStateError) and e.kind == "concurrent_execution" for e in errors
        )
        outcomes = await asyncio.gather(
            *(adapter(f, store=s, mutation=port).run_once(key) for s in [f.store, *stores])
        )
        assert all(o.disposition == "busy" for o in outcomes)
        assert snapshot(f.store) == before
        attempt = await f.store.get_workflow_activity(key)
        assert attempt.state is State.RUNNING
        assert attempt.result is None
        assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.WAITING
    finally:
        release.set()
        result = await asyncio.wait_for(task, 15)
    assert result.attempt.result.usage.known
    assert result.attempt.result.usage.tool_calls == len(port.calls) == (0 if no_changes else 2)
    assert (await adapter(f).run_once(key)).attempt == result.attempt
    for request in port.calls:
        path = f.binding.workspace_root / request.path
        assert (path.read_bytes() if path.exists() else None) == (
            request.desired.content if request.desired else None
        )
    run = await f.store.get_workflow_run("workflow-run")
    tick = await engine(f.store).advance_once(
        run.run_id,
        expected_generation=run.generation,
        owner_id=run.owner_id,
        owner_fence=run.owner_fence,
        updated_at=END + timedelta(seconds=2),
    )
    assert tick.action == "consume_activity"


async def test_active_preparation_is_not_abandoned_running(tmp_path):
    f, key = await ready(tmp_path)
    first = adapter(f)
    observed, release = asyncio.Event(), asyncio.Event()
    factory = first.factory

    def build(port):
        original = factory(port)

        class HoldPrepare:
            parent_session_id = original.parent_session_id

            async def prepare(self, *args, **kwargs):
                observed.set()
                await asyncio.wait_for(release.wait(), 30)
                return await original.prepare(*args, **kwargs)

            async def adopt(self, *args, **kwargs):
                return await original.adopt(*args, **kwargs)

        return HoldPrepare()

    first.factory = build
    task = asyncio.create_task(first.run_once(key))
    try:
        await asyncio.wait_for(observed.wait(), 15)
        before = snapshot(f.store)
        other = SqliteSessionStore(f.store.database_path)
        assert (await adapter(f, store=other).run_once(key)).disposition == "busy"
        assert snapshot(f.store) == before
    finally:
        release.set()
        result = await asyncio.wait_for(task, 15)
    assert result.attempt.result.usage.known


async def test_deadline_and_cancel_do_not_release_inflight_execution(tmp_path):
    from neuro_code.domain.workflows.state import WorkflowChange, WorkflowEventKind

    f, key = await ready(tmp_path)
    observed, release = asyncio.Event(), asyncio.Event()

    class HoldPort(DiskMutation):
        async def apply(self, request, *, session_id):
            result = await super().apply(request, session_id=session_id)
            observed.set()
            await asyncio.wait_for(release.wait(), 30)
            return result

    port = HoldPort(f.parent, f.binding.workspace_root)
    task = asyncio.create_task(adapter(f, mutation=port).run_once(key))
    try:
        await asyncio.wait_for(observed.wait(), 15)
        other = SqliteSessionStore(f.store.database_path)
        before = snapshot(f.store)
        # Far past the dispatch deadline does not prove the live scope is gone.
        with pytest.raises(WorkflowStateError, match="still active"):
            await other.mark_workflow_adoption_attention(
                key, expected_revision=2, updated_at=END + timedelta(hours=1)
            )
        assert snapshot(f.store) == before
        run = await f.store.get_workflow_run("workflow-run")
        await f.store.transition_workflow_run(
            run.run_id,
            WorkflowChange(WorkflowEventKind.TRANSITION, status=WorkflowStatus.CANCELLED),
            expected_generation=run.generation,
            owner_id=run.owner_id,
            owner_fence=run.owner_fence,
            request_id="cancel-in-port",
            updated_at=END + timedelta(seconds=2),
        )
    finally:
        release.set()
        await asyncio.wait_for(task, 15)
    assert len(port.calls) == 1  # The second target cannot dispatch after cancel.
    assert (await f.store.get_workflow_run("workflow-run")).status is WorkflowStatus.CANCELLED


async def test_cancelled_live_scope_releases_for_unknown_terminal_recovery(tmp_path):
    f, key = await ready(tmp_path)
    first = adapter(f)
    observed, release = asyncio.Event(), asyncio.Event()
    delayed_terminal(first, observed, release)
    task = asyncio.create_task(first.run_once(key))
    await asyncio.wait_for(observed.wait(), 15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    other = SqliteSessionStore(f.store.database_path)
    result = await adapter(f, store=other).run_once(key)
    assert result.attempt.result.usage.tool_calls is None
    assert result.attempt.result.usage.wall_milliseconds is None
    assert (await other.get_workflow_run("workflow-run")).status is WorkflowStatus.NEEDS_ATTENTION
    assert len(f.mutation.calls) == 2
    assert (await adapter(f, store=other).run_once(key)).attempt == result.attempt


# Real child owns setup, engine, injected disk port, RUNNING and OS lock. Its
# terminal barrier uses stdin/stdout, not monkeypatching the measurement writer.
_EXECUTOR = r"""
import asyncio,json,os,sys
from pathlib import Path
from neuro_code.shared.async_utils import run_blocking
from tests.architecture.test_workflow_adopt_adapter import adapter,ready
from tests.architecture.test_workflow_adopt_execution_race import DiskMutation
async def main():
    f,key=await ready(Path(sys.argv[1]))
    port=DiskMutation(f.parent,f.binding.workspace_root)
    first=adapter(f,mutation=port)
    factory=first.factory
    def build(measured):
        original=factory(measured)
        class HoldReturn:
            parent_session_id=original.parent_session_id
            async def prepare(self,*a,**kw):return await original.prepare(*a,**kw)
            async def adopt(self,*a,**kw):
                record=await original.adopt(*a,**kw)
                print(json.dumps({'db':str(f.store.database_path),'key':key,'root':str(f.binding.workspace_root),'session':f.binding.runner.session_id,'calls':len(port.calls)}),flush=True)
                command=await run_blocking(sys.stdin.readline)
                if command.strip()=='exit':os._exit(19)
                return record
        return HoldReturn()
    first.factory=build
    result=await first.run_once(key)
    print(json.dumps({'tool_calls':result.attempt.result.usage.tool_calls,'known':result.attempt.result.usage.known}),flush=True)
asyncio.run(main())
"""


async def start_child(tmp_path):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _EXECUTOR,
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        assert line, (await process.stderr.read()).decode()
        return process, json.loads(line)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise


@pytest.mark.parametrize("abandon", [False, True])
async def test_real_cross_process_live_or_killed_executor(tmp_path, abandon):
    child, data = await start_child(tmp_path)
    try:
        store = SqliteSessionStore(Path(data["db"]))
        before = snapshot(store)
        key, root, session = data["key"], Path(data["root"]), data["session"]
        assert data["calls"] == 2
        with pytest.raises(WorkflowStateError) as error:
            await reconcile(store, key, root, session)
        assert error.value.kind == "concurrent_execution"
        assert snapshot(store) == before
        assert (await store.get_workflow_activity(key)).state is State.RUNNING
        if abandon:
            child.kill()  # Real OS process termination releases the exclusive lock.
            await asyncio.wait_for(child.wait(), 15)
        else:
            child.stdin.write(b"complete\n")
            await child.stdin.drain()
            line = await asyncio.wait_for(child.stdout.readline(), 15)
            outcome = json.loads(line)
            assert outcome == {"tool_calls": 2, "known": True}
            await asyncio.wait_for(child.wait(), 15)
            assert child.returncode == 0, (await child.stderr.read()).decode()
        result = await reconcile(store, key, root, session)
        assert result.result.usage.tool_calls == (None if abandon else 2)
        assert result.result.usage.known is not abandon
        run = await store.get_workflow_run("workflow-run")
        assert run.status is (WorkflowStatus.NEEDS_ATTENTION if abandon else WorkflowStatus.WAITING)
        stable = snapshot(store)
        assert await reconcile(SqliteSessionStore(Path(data["db"])), key, root, session) == result
        assert snapshot(store) == stable
        request = await store.get_workflow_adoption_request(key)
        record = await store.get_result_adoption(request.adoption_id)
        for target in record.plan.targets:
            path = root / target.path
            assert (path.read_bytes() if path.exists() else None) == (
                target.desired.content if target.desired else None
            )
        # Neither recovery invokes a mutation port. Durable intent count stays two.
        with closing(store._connect()) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM workflow_adoption_dispatch_events WHERE phase='intent'"
                ).fetchone()[0]
                == 2
            )
        if not abandon:
            tick = await engine(store).advance_once(
                run.run_id,
                expected_generation=run.generation,
                owner_id=run.owner_id,
                owner_fence=run.owner_fence,
                updated_at=END + timedelta(seconds=2),
            )
            assert tick.action == "consume_activity"
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


def test_lock_is_nonblocking_independent_per_invocation_and_never_unlinked(tmp_path):
    db = tmp_path / "db.sqlite"
    db.touch()
    with adoption_execution_lock(db, "same"):
        with pytest.raises(WorkflowStateError) as error, adoption_execution_lock(db, "same"):
            pytest.fail("second descriptor acquired live lock")
        assert error.value.kind == "concurrent_execution"
        with adoption_execution_lock(db, "different"):
            pass
    paths = list(tmp_path.glob("*.workflow-adoption-locks/*.lock"))
    assert len(paths) == 2
    inodes = [p.stat().st_ino for p in paths]
    with adoption_execution_lock(db, "same"):
        pass
    assert [p.stat().st_ino for p in paths] == inodes


def test_symlink_database_alias_uses_same_lock(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows symlinks require platform privilege")
    db = tmp_path / "db.sqlite"
    db.touch()
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(db)
    with (
        adoption_execution_lock(db, "same"),
        pytest.raises(WorkflowStateError, match="still active"),
        adoption_execution_lock(alias, "same"),
    ):
        pytest.fail("alias must share lock")


async def test_legacy_v40_running_has_no_lock_or_fabricated_measurement(tmp_path):
    from neuro_code.domain.workflows.state import BudgetAmounts
    from tests.architecture.test_workflow_adoption import service

    f, key = await ready(tmp_path)
    request = await f.store.get_workflow_adoption_request(key)
    claimed = await f.store.claim_workflow_activity(
        key,
        expected_revision=0,
        owner_id="legacy-owner",
        reserved=BudgetAmounts(tool_calls=64, wall_milliseconds=30000),
        updated_at=END,
    )
    await f.store.start_workflow_activity(
        key,
        expected_revision=1,
        owner_id=claimed.owner_id,
        owner_fence=claimed.owner_fence,
        updated_at=END,
    )
    await service(f).adopt(request)
    with closing(f.store._connect()) as connection, connection:
        for table in (
            "workflow_adoption_measurements",
            "workflow_adoption_dispatch_events",
            "workflow_adoption_executions",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET version=40")
    other = SqliteSessionStore(f.store.database_path)
    await other.initialize()
    result = await reconcile(other, key, f.binding.workspace_root, f.binding.runner.session_id)
    assert result.result.usage.tool_calls is None
    assert result.result.usage.wall_milliseconds is None
    assert len(f.mutation.calls) == 2
    with closing(other._connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM workflow_adoption_executions").fetchone()[0]
            == 0
        )
    assert (await other.get_workflow_run("workflow-run")).status is WorkflowStatus.NEEDS_ATTENTION


async def test_unknown_settlement_failure_releases_os_lock_and_rolls_back(tmp_path):
    f, key = await ready(tmp_path)
    first = adapter(f)
    observed, release = asyncio.Event(), asyncio.Event()
    delayed_terminal(first, observed, release)
    task = asyncio.create_task(first.run_once(key))
    await asyncio.wait_for(observed.wait(), 15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with closing(f.store._connect()) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_unknown BEFORE INSERT ON workflow_activity_results BEGIN SELECT RAISE(ABORT,'rollback recovery'); END"
        )
    before = snapshot(f.store)
    for _ in range(2):
        with pytest.raises(WorkflowStateError) as failure:
            await reconcile(
                SqliteSessionStore(f.store.database_path),
                key,
                f.binding.workspace_root,
                f.binding.runner.session_id,
            )
        assert "rollback recovery" in str(failure.value.__cause__)
        assert snapshot(f.store) == before
    with closing(f.store._connect()) as connection, connection:
        connection.execute("DROP TRIGGER reject_unknown")
    result = await reconcile(f.store, key, f.binding.workspace_root, f.binding.runner.session_id)
    assert result.result.usage.tool_calls is None
    assert len(f.mutation.calls) == 2


async def test_stale_execution_after_lock_release_does_not_dispatch(tmp_path):
    f, key = await ready(tmp_path)
    await adapter(f).run_once(key)
    before = snapshot(f.store)

    async def unexpected_dispatch(*_args):
        pytest.fail("terminal invocation cannot dispatch")

    with pytest.raises(WorkflowStateError, match="first owner CAS"):
        await f.store.execute_workflow_adoption(
            key,
            expected_revision=1,
            owner_id="stale",
            owner_fence=1,
            updated_at=END,
            mutation=f.mutation,
            dispatch=unexpected_dispatch,
        )
    assert snapshot(f.store) == before
    assert len(f.mutation.calls) == 2


async def test_child_task_cannot_borrow_first_task_attention_bypass(tmp_path):
    from neuro_code.infrastructure.persistence.sqlite_session_workflow_adoption_lock import (
        owns_execution_lock,
    )

    db = tmp_path / "db.sqlite"
    db.touch()
    with adoption_execution_lock(db, "same", execution=True):
        assert owns_execution_lock(db, "same")

        async def child():
            assert not owns_execution_lock(db, "same")
            with pytest.raises(WorkflowStateError), adoption_execution_lock(db, "same"):
                pytest.fail("inherited ContextVar must not bypass OS lock")

        await asyncio.create_task(child())
    assert not owns_execution_lock(db, "same")


async def test_known_terminal_replays_while_first_scope_still_holds_lock(tmp_path, monkeypatch):
    from neuro_code.infrastructure.persistence import (
        sqlite_session_workflow_adoption_meter as meter,
    )

    f, key = await ready(tmp_path)
    original = meter._ExecutionMeter.complete

    async def commit_then_lose_ack(scope):
        await original(scope)
        other = SqliteSessionStore(f.store.database_path)
        before = snapshot(f.store)
        terminal = await reconcile(
            other, key, f.binding.workspace_root, f.binding.runner.session_id
        )
        assert terminal.result.usage.known
        assert terminal.result.usage.tool_calls == 2
        assert snapshot(f.store) == before
        raise asyncio.CancelledError("known settlement committed, outer ACK lost")

    monkeypatch.setattr(meter._ExecutionMeter, "complete", commit_then_lose_ack)
    with pytest.raises(asyncio.CancelledError):
        await adapter(f).run_once(key)
    other = SqliteSessionStore(f.store.database_path)
    terminal = await reconcile(other, key, f.binding.workspace_root, f.binding.runner.session_id)
    assert terminal.result.usage.tool_calls == len(f.mutation.calls) == 2
    assert (await other.get_workflow_run("workflow-run")).status is WorkflowStatus.WAITING


def test_exception_releases_lock_without_deleting_inode(tmp_path):
    db = tmp_path / "db.sqlite"
    db.touch()
    with pytest.raises(RuntimeError, match="leave scope"), adoption_execution_lock(db, "same"):
        raise RuntimeError("leave scope")
    with adoption_execution_lock(db, "same"):
        pass


def test_windows_byte_lock_protocol_and_busy_close(tmp_path, monkeypatch):
    import errno
    from types import SimpleNamespace

    from neuro_code.infrastructure.persistence import sqlite_session_workflow_adoption_lock as locks

    db = tmp_path / "db.sqlite"
    db.touch()
    calls = []

    def locking(descriptor, mode, count):
        calls.append((os.lseek(descriptor, 0, os.SEEK_CUR), mode, count))
        assert os.fstat(descriptor).st_size == 1

    # Only tests API selection locally; real Windows exclusivity is exercised by
    # the same Store/child-process races in the cross-platform test matrix.
    with monkeypatch.context() as patch:
        patch.setattr(locks.sys, "platform", "win32")
        patch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=locking))
        with adoption_execution_lock(db, "same"):
            pass
        assert calls == [(0, 2, 1)]

        def busy(*_args):
            raise OSError(errno.EACCES, "byte locked")

        patch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=busy))
        with (
            pytest.raises(WorkflowStateError, match="still active"),
            adoption_execution_lock(db, "same"),
        ):
            pytest.fail("busy lock should fail")
    # Failed acquisition closes its descriptor; a subsequent real lock succeeds.
    with adoption_execution_lock(db, "same"):
        pass


def test_lock_unexpected_os_failure_is_not_reported_as_busy(tmp_path, monkeypatch):
    import errno

    db = tmp_path / "db.sqlite"
    db.touch()
    if sys.platform == "win32":
        import msvcrt as native

        method = "locking"
    else:
        import fcntl as native

        method = "flock"

    def fail(*_args):
        raise OSError(errno.EINVAL, "unsupported lock")

    with monkeypatch.context() as patch:
        patch.setattr(native, method, fail)
        with pytest.raises(OSError, match="unsupported lock"), adoption_execution_lock(db, "same"):
            pytest.fail("cannot execute with unsupported lock")
    with adoption_execution_lock(db, "same"):
        pass
