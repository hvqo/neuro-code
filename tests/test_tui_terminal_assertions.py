"""Darwin-specific termios comparisons for kernel-owned state bits."""

from __future__ import annotations

from types import ModuleType

from tests.terminal_assertions import terminal_mode_signature


def test_darwin_terminal_comparison_masks_only_kernel_state_flags() -> None:
    darwin_termios = ModuleType("darwin_termios_fixture")
    darwin_termios.PENDIN = 536870912
    darwin_termios.FLUSHO = 8388608
    pendin = darwin_termios.PENDIN
    flusho = darwin_termios.FLUSHO
    original = [1, 2, 3, 0x100, [4, 5], 9600, 9600]
    pendin_changed = [1, 2, 3, 0x100 | pendin, [4, 5], 9600, 9600]
    other_state_changed = [1, 2, 3, 0x100 | flusho, [4, 5], 9600, 9600]
    user_mode_changed = [1, 2, 3, 0x101 | pendin, [4, 5], 9600, 9600]

    assert terminal_mode_signature(original, darwin_termios, platform="darwin") == (
        terminal_mode_signature(pendin_changed, darwin_termios, platform="darwin")
    )
    assert terminal_mode_signature(original, darwin_termios, platform="darwin") != (
        terminal_mode_signature(user_mode_changed, darwin_termios, platform="darwin")
    )
    assert terminal_mode_signature(original, darwin_termios, platform="darwin") != (
        terminal_mode_signature(other_state_changed, darwin_termios, platform="darwin")
    )
    assert terminal_mode_signature(original, darwin_termios, platform="linux") != (
        terminal_mode_signature(pendin_changed, darwin_termios, platform="linux")
    )
