"""Native Neuro Code replay with original delta timing and terminal-write probes.

No provider/auth is loaded. The default tape is synthetic and labelled as such.
Enter starts the replay. Ctrl+C exits; --auto-exit starts and exits automatically
for an actual controlling-PTY measurement. No production timing setting is exposed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from time import perf_counter, process_time, thread_time
from unittest.mock import patch

from tests.visual.showcases import _VisualFixtureRunner
from tests.visual.text_arrival.frame_pacing import (
    CASES,
    Profile,
    deliver,
    load_recording,
    tape,
)
from textual._compositor import ChopsUpdate, LayoutUpdate
from textual.drivers._writer_thread import WriterThread
from textual.drivers.linux_driver import LinuxDriver

from neuro_code.interfaces.tui import widgets
from neuro_code.interfaces.tui.app import NeuroCodeApp
from neuro_code.interfaces.tui.widgets import PromptInput, TranscriptScroll
from neuro_code.shared.ui_theme import UiTheme


class MeasuredOutput:
    """Observe actual file writes in the writer thread, not only queue enqueue.

    Thread-local CPU is reported separately from event-loop nested exclusive cost.
    The real writer and flushing behavior are preserved.
    """

    def __init__(self, file, profile):
        self.file, self.profile = file, profile

    def __getattr__(self, name):
        return getattr(self.file, name)

    def write(self, text):
        wall, cpu = perf_counter(), thread_time()
        result = self.file.write(text)
        if self.profile.active:
            self.profile.counts["writer_file_write"] += 1
            self.profile.counts["writer_bytes"] += len(text.encode())
            self.profile.cpu["writer_thread_write"] += thread_time() - cpu
            self.profile.durations["writer_file_write"].append((perf_counter() - wall) * 1000)
            self.profile.event("writer_file_write", bytes=len(text.encode()))
        return result

    def flush(self):
        started = perf_counter()
        result = self.file.flush()
        if self.profile.active:
            self.profile.counts["writer_file_flush"] += 1
            self.profile.durations["writer_file_flush"].append((perf_counter() - started) * 1000)
            self.profile.event("writer_file_flush")
        return result


class NativePacingReplay(NeuroCodeApp):
    def __init__(self, args, profile):
        super().__init__(
            _VisualFixtureRunner(),
            ui_theme=UiTheme(args.theme),
            provider_name="timed-replay",
            model_name=f"{widgets.VIEW_COMMIT_SECONDS * 1000:g}ms-view / 24fps-ink",
            cwd=Path.cwd(),
        )
        self.replay_args = args
        self.profile = profile
        self._replay_running = False
        self._agent_preferences = replace(
            self._agent_preferences, text_arrival_animation=not args.off
        )

    def on_mount(self):
        super().on_mount()
        if self.replay_args.auto_exit:
            self.call_after_refresh(self.start_replay)

    def start_replay(self):
        if not self._replay_running:
            self._replay_running = True
            self.run_worker(self.replay(), exclusive=True)

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted):
        event.stop()
        event.prevent_default()
        event.input.clear()
        self.start_replay()

    async def replay(self):
        args, profile = self.replay_args, self.profile
        self._replace_transcript([])
        self._write_entry(
            "user", f"{args.case}: {widgets.VIEW_COMMIT_SECONDS * 1000:g}ms body / 24fps ink"
        )
        await asyncio.sleep(0.3)
        profile.__init__()
        profile.app = self
        profile.active = True
        profile.start = perf_counter()
        cpu = process_time()
        recording = load_recording(args.recording) if args.recording else None
        text = await deliver(self, profile, recording if recording is not None else tape(args.case))
        await asyncio.sleep(0.3)
        result = profile.summary(
            process_time() - cpu,
            perf_counter() - profile.start,
            widgets.VIEW_COMMIT_SECONDS * 1000,
            args.case,
            "native-driver",
        )
        transcript = self.query_one("#transcript", TranscriptScroll)
        result["followed_tail"] = transcript.is_vertical_scroll_end
        result["animation"] = not args.off
        result["input_origin"] = "external-recording" if recording else "synthetic-stress-tape"
        result["trace"] = list(profile.trace)
        profile.active = False
        if self._pending_assistant is not None:
            assert self._pending_assistant.content == text
            assert self._pending_assistant.renderable.markup == text
            assert self._pending_assistant._stream_view_timer is None
            assert self._pending_assistant._arrival_timer is None
            self._finish_pending_assistant(text)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        self._replay_running = False
        if args.auto_exit:
            self.exit()


def run(args):
    profile = Profile()
    app = NativePacingReplay(args, profile)
    profile.app = app
    constructor = WriterThread.__init__

    def writer_init(writer, file):
        constructor(writer, MeasuredOutput(file, profile))

    with ExitStack() as stack:
        stack.enter_context(profile.hooks())
        for cls in (ChopsUpdate, LayoutUpdate):
            stack.enter_context(
                patch.object(
                    cls, "render_segments", profile.wrap(cls.render_segments, "terminal_serialize")
                )
            )
        stack.enter_context(
            patch.object(LinuxDriver, "write", profile.wrap(LinuxDriver.write, "driver_enqueue"))
        )
        stack.enter_context(patch.object(WriterThread, "__init__", writer_init))
        app.run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=list(CASES), default="mixed")
    parser.add_argument("--theme", choices=["graphite", "porcelain", "system"], default="system")
    parser.add_argument("--off", action="store_true")
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/tmp/neuro-native-pacing.json"))
    parser.add_argument("--auto-exit", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
