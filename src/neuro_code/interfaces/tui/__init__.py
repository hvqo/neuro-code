"""TUI-safe projections for typed runtime events.

面向 TUI 的类型化运行时事件安全投影.
"""

from neuro_code.interfaces.tui.execution import (
    recoverable_execution_reason,
    recoverable_terminal_status,
)

__all__ = ["recoverable_execution_reason", "recoverable_terminal_status"]
