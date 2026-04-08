from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextDocument


@dataclass(frozen=True)
class TextChange:
    # 文字位置（toPlainText 基準）
    start: int
    old_end: int
    new_end: int
    # バイト位置（Tree-sitter 用）
    start_byte: int
    old_end_byte: int
    new_end_byte: int
    # 行/列（LSP 用、0-indexed）
    start_point: tuple[int, int]
    old_end_point: tuple[int, int]
    new_end_point: tuple[int, int]


def _offset_to_point(text: str, offset: int) -> tuple[int, int]:
    prefix = text[:offset]
    line = prefix.count("\n")
    last_newline = prefix.rfind("\n")
    col = offset - last_newline - 1
    return (line, col)


def _diff(old: str, new: str) -> tuple[int, int, int]:
    """変更前後のテキストから最小変更範囲を返す (start, old_end, new_end)。"""
    start = 0
    min_len = min(len(old), len(new))
    while start < min_len and old[start] == new[start]:
        start += 1

    old_end = len(old)
    new_end = len(new)
    while old_end > start and new_end > start and old[old_end - 1] == new[new_end - 1]:
        old_end -= 1
        new_end -= 1

    return start, old_end, new_end


class TextDocument(QObject):
    changed = Signal(TextChange)

    def __init__(self, doc: QTextDocument) -> None:
        super().__init__()
        self._doc = doc
        self._text = ""
        doc.contentsChanged.connect(self._on_changed)

    @property
    def text(self) -> str:
        return self._text

    def _on_changed(self) -> None:
        old_text = self._text
        new_text = self._doc.toPlainText()

        if old_text == new_text:
            return

        start, old_end, new_end = _diff(old_text, new_text)

        prefix_bytes = len(old_text[:start].encode())
        start_byte = prefix_bytes
        old_end_byte = prefix_bytes + len(old_text[start:old_end].encode())
        new_end_byte = prefix_bytes + len(new_text[start:new_end].encode())

        self._text = new_text

        self.changed.emit(
            TextChange(
                start=start,
                old_end=old_end,
                new_end=new_end,
                start_byte=start_byte,
                old_end_byte=old_end_byte,
                new_end_byte=new_end_byte,
                start_point=_offset_to_point(old_text, start),
                old_end_point=_offset_to_point(old_text, old_end),
                new_end_point=_offset_to_point(new_text, new_end),
            )
        )
