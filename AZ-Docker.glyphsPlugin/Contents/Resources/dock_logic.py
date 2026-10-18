"""Pure bookkeeping for AZ Docker. No AppKit imports — fully unit-testable."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CandidateInfo:
    """Everything the filter needs to know about a window, pre-extracted."""

    visible: bool
    is_panel: bool
    level: int
    has_content_view: bool
    is_document_window: bool
    is_docked: bool
    has_title: bool = True
    is_dialog: bool = False


def is_candidate(info: CandidateInfo) -> bool:
    """True if this window may be docked into the sidebar stack."""
    if not (info.visible and info.has_content_view):
        return False
    if info.is_document_window or info.is_docked or info.is_dialog:
        return False
    if info.is_panel or info.level > 0:
        return True
    # normal-level plugin windows (vanilla Window() etc.); the title
    # requirement filters out borderless/transient junk
    return info.has_title


def height_resizable(
    has_resizable_mask: bool, min_height: float, max_height: float
) -> bool:
    """True if the window's own height may change, so its card gets a resize grip."""
    # vanilla only sets the resizable bit when minSize/maxSize are passed,
    # and pins height by giving both the same height
    return has_resizable_mask and min_height < max_height


@dataclass
class DockedEntry:
    """One docked window: the shell NSWindow, its adopted content, restore info."""

    window: Any
    content_view: Any
    frame: Any
    title: str
    content_height: float
    collapsed: bool = False
    height_resizable: bool = True


class DockStore:
    """Ordered set of docked windows. Pure bookkeeping, no views."""

    def __init__(self) -> None:
        self.entries: list[DockedEntry] = []

    def find(self, window: Any) -> DockedEntry | None:
        for candidate in self.entries:
            if candidate.window == window:
                return candidate
        return None

    def index_of(self, window: Any) -> int | None:
        for index, candidate in enumerate(self.entries):
            if candidate.window == window:
                return index
        return None

    def dock(self, entry: DockedEntry) -> bool:
        if self.find(entry.window) is not None:
            return False
        self.entries.append(entry)
        return True

    def undock(self, window: Any) -> DockedEntry | None:
        entry = self.find(window)
        if entry is not None:
            self.entries.remove(entry)
        return entry

    def move(self, from_index: int, to_index: int) -> None:
        if not 0 <= from_index < len(self.entries):
            return
        to_index = max(0, min(to_index, len(self.entries) - 1))
        self.entries.insert(to_index, self.entries.pop(from_index))

    def toggle_collapsed(self, window: Any) -> bool | None:
        entry = self.find(window)
        if entry is None:
            return None
        entry.collapsed = not entry.collapsed
        return entry.collapsed
