import re
import sys
from collections.abc import Sequence
from pathlib import Path

from kaku.document import TextDocument
from kaku.highlighter import (
    EDITOR_BG,
    EDITOR_FG,
    LINE_NUMBER_BG,
    LINE_NUMBER_FG,
    PythonHighlighter,
)
from kaku.lsp import LspClient

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFontDialog,
    QFrame,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QStyledItemDelegate,
    QTextEdit,
    QToolTip,
    QWidget,
)

_DEFAULT_FONT_SIZE = 13

_LINE_NUMBER_PADDING = 8  # 右側パディング (px)

_AUTO_CLOSE = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}
_CLOSE_CHARS = {")", "]", "}"}

def _hover_md_to_html(md: str) -> str:
    """ホバー用Markdown/plaintext→HTML変換"""
    _HR = "<hr>"
    _CODE_STYLE = "font-family:Menlo,'Courier New',Courier;color:#1e66f5;"

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def inline_code(s: str) -> str:
        return re.sub(r"`([^`]+)`",
                      lambda m: f'<code style="{_CODE_STYLE}">{esc(m.group(1))}</code>', s)

    parts: list[str] = []
    in_code = False
    code_buf: list[str] = []
    first_content = True  # 最初の内容行をタイプシグネチャとして扱う

    for line in md.splitlines():
        # コードフェンス
        if line.startswith("```"):
            if not in_code:
                in_code = True
                code_buf = []
                first_content = False
            else:
                in_code = False
                code = "\n".join(code_buf)
                parts.append(f'<code style="{_CODE_STYLE}">{esc(code)}</code>')
            continue
        if in_code:
            code_buf.append(line)
            continue

        stripped = line.strip()
        # 3本以上のハイフンのみの行 → 横線
        if re.fullmatch(r"-{3,}", stripped):
            parts.append(_HR)
            continue

        if stripped:
            if first_content:
                # 1行目はタイプシグネチャ → monospace + 色付き
                parts.append(f'<code style="{_CODE_STYLE}">{esc(stripped)}</code>')
                first_content = False
            else:
                parts.append(inline_code(esc(stripped)))
        else:
            parts.append("")
            first_content = False

    # ブロックリストに変換：("tag"|"hr"|"text", content)
    blocks: list[tuple[str, str]] = []
    text_buf = []

    for part in parts:
        if part == _HR:
            if text_buf:
                blocks.append(("text", " ".join(text_buf)))
                text_buf = []
            blocks.append(("hr", ""))
        elif part.startswith("<"):
            if text_buf:
                blocks.append(("text", " ".join(text_buf)))
                text_buf = []
            blocks.append(("tag", part))
        elif part == "":
            if text_buf:
                blocks.append(("text", " ".join(text_buf)))
                text_buf = []
        else:
            text_buf.append(part)

    if text_buf:
        blocks.append(("text", " ".join(text_buf)))

    html_parts = []
    for i, (kind, content) in enumerate(blocks):
        margin = "margin:0;" if i == 0 else "margin:5px 0 0 0;"
        if kind == "hr":
            html_parts.append(_HR)
        elif kind == "tag":
            html_parts.append(f'<p style="{margin}">{content}</p>')
        else:
            html_parts.append(f'<p style="{margin}">{content}</p>')

    return f"<html><body style='color:#4c4f69;'>{''.join(html_parts)}</body></html>"


def _build_sig_html(sig: dict, active_param: int) -> str:
    """SignatureInformation → アクティブパラメータをハイライトした HTML"""
    label: str = sig.get("label", "")
    params: list = sig.get("parameters", [])

    _CODE   = "font-family:Menlo,'Courier New',Courier;color:#4c4f69;"
    _ACTIVE = "font-family:Menlo,'Courier New',Courier;color:#1e66f5;font-weight:bold;"

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if not params or active_param < 0 or active_param >= len(params):
        return (
            f"<html><body style='margin:0;'>"
            f'<code style="{_CODE}">{esc(label)}</code>'
            f"</body></html>"
        )

    param_label = params[active_param].get("label", "")
    if isinstance(param_label, list) and len(param_label) == 2:
        s, e = int(param_label[0]), int(param_label[1])
        before, active, after = label[:s], label[s:e], label[e:]
    elif isinstance(param_label, str) and param_label:
        idx = label.find(param_label)
        if idx >= 0:
            before = label[:idx]
            active = param_label
            after = label[idx + len(param_label):]
        else:
            before, active, after = label, "", ""
    else:
        before, active, after = label, "", ""

    return (
        f"<html><body style='margin:0;'>"
        f'<code style="{_CODE}">{esc(before)}</code>'
        f'<code style="{_ACTIVE}">{esc(active)}</code>'
        f'<code style="{_CODE}">{esc(after)}</code>'
        f"</body></html>"
    )


# LSP CompletionItemKind → 短縮タグ
_COMPLETION_KIND: dict[int, str] = {
    1: "txt", 2: "mth", 3: "fun", 4: "ctr", 5: "fld",
    6: "var", 7: "cls", 8: "ifc", 9: "mod", 10: "prp",
    11: "unt", 12: "val", 13: "enm", 14: "kwd", 15: "snp",
    16: "clr", 17: "fil", 18: "ref", 19: "fld", 20: "evt",
    21: "opr", 22: "typ", 23: "prm", 24: "typ", 25: "als",
}


class CompletionDelegate(QStyledItemDelegate):
    _COLOR_KIND   = QColor("#9ca0b0")  # Overlay0
    _COLOR_LABEL  = QColor("#4c4f69")  # Text
    _COLOR_DETAIL = QColor("#8c8fa1")  # Overlay1
    _COLOR_SEL    = QColor("#ccd0da")  # Surface1
    _COLOR_BG     = QColor("#eff1f5")  # Base
    _PAD_X = 8
    _PAD_BETWEEN = 10

    def paint(self, painter, option, index) -> None:
        item: dict = index.data(Qt.ItemDataRole.UserRole) or {}
        painter.save()

        bg = self._COLOR_SEL if option.state & option.state.State_Selected else self._COLOR_BG
        painter.fillRect(option.rect, bg)

        fm = painter.fontMetrics()
        y = option.rect.top() + (option.rect.height() + fm.ascent() - fm.descent()) // 2

        kind_text = _COMPLETION_KIND.get(item.get("kind", 0), "   ")
        label     = item.get("label", "")
        detail    = item.get("detail", "")

        x = option.rect.left() + self._PAD_X

        painter.setPen(self._COLOR_KIND)
        painter.drawText(x, y, kind_text)
        x += fm.horizontalAdvance(kind_text) + self._PAD_BETWEEN

        painter.setPen(self._COLOR_LABEL)
        painter.drawText(x, y, label)
        x += fm.horizontalAdvance(label) + self._PAD_BETWEEN

        if detail:
            painter.setPen(self._COLOR_DETAIL)
            painter.drawText(x, y, detail)

        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        sh = super().sizeHint(option, index)
        return QSize(sh.width(), max(sh.height(), 20))


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._editor.paint_line_numbers(event)

    def mouseMoveEvent(self, event) -> None:
        QToolTip.hideText()


class CodeEditor(QTextEdit):
    completion_requested = Signal(int, int)       # line, character
    hover_requested = Signal(int, int)            # line, character
    resolve_requested = Signal(dict)              # CompletionItem
    signature_help_requested = Signal(int, int)   # line, character

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        self._line_number_area = LineNumberArea(self)
        self._line_height_override: int | None = None
        self._text_document = TextDocument(self.document())
        self._highlighter = PythonHighlighter(self._text_document)
        self._diagnostics: list[dict] = []
        self._diagnostic_selections: list[QTextEdit.ExtraSelection] = []
        self.setMouseTracking(True)

        self.document().blockCountChanged.connect(self._update_line_number_area_width)
        self.verticalScrollBar().valueChanged.connect(self._line_number_area.update)
        self.document().contentsChanged.connect(self._line_number_area.update)
        self.cursorPositionChanged.connect(self._update_extra_selections)
        self.cursorPositionChanged.connect(self._line_number_area.update)
        self.cursorPositionChanged.connect(self._on_cursor_for_completion)
        self.cursorPositionChanged.connect(self._on_cursor_for_sig_help)

        self._update_line_number_area_width()

        # ── 補完ポップアップ ──────────────────────────────────
        self._completion_popup = QListWidget(self.viewport())
        self._completion_popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._completion_popup.setStyleSheet(
            "QListWidget { background: #eff1f5; border: 1px solid #bcc0cc; outline: 0; }"
        )
        self._completion_popup.setItemDelegate(CompletionDelegate(self._completion_popup))
        self._completion_popup.setMouseTracking(True)
        self._completion_popup.viewport().setMouseTracking(True)
        self._completion_popup.hide()
        self._completion_popup.itemClicked.connect(lambda _: self._accept_completion())
        self._completion_popup.itemEntered.connect(self._show_completion_doc)
        self._completion_popup.installEventFilter(self)
        self._completion_popup.viewport().installEventFilter(self)
        self._completion_items: list[dict] = []
        self._completion_block: int = -1  # 補完リクエスト時のブロック番号

        # ドキュメントパネル
        self._completion_doc = QFrame(self.viewport())
        self._completion_doc.setStyleSheet(
            "QFrame { background: #eff1f5; border: 1px solid #bcc0cc; }"
        )
        self._completion_doc_label = QLabel(self._completion_doc)
        self._completion_doc_label.setWordWrap(True)
        self._completion_doc_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._completion_doc_label.setStyleSheet("QLabel { border: none; color: #4c4f69; }")
        self._completion_doc.hide()

        self._completion_timer = QTimer(self)
        self._completion_timer.setSingleShot(True)
        self._completion_timer.setInterval(150)
        self._completion_timer.timeout.connect(self._request_completion)

        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(500)
        self._hover_timer.timeout.connect(self._request_hover)
        self._hover_global_pos = QPoint()

        # ── シグネチャヘルプ ──────────────────────────────────
        self._sig_help_widget = QFrame(self.viewport())
        self._sig_help_widget.setStyleSheet(
            "QFrame { background: #eff1f5; border: 1px solid #bcc0cc; }"
        )
        self._sig_help_label = QLabel(self._sig_help_widget)
        self._sig_help_label.setTextFormat(Qt.TextFormat.RichText)
        self._sig_help_label.setStyleSheet("QLabel { border: none; }")
        self._sig_help_widget.hide()
        self._sig_help_block: int = -1
        self._sig_help_open_pos: int = -1  # 開き括弧のドキュメント位置

    @property
    def text_document(self) -> TextDocument:
        return self._text_document

    def setFont(self, font: QFont | str | Sequence[str]):
        super().setFont(font)
        f = self.font()
        self._completion_popup.setFont(f)
        self._update_line_number_area_width()
        self._fix_all_line_heights()

    def current_line_height(self) -> int:
        return (
            self._line_height_override
            if self._line_height_override is not None
            else self._line_height()
        )

    def set_line_height(self, value: int | None) -> None:
        self._line_height_override = value
        self._fix_all_line_heights()

    def _line_height(self) -> int:
        if self._line_height_override is not None:
            return self._line_height_override
        return round(self.fontMetrics().height() * 1.2)

    def _block_fmt(self) -> QTextBlockFormat:
        fmt = QTextBlockFormat()
        fmt.setLineHeight(float(self._line_height()), 2)  # 2 = FixedHeight
        return fmt

    def setPlainText(self, text: str) -> None:
        super().setPlainText(text)
        # setPlainText はundoスタックをクリアする。その後 _fix_all_line_heights が
        # エントリを作らないよう一時的にundoを無効化する（スタックは既に空）。
        self.document().setUndoRedoEnabled(False)
        self._fix_all_line_heights()
        self.document().setUndoRedoEnabled(True)

    def _fix_all_line_heights(self):
        fmt = self._block_fmt()
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.setBlockFormat(fmt)
        cursor.endEditBlock()

    def line_number_area_width(self) -> int:
        digits = max(3, len(str(self.document().blockCount())))
        char_width = self.fontMetrics().horizontalAdvance("9")
        return char_width * digits + _LINE_NUMBER_PADDING * 2

    def _update_line_number_area_width(self):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )

    def keyPressEvent(self, event) -> None:
        key = event.key()
        text = event.text()

        self._hover_timer.stop()

        # ── 補完ポップアップ操作 ──────────────────────────────
        if self._completion_popup.isVisible():
            if key == Qt.Key.Key_Escape:
                self._completion_popup.hide()
                return
            elif key == Qt.Key.Key_Tab:
                self._accept_completion()
                return
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._completion_popup.currentRow() >= 0:
                    self._accept_completion()
                    return
            elif key == Qt.Key.Key_Up:
                row = self._completion_popup.currentRow()
                self._completion_popup.setCurrentRow(max(0, row - 1))
                return
            elif key == Qt.Key.Key_Down:
                row = self._completion_popup.currentRow()
                self._completion_popup.setCurrentRow(
                    min(self._completion_popup.count() - 1, row + 1)
                )
                return

        # Escape で sig help を閉じる（completion popup がない場合）
        if key == Qt.Key.Key_Escape and self._sig_help_widget.isVisible():
            if not self._completion_popup.isVisible():
                self._sig_help_widget.hide()
                return

        # ── 通常キー処理 ──────────────────────────────────────
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._completion_popup.hide()
            self._completion_timer.stop()
            self._sig_help_widget.hide()
            self._insert_newline_with_indent()
        elif key == Qt.Key.Key_Backspace:
            self._smart_backspace(event)
        elif text in _AUTO_CLOSE:
            self._insert_auto_close(text)
        elif text in _CLOSE_CHARS:
            self._skip_or_insert_close(text, event)
        else:
            super().keyPressEvent(event)

        # ── 補完トリガー判定 ──────────────────────────────────
        if text and (text.isalnum() or text == "_"):
            if self._completion_popup.isVisible():
                self._filter_completion()
            else:
                self._completion_timer.start()
        elif key == Qt.Key.Key_Backspace:
            if self._completion_popup.isVisible():
                self._filter_completion()
        elif text == ".":
            self._completion_popup.hide()
            self._completion_timer.start()
        else:
            self._completion_popup.hide()
            self._completion_timer.stop()

        # ── シグネチャヘルプトリガー ──────────────────────────
        if text == "(":
            cursor = self.textCursor()
            # auto-close で () 挿入後、カーソルは ( と ) の間にある
            # ( の位置 = cursor.position() - 1
            self._sig_help_open_pos = cursor.position() - 1
            self.signature_help_requested.emit(cursor.blockNumber(), cursor.positionInBlock())
        elif text == "," and self._sig_help_widget.isVisible():
            cursor = self.textCursor()
            self.signature_help_requested.emit(cursor.blockNumber(), cursor.positionInBlock())

    # ── 補完メソッド ─────────────────────────────────────────

    def _get_word_prefix(self, cursor: QTextCursor) -> str:
        col = cursor.positionInBlock()
        text = cursor.block().text()
        i = col
        while i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
            i -= 1
        return text[i:col]

    def _request_completion(self) -> None:
        cursor = self.textCursor()
        self._completion_block = cursor.blockNumber()
        self.completion_requested.emit(cursor.blockNumber(), cursor.positionInBlock())

    def show_completion(self, items: list[dict]) -> None:
        self._completion_items = items
        self._apply_completion_filter()

    def _filter_completion(self) -> None:
        self._apply_completion_filter()

    def _apply_completion_filter(self) -> None:
        prefix = self._get_word_prefix(self.textCursor())
        if prefix:
            filtered = [
                item for item in self._completion_items
                if item.get("label", "").lower().startswith(prefix.lower())
            ]
        else:
            filtered = self._completion_items

        # sortText があればそれで、なければ label で並び替え
        filtered = sorted(
            filtered,
            key=lambda item: str(item.get("sortText") or item.get("label", "")),
        )

        if not filtered:
            self._completion_popup.hide()
            return

        self._completion_popup.clear()
        for item in filtered[:50]:
            wi = QListWidgetItem()
            wi.setData(Qt.ItemDataRole.UserRole, item)
            wi.setText(item.get("label", ""))  # アクセシビリティ用
            self._completion_popup.addItem(wi)
        self._completion_popup.setCurrentRow(0)
        self._reposition_completion_popup()
        self._completion_popup.raise_()
        self._completion_popup.show()

    def _reposition_completion_popup(self) -> None:
        rect = self.cursorRect()
        x = rect.left()
        y = rect.bottom() + 2
        count = min(self._completion_popup.count(), 10)
        row_h = self._completion_popup.sizeHintForRow(0) if self._completion_popup.count() else 20
        self._completion_popup.move(x, y)
        self._completion_popup.resize(300, count * row_h + 4)

    def _accept_completion(self) -> None:
        wi = self._completion_popup.currentItem()
        if wi is None:
            self._completion_popup.hide()
            return
        comp_item: dict = wi.data(Qt.ItemDataRole.UserRole) or {}
        insert_text = comp_item.get("insertText") or comp_item.get("label", "")
        additional_edits = comp_item.get("additionalTextEdits")

        cursor = self.textCursor()
        col = cursor.positionInBlock()
        block_text = cursor.block().text()
        word_start = col
        while word_start > 0 and (block_text[word_start - 1].isalnum() or block_text[word_start - 1] == "_"):
            word_start -= 1
        cursor.setPosition(cursor.block().position() + word_start)
        cursor.setPosition(cursor.block().position() + col, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(insert_text)
        self.setTextCursor(cursor)
        self._completion_popup.hide()

        if additional_edits:
            self._apply_additional_text_edits(additional_edits)
        else:
            # additionalTextEdits が未解決の場合は resolve してから適用
            self.resolve_requested.emit(comp_item)

    def _apply_additional_text_edits(self, edits: list[dict]) -> None:
        """LSP additionalTextEdits をドキュメントに適用する（auto-import など）"""
        if not edits:
            return
        doc = self.document()
        # 下から上の順で適用（位置ずれを防ぐ）
        sorted_edits = sorted(
            edits,
            key=lambda e: (
                e.get("range", {}).get("start", {}).get("line", 0),
                e.get("range", {}).get("start", {}).get("character", 0),
            ),
            reverse=True,
        )
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        for edit in sorted_edits:
            r = edit.get("range", {})
            start = r.get("start", {})
            end = r.get("end", {})
            new_text = edit.get("newText", "")
            start_block = doc.findBlockByNumber(start.get("line", 0))
            end_block = doc.findBlockByNumber(end.get("line", 0))
            if not start_block.isValid() or not end_block.isValid():
                continue
            start_pos = start_block.position() + min(start.get("character", 0), start_block.length() - 1)
            end_pos = end_block.position() + min(end.get("character", 0), end_block.length() - 1)
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(new_text)
        cursor.endEditBlock()

    def apply_resolve(self, item: dict) -> None:
        """completionItem/resolve のレスポンスを受け取り additionalTextEdits を適用する"""
        edits = item.get("additionalTextEdits")
        if edits:
            self._apply_additional_text_edits(edits)

    def _on_cursor_for_completion(self) -> None:
        if not self._completion_popup.isVisible():
            return
        cursor = self.textCursor()
        # 別の行に移動した場合は非表示
        if cursor.blockNumber() != self._completion_block:
            self._completion_popup.hide()
            self._completion_timer.stop()

    def _on_cursor_for_sig_help(self) -> None:
        if not self._sig_help_widget.isVisible():
            return
        pos = self.textCursor().position()
        if pos <= self._sig_help_open_pos:
            self._sig_help_widget.hide()
            return
        # 開き括弧からカーソルまでの括弧深さを確認
        doc = self.document()
        depth = 0
        for i in range(self._sig_help_open_pos, pos):
            ch = doc.characterAt(i)
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    self._sig_help_widget.hide()
                    return

    def show_signature_help(self, result: dict) -> None:
        if not result:
            self._sig_help_widget.hide()
            return
        signatures = result.get("signatures") or []
        if not signatures:
            self._sig_help_widget.hide()
            return

        active_sig = result.get("activeSignature") or 0
        if active_sig >= len(signatures):
            active_sig = 0
        sig = signatures[active_sig]
        active_param = sig.get("activeParameter") if sig.get("activeParameter") is not None \
            else (result.get("activeParameter") or 0)

        self._sig_help_label.setText(_build_sig_html(sig, active_param))
        self._sig_help_label.adjustSize()

        pad = 6
        lw = self._sig_help_label.sizeHint().width()
        lh = self._sig_help_label.sizeHint().height()

        rect = self.cursorRect()
        x = rect.left()
        y = rect.top() - lh - pad * 2 - 2
        if y < 0:
            y = rect.bottom() + 2
        vp = self.viewport()
        x = min(x, max(0, vp.width() - lw - pad * 2))

        self._sig_help_widget.setGeometry(x, y, lw + pad * 2, lh + pad * 2)
        self._sig_help_label.setGeometry(pad, pad, lw, lh)
        self._sig_help_widget.raise_()
        self._sig_help_widget.show()
        self._sig_help_block = self.textCursor().blockNumber()

    def eventFilter(self, obj, event) -> bool:
        if not hasattr(self, "_completion_popup"):
            return super().eventFilter(obj, event)
        if obj is self._completion_popup and event.type() == QEvent.Type.Hide:
            self._completion_doc.hide()
        elif obj is self._completion_popup.viewport() and event.type() == QEvent.Type.MouseMove:
            item = self._completion_popup.itemAt(event.position().toPoint())
            if item:
                self._show_completion_doc(item)
            else:
                self._completion_doc.hide()
        return super().eventFilter(obj, event)

    def _show_completion_doc(self, wi: QListWidgetItem) -> None:
        comp_item: dict = wi.data(Qt.ItemDataRole.UserRole) or {}
        detail = comp_item.get("detail", "")
        doc = comp_item.get("documentation", "")
        if isinstance(doc, dict):
            doc = doc.get("value", "")
        text = "\n\n".join(filter(None, [detail, doc]))
        if not text:
            self._completion_doc.hide()
            return

        self._completion_doc_label.setText(_hover_md_to_html(text))

        popup = self._completion_popup
        pad = 6
        max_w = 300
        max_h = 200
        label_w = max_w - pad * 2
        self._completion_doc_label.setFixedWidth(label_w)
        self._completion_doc_label.adjustSize()
        label_h = min(self._completion_doc_label.sizeHint().height(), max_h - pad * 2)
        panel_w = max_w
        panel_h = label_h + pad * 2

        x = popup.x() + popup.width() + 2
        y = popup.y()
        # はみ出す場合は左に配置
        if x + panel_w > self.viewport().width():
            x = max(0, popup.x() - panel_w - 2)

        self._completion_doc.setGeometry(x, y, panel_w, panel_h)
        self._completion_doc_label.setGeometry(pad, pad, label_w, label_h)
        self._completion_doc.raise_()
        self._completion_doc.show()

    # ── スマートバックスペース ────────────────────────────────

    def _smart_backspace(self, event) -> None:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            pos = cursor.position()
            doc = self.document()
            col = cursor.positionInBlock()
            text_before = cursor.block().text()[:col]

            # ( | ) のようにカーソルが括弧ペアの中にある場合、両方を削除
            if pos > 0 and pos < doc.characterCount() - 1:
                prev_char = doc.characterAt(pos - 1)
                next_char = doc.characterAt(pos)
                if _AUTO_CLOSE.get(prev_char) == next_char:
                    cursor.setPosition(pos - 1)
                    cursor.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
                    cursor.removeSelectedText()
                    self.setTextCursor(cursor)
                    return

            # 行頭からカーソルまで全てスペースの場合、タブ幅単位で削除
            if text_before and text_before == " " * col:
                n = (col - 1) % 4 + 1
                for _ in range(n):
                    cursor.movePosition(
                        QTextCursor.MoveOperation.PreviousCharacter,
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                cursor.removeSelectedText()
                self.setTextCursor(cursor)
                return
        super().keyPressEvent(event)

    def _insert_newline_with_indent(self) -> None:
        cursor = self.textCursor()
        line = cursor.block().text()

        # 現在行の先頭インデントを取得
        indent = line[: len(line) - len(line.lstrip())]

        # カーソル位置までの文字（末尾空白除去）が ':' で終わる場合、インデントを追加
        text_before_cursor = line[: cursor.positionInBlock()].rstrip()
        if text_before_cursor.endswith(":"):
            indent += "    "

        cursor.insertText("\n" + indent)
        self.setTextCursor(cursor)

    def _insert_auto_close(self, open_char: str) -> None:
        close_char = _AUTO_CLOSE[open_char]
        cursor = self.textCursor()
        # クォートのように開閉が同じ文字の場合、次の文字が同じならスキップ
        if open_char == close_char and not cursor.hasSelection():
            doc = self.document()
            pos = cursor.position()
            if pos < doc.characterCount() - 1 and doc.characterAt(pos) == close_char:
                cursor.movePosition(QTextCursor.MoveOperation.NextCharacter)
                self.setTextCursor(cursor)
                return
        if cursor.hasSelection():
            selected = cursor.selectedText()
            cursor.insertText(open_char + selected + close_char)
        else:
            cursor.insertText(open_char + close_char)
            cursor.movePosition(QTextCursor.MoveOperation.PreviousCharacter)
            self.setTextCursor(cursor)

    def _skip_or_insert_close(self, char: str, event) -> None:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            doc = self.document()
            if cursor.position() < doc.characterCount() - 1:
                if doc.characterAt(cursor.position()) == char:
                    cursor.movePosition(QTextCursor.MoveOperation.NextCharacter)
                    self.setTextCursor(cursor)
                    return
        super().keyPressEvent(event)

    def _update_extra_selections(self) -> None:
        # 現在行ハイライト
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#e8ebf2"))  # Base より少し暗い
        fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
        line_sel = QTextEdit.ExtraSelection()
        line_sel.cursor = self.textCursor()
        line_sel.cursor.clearSelection()
        line_sel.format = fmt
        self.setExtraSelections([line_sel] + self._diagnostic_selections)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        global_pos = event.globalPosition().toPoint()
        doc = self.document()
        cursor = self.cursorForPosition(event.position().toPoint())
        pos = cursor.position()

        # 診断ホバー（優先）
        for diag in self._diagnostics:
            r = diag.get("range", {})
            start = r.get("start", {})
            end = r.get("end", {})
            start_block = doc.findBlockByNumber(start.get("line", 0))
            end_block = doc.findBlockByNumber(end.get("line", 0))
            if not start_block.isValid() or not end_block.isValid():
                continue
            start_pos = start_block.position() + start.get("character", 0)
            end_pos = end_block.position() + end.get("character", 0)
            if start_pos <= pos <= end_pos:
                code = diag.get("code", "")
                message = diag.get("message", "")
                text = f"[{code}] {message}" if code else message
                QToolTip.showText(global_pos, text, self)
                self._hover_timer.stop()
                return

        # 最終行より下はホバー対象外
        doc = self.document()
        last_rect = doc.documentLayout().blockBoundingRect(doc.lastBlock())
        scroll_y = self.verticalScrollBar().value()
        if event.position().y() > last_rect.bottom() - scroll_y:
            QToolTip.hideText()
            self._hover_timer.stop()
            return

        # LSP ホバー（遅延リクエスト）
        QToolTip.hideText()
        self._hover_global_pos = global_pos
        self._hover_timer.start()

    def _request_hover(self) -> None:
        cursor = self.cursorForPosition(
            self.viewport().mapFromGlobal(self._hover_global_pos)
        )
        self.hover_requested.emit(cursor.blockNumber(), cursor.positionInBlock())

    def show_hover(self, result: dict) -> None:
        contents = result.get("contents", "")
        if isinstance(contents, str):
            text = contents
        elif isinstance(contents, dict):
            text = contents.get("value", "")
        elif isinstance(contents, list):
            text = "\n\n".join(
                c.get("value", c) if isinstance(c, dict) else str(c)
                for c in contents
            )
        else:
            text = ""
        text = text.strip()
        if text:
            QToolTip.showText(self._hover_global_pos, _hover_md_to_html(text), self)

    def set_diagnostics(self, diagnostics: list) -> None:
        """LSP診断をwavy underlineでエディタに表示する。"""
        self._diagnostics = diagnostics
        _severity_colors = {
            1: QColor("#d20f39"),  # Error   — Catppuccin Red
            2: QColor("#df8e1d"),  # Warning — Catppuccin Yellow
            3: QColor("#1e66f5"),  # Info    — Catppuccin Blue
            4: QColor("#8c8fa1"),  # Hint    — Catppuccin Overlay1
        }
        _severity_bg = {
            1: QColor("#fce8ec"),  # Error bg
            2: QColor("#fdf3dc"),  # Warning bg
            3: QColor("#e4ecfe"),  # Info bg
            4: QColor("#f0f1f4"),  # Hint bg
        }
        doc = self.document()
        selections: list[QTextEdit.ExtraSelection] = []
        for diag in diagnostics:
            r = diag.get("range", {})
            start = r.get("start", {})
            end = r.get("end", {})
            start_block = doc.findBlockByNumber(start.get("line", 0))
            end_block = doc.findBlockByNumber(end.get("line", 0))
            if not start_block.isValid() or not end_block.isValid():
                continue

            start_char = min(start.get("character", 0), start_block.length() - 1)
            end_char = min(end.get("character", 0), end_block.length() - 1)
            cursor = QTextCursor(start_block)
            cursor.setPosition(start_block.position() + start_char)
            cursor.setPosition(
                end_block.position() + end_char,
                QTextCursor.MoveMode.KeepAnchor,
            )

            severity = diag.get("severity", 1)
            fmt = QTextCharFormat()
            fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            fmt.setUnderlineColor(_severity_colors.get(severity, _severity_colors[1]))
            fmt.setBackground(_severity_bg.get(severity, _severity_bg[1]))

            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = fmt
            selections.append(sel)

        self._diagnostic_selections = selections
        self._update_extra_selections()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        self._paint_indent_guides(event)

    def _paint_indent_guides(self, event) -> None:
        doc = self.document()
        doc_layout = doc.documentLayout()
        scroll_y = self.verticalScrollBar().value()
        scroll_x = self.horizontalScrollBar().value()
        doc_margin = doc.documentMargin()
        tab_px = self.fontMetrics().horizontalAdvance(" ") * 4

        painter = QPainter(self.viewport())
        painter.setPen(QColor("#d0d3de"))  # Surface0 より少し明るい

        block = doc.begin()
        while block.isValid():
            rect = doc_layout.blockBoundingRect(block)
            top = rect.top() - scroll_y
            bottom = rect.bottom() - scroll_y

            if top > event.rect().bottom():
                break

            if bottom >= event.rect().top():
                text = block.text()
                if text.strip():  # 空行はスキップ
                    indent = len(text) - len(text.lstrip(" "))
                    for level in range(0, indent // 4):
                        x = doc_margin - scroll_x + level * tab_px
                        painter.drawLine(int(x), int(top), int(x), int(bottom) - 1)

            block = block.next()
        painter.end()

    def paint_line_numbers(self, event):
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), LINE_NUMBER_BG)

        doc_layout = self.document().documentLayout()
        scroll_y = self.verticalScrollBar().value()
        area_width = self._line_number_area.width()
        line_height = self._line_height()
        current_block_num = self.textCursor().blockNumber()

        block = self.document().begin()
        while block.isValid():
            rect = doc_layout.blockBoundingRect(block)
            top = int(rect.top()) - scroll_y
            bottom = int(rect.bottom()) - scroll_y
            if top > event.rect().bottom():
                break
            if bottom >= event.rect().top():
                is_current = block.blockNumber() == current_block_num
                painter.setPen(EDITOR_FG if is_current else LINE_NUMBER_FG)
                painter.drawText(
                    0,
                    top,
                    area_width - _LINE_NUMBER_PADDING,
                    line_height,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(block.blockNumber() + 1),
                )
            block = block.next()


class KakuEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self._file_path: Path | None = None
        self._is_modified = False

        # LSP
        self._lsp_ruff = LspClient()
        self._lsp_ty = LspClient()
        self._lsp_ruff_active = False
        self._lsp_ty_active = False
        self._lsp_uri: str | None = None
        self._lsp_version = 1
        self._lsp_timer = QTimer(self)
        self._lsp_timer.setSingleShot(True)
        self._lsp_timer.setInterval(500)
        self._lsp_timer.timeout.connect(self._send_lsp_change)
        self._lsp_ruff.diagnostics_received.connect(
            lambda uri, d: self._on_lsp_diagnostics(uri, d, "ruff")
        )
        self._lsp_ty.diagnostics_received.connect(
            lambda uri, d: self._on_lsp_diagnostics(uri, d, "ty")
        )
        self._lsp_ruff.completion_received.connect(
            lambda items: self._on_lsp_completion(items, "ruff")
        )
        self._lsp_ty.completion_received.connect(
            lambda items: self._on_lsp_completion(items, "ty")
        )
        self._lsp_ruff.hover_received.connect(self._on_lsp_hover)
        self._lsp_ty.hover_received.connect(self._on_lsp_hover)
        self._lsp_ruff.resolve_received.connect(self._on_lsp_resolve)
        self._lsp_ty.resolve_received.connect(self._on_lsp_resolve)
        self._lsp_ty.signature_help_received.connect(self._on_lsp_signature_help)
        self._lsp_ruff.signature_help_received.connect(self._on_lsp_signature_help)
        self._diagnostics_by_source: dict[str, list] = {"ruff": [], "ty": []}
        self._completion_results: dict[str, list] = {"ruff": [], "ty": []}

        self._setup_ui()
        self._setup_menu()
        self._update_title()

        self._lsp_ruff_active = self._lsp_ruff.start([sys.executable, "-m", "ruff", "server"])
        if self._lsp_ruff_active:
            self._lsp_ruff.initialize()
        self._lsp_ty_active = self._lsp_ty.start([sys.executable, "-m", "ty", "server"])
        if self._lsp_ty_active:
            self._lsp_ty.initialize()

    def _setup_ui(self):
        self.setMinimumSize(800, 600)

        self._editor = CodeEditor()
        self._editor.setFont(self._default_font())
        self._editor.setTabStopDistance(40)
        palette = self._editor.palette()
        palette.setColor(palette.ColorRole.Base, EDITOR_BG)
        palette.setColor(palette.ColorRole.Text, EDITOR_FG)
        self._editor.setPalette(palette)
        self.setCentralWidget(self._editor)

        self._editor.textChanged.connect(self._on_text_changed)
        self._editor.completion_requested.connect(self._on_completion_requested)
        self._editor.hover_requested.connect(self._on_hover_requested)
        self._editor.resolve_requested.connect(self._on_resolve_requested)
        self._editor.signature_help_requested.connect(self._on_sig_help_requested)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._diag_label = QLabel()
        self._diag_label.setStyleSheet("QLabel { margin-right: 4px; }")
        self._status_bar.addPermanentWidget(self._diag_label)
        self._editor.cursorPositionChanged.connect(self._update_cursor_position)
        self._update_cursor_position()

    def _setup_menu(self):
        menu_bar = self.menuBar()

        # ファイルメニュー
        file_menu = menu_bar.addMenu("ファイル(&F)")

        new_action = QAction("新規(&N)", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self._new_file)
        file_menu.addAction(new_action)

        open_action = QAction("開く(&O)...", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._open_file)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        save_action = QAction("保存(&S)", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._save_file)
        file_menu.addAction(save_action)

        save_as_action = QAction("名前を付けて保存(&A)...", self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(self._save_file_as)
        file_menu.addAction(save_as_action)

        file_menu.addSeparator()

        quit_action = QAction("終了(&Q)", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # 編集メニュー
        edit_menu = menu_bar.addMenu("編集(&E)")

        # QTextEdit がこれらのショートカットをネイティブに処理するため、
        # QAction にはショートカットを設定しない（設定するとmacOSメニューがチカチカする）。
        undo_action = QAction("元に戻す", self)
        undo_action.triggered.connect(self._editor.undo)
        edit_menu.addAction(undo_action)

        redo_action = QAction("やり直し", self)
        redo_action.triggered.connect(self._editor.redo)
        edit_menu.addAction(redo_action)

        edit_menu.addSeparator()

        cut_action = QAction("切り取り", self)
        cut_action.triggered.connect(self._editor.cut)
        edit_menu.addAction(cut_action)

        copy_action = QAction("コピー", self)
        copy_action.triggered.connect(self._editor.copy)
        edit_menu.addAction(copy_action)

        paste_action = QAction("貼り付け", self)
        paste_action.triggered.connect(self._editor.paste)
        edit_menu.addAction(paste_action)

        edit_menu.addSeparator()

        select_all_action = QAction("すべて選択", self)
        select_all_action.triggered.connect(self._editor.selectAll)
        edit_menu.addAction(select_all_action)

        # 表示メニュー
        view_menu = menu_bar.addMenu("表示(&V)")

        font_action = QAction("フォント(&F)...", self)
        font_action.triggered.connect(self._change_font)
        view_menu.addAction(font_action)

        line_height_action = QAction("行の高さ(&L)...", self)
        line_height_action.triggered.connect(self._change_line_height)
        view_menu.addAction(line_height_action)

    def _default_font(self) -> QFont:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(_DEFAULT_FONT_SIZE)
        return self._with_cjk_fallback(font)

    def _with_cjk_fallback(self, font: QFont) -> QFont:
        fallback = self._find_cjk_fixed_font()
        if fallback and fallback != font.family():
            font.setFamilies([font.family(), fallback])
        return font

    @staticmethod
    def _find_cjk_fixed_font() -> str | None:
        candidates = [
            fam
            for fam in QFontDatabase.families()
            if QFontDatabase.isFixedPitch(fam)
            and QFontDatabase.WritingSystem.Japanese
            in QFontDatabase.writingSystems(fam)
        ]
        if not candidates:
            return None

        # leading=0 を優先し、同じなら lineSpacing が小さいものを選ぶ
        def metrics_key(fam: str) -> tuple[int, int]:
            m = QFontMetrics(QFont(fam))
            return (m.leading(), m.lineSpacing())

        return min(candidates, key=metrics_key)

    def _change_font(self):
        ok, font = QFontDialog.getFont(self._editor.font(), self, "フォントの選択")
        if ok:
            self._editor.setFont(self._with_cjk_fallback(font))

    def _change_line_height(self):
        current = self._editor.current_line_height()
        value, ok = QInputDialog.getInt(
            self, "行の高さ", "行の高さ (px):", current, 1, 200
        )
        if ok:
            self._editor.set_line_height(value)

    def _on_text_changed(self):
        if not self._is_modified:
            self._is_modified = True
            self._update_title()
        if (self._lsp_ruff_active or self._lsp_ty_active) and self._lsp_uri:
            self._lsp_timer.start()

    def _send_lsp_change(self) -> None:
        if not self._lsp_uri:
            return
        self._lsp_version += 1
        text = self._editor.toPlainText()
        if self._lsp_ruff_active:
            self._lsp_ruff.did_change(self._lsp_uri, text, self._lsp_version)
        if self._lsp_ty_active:
            self._lsp_ty.did_change(self._lsp_uri, text, self._lsp_version)

    def _on_lsp_diagnostics(self, uri: str, diagnostics: list, source: str) -> None:
        if uri == self._lsp_uri:
            self._diagnostics_by_source[source] = diagnostics
            merged = self._diagnostics_by_source["ruff"] + self._diagnostics_by_source["ty"]
            self._editor.set_diagnostics(merged)
            self._update_diag_label(merged)

    def _update_diag_label(self, diagnostics: list) -> None:
        errors = sum(1 for d in diagnostics if d.get("severity") == 1)
        warnings = sum(1 for d in diagnostics if d.get("severity") == 2)
        parts = []
        if errors:
            parts.append(f'<span style="color:#d20f39;">✕ {errors}</span>')
        if warnings:
            parts.append(f'<span style="color:#df8e1d;">⚠ {warnings}</span>')
        self._diag_label.setText("  ".join(parts) if parts else "")

    def _on_completion_requested(self, line: int, character: int) -> None:
        # 補完前に最新の変更を即座にLSPへ通知（didChangeタイマーを待たない）
        if self._lsp_uri and self._lsp_timer.isActive():
            self._lsp_timer.stop()
            self._send_lsp_change()

        self._completion_results = {"ruff": [], "ty": []}
        if self._lsp_uri:
            if self._lsp_ruff_active:
                self._lsp_ruff.completion(self._lsp_uri, line, character)
            if self._lsp_ty_active:
                self._lsp_ty.completion(self._lsp_uri, line, character)

    def _on_hover_requested(self, line: int, character: int) -> None:
        if self._lsp_uri:
            if self._lsp_ruff_active:
                self._lsp_ruff.hover(self._lsp_uri, line, character)
            if self._lsp_ty_active:
                self._lsp_ty.hover(self._lsp_uri, line, character)

    def _on_lsp_hover(self, result: dict) -> None:
        self._editor.show_hover(result)

    def _on_lsp_completion(self, items: list, source: str) -> None:
        # source タグを付与（resolve 時にどのサーバーへ送るか判別するため）
        tagged = [{**item, "_lsp_source": source} for item in items]
        self._completion_results[source] = tagged
        # ty 優先でマージ（同ラベルは ty を残す）
        ty_labels = {item.get("label") for item in self._completion_results["ty"]}
        unique_ruff = [
            item for item in self._completion_results["ruff"]
            if item.get("label") not in ty_labels
        ]
        merged = self._completion_results["ty"] + unique_ruff
        self._editor.show_completion(merged)

    def _on_resolve_requested(self, item: dict) -> None:
        source = item.get("_lsp_source", "")
        # _lsp_source はクライアント内部フィールドなので LSP サーバーには送らない
        clean = {k: v for k, v in item.items() if not k.startswith("_")}
        if source == "ruff" and self._lsp_ruff_active:
            self._lsp_ruff.resolve(clean)
        elif source == "ty" and self._lsp_ty_active:
            self._lsp_ty.resolve(clean)
        elif self._lsp_ruff_active:
            self._lsp_ruff.resolve(clean)
        elif self._lsp_ty_active:
            self._lsp_ty.resolve(clean)

    def _on_lsp_resolve(self, item: dict) -> None:
        self._editor.apply_resolve(item)

    def _on_sig_help_requested(self, line: int, character: int) -> None:
        if not self._lsp_uri:
            return
        # did_change を即時送信してから sig help を要求
        if self._lsp_timer.isActive():
            self._lsp_timer.stop()
            self._send_lsp_change()
        if self._lsp_ty_active:
            self._lsp_ty.signature_help(self._lsp_uri, line, character)
        elif self._lsp_ruff_active:
            self._lsp_ruff.signature_help(self._lsp_uri, line, character)

    def _on_lsp_signature_help(self, result: dict) -> None:
        self._editor.show_signature_help(result)

    def _update_title(self):
        name = self._file_path.name if self._file_path else "無題"
        modified = " *" if self._is_modified else ""
        self.setWindowTitle(f"{name}{modified} - Kaku")

    def _update_cursor_position(self):
        cursor = self._editor.textCursor()
        line = cursor.blockNumber() + 1
        col = cursor.columnNumber() + 1
        self._status_bar.showMessage(f"行 {line}, 列 {col}")

    def _confirm_discard(self) -> bool:
        if not self._is_modified:
            return True
        name = self._file_path.name if self._file_path else "無題"
        reply = QMessageBox.question(
            self,
            "未保存の変更",
            f"'{name}' への変更を保存しますか？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Save:
            return self._save_file()
        if reply == QMessageBox.StandardButton.Discard:
            return True
        return False

    def _new_file(self):
        if not self._confirm_discard():
            return
        self._close_lsp_document()
        self._editor.clear()
        self._editor.set_diagnostics([])
        self._update_diag_label([])
        self._file_path = None
        self._is_modified = False
        self._update_title()

    def _open_file(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "ファイルを開く", "", "すべてのファイル (*)"
        )
        if not path:
            return
        self._load_file(Path(path))

    def _close_lsp_document(self) -> None:
        if self._lsp_uri:
            self._lsp_timer.stop()
            if self._lsp_ruff_active:
                self._lsp_ruff.did_close(self._lsp_uri)
            if self._lsp_ty_active:
                self._lsp_ty.did_close(self._lsp_uri)
            self._lsp_uri = None
            self._diagnostics_by_source = {"ruff": [], "ty": []}

    def _load_file(self, path: Path):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            QMessageBox.critical(
                self, "エラー", f"テキストファイルではないため開けません:\n{path.name}"
            )
            return
        except OSError as e:
            QMessageBox.critical(self, "エラー", f"ファイルを開けませんでした:\n{e}")
            return
        self._close_lsp_document()
        self._editor.setPlainText(text)
        self._file_path = path
        self._is_modified = False
        self._update_title()
        if self._lsp_ruff_active or self._lsp_ty_active:
            self._lsp_uri = path.as_uri()
            self._lsp_version = 1
            if self._lsp_ruff_active:
                self._lsp_ruff.did_open(self._lsp_uri, text)
            if self._lsp_ty_active:
                self._lsp_ty.did_open(self._lsp_uri, text)

    def _save_file(self) -> bool:
        if self._file_path is None:
            return self._save_file_as()
        return self._write_file(self._file_path)

    def _save_file_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "名前を付けて保存",
            "",
            "Pythonファイル (*.py);;テキストファイル (*.txt);;すべてのファイル (*)",
        )
        if not path:
            return False
        return self._write_file(Path(path))

    def _write_file(self, path: Path) -> bool:
        try:
            path.write_text(self._editor.toPlainText(), encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(
                self, "エラー", f"ファイルを保存できませんでした:\n{e}"
            )
            return False
        self._file_path = path
        self._is_modified = False
        self._update_title()
        return True

    def closeEvent(self, event):
        if self._confirm_discard():
            self._lsp_ruff.stop()
            self._lsp_ty.stop()
            event.accept()
        else:
            event.ignore()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Kaku")
    app.setStyleSheet(
        "QToolTip { font-size: 13px; background-color: #eff1f5;"
        " color: #4c4f69; border: 1px solid #bcc0cc; }"
    )

    window = KakuEditor()
    window.show()

    if len(sys.argv) > 1:
        window._load_file(Path(sys.argv[1]).resolve())

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
