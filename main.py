import sys
from collections.abc import Sequence
from pathlib import Path

from kaku.document import TextDocument
from kaku.highlighter import EDITOR_BG, EDITOR_FG, LINE_NUMBER_BG, LINE_NUMBER_FG, PythonHighlighter
from kaku.lsp import LspClient

from PySide6.QtCore import QRect, QSize, Qt, QTimer
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
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFontDialog,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTextEdit,
    QToolTip,
    QWidget,
)

_DEFAULT_FONT_SIZE = 13

_LINE_NUMBER_PADDING = 8  # 右側パディング (px)


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._editor.paint_line_numbers(event)


class CodeEditor(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        self._line_number_area = LineNumberArea(self)
        self._line_height_override: int | None = None
        self._text_document = TextDocument(self.document())
        self._highlighter = PythonHighlighter(self._text_document)
        self._diagnostics: list[dict] = []
        self.setMouseTracking(True)

        self.document().blockCountChanged.connect(self._update_line_number_area_width)
        self.verticalScrollBar().valueChanged.connect(self._line_number_area.update)
        self.document().contentsChanged.connect(self._line_number_area.update)

        self._update_line_number_area_width()

    @property
    def text_document(self) -> TextDocument:
        return self._text_document

    def setFont(self, font: QFont | str | Sequence[str]):
        super().setFont(font)
        self._update_line_number_area_width()
        self._fix_all_line_heights()

    def current_line_height(self) -> int:
        return self._line_height_override if self._line_height_override is not None else self._line_height()

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

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        doc = self.document()
        pos = self.cursorForPosition(event.position().toPoint()).position()
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
                QToolTip.showText(event.globalPosition().toPoint(), text, self)
                return
        QToolTip.hideText()

    def set_diagnostics(self, diagnostics: list) -> None:
        """LSP診断をwavy underlineでエディタに表示する。"""
        self._diagnostics = diagnostics
        _severity_colors = {
            1: QColor("#d20f39"),  # Error   — Catppuccin Red
            2: QColor("#df8e1d"),  # Warning — Catppuccin Yellow
            3: QColor("#1e66f5"),  # Info    — Catppuccin Blue
            4: QColor("#8c8fa1"),  # Hint    — Catppuccin Overlay1
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

            cursor = QTextCursor(start_block)
            cursor.setPosition(start_block.position() + start.get("character", 0))
            cursor.setPosition(
                end_block.position() + end.get("character", 0),
                QTextCursor.MoveMode.KeepAnchor,
            )

            fmt = QTextCharFormat()
            fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            fmt.setUnderlineColor(_severity_colors.get(diag.get("severity", 1), _severity_colors[1]))

            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = fmt
            selections.append(sel)

        self.setExtraSelections(selections)

    def paint_line_numbers(self, event):
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), LINE_NUMBER_BG)
        painter.setPen(LINE_NUMBER_FG)

        doc_layout = self.document().documentLayout()
        scroll_y = self.verticalScrollBar().value()
        area_width = self._line_number_area.width()
        line_height = self._line_height()

        block = self.document().begin()
        while block.isValid():
            rect = doc_layout.blockBoundingRect(block)
            top = int(rect.top()) - scroll_y
            bottom = int(rect.bottom()) - scroll_y
            if top > event.rect().bottom():
                break
            if bottom >= event.rect().top():
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
        self._lsp = LspClient()
        self._lsp_active = False
        self._lsp_uri: str | None = None
        self._lsp_version = 1
        self._lsp_timer = QTimer(self)
        self._lsp_timer.setSingleShot(True)
        self._lsp_timer.setInterval(500)
        self._lsp_timer.timeout.connect(self._send_lsp_change)
        self._lsp.diagnostics_received.connect(self._on_lsp_diagnostics)

        self._setup_ui()
        self._setup_menu()
        self._update_title()

        self._lsp_active = self._lsp.start(["ruff", "server"])
        if self._lsp_active:
            self._lsp.initialize()

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

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
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
            fam for fam in QFontDatabase.families()
            if QFontDatabase.isFixedPitch(fam)
            and QFontDatabase.WritingSystem.Japanese in QFontDatabase.writingSystems(fam)
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
        if self._lsp_active and self._lsp_uri:
            self._lsp_timer.start()

    def _send_lsp_change(self) -> None:
        if self._lsp_uri:
            self._lsp_version += 1
            self._lsp.did_change(self._lsp_uri, self._editor.toPlainText(), self._lsp_version)

    def _on_lsp_diagnostics(self, uri: str, diagnostics: list) -> None:
        if uri == self._lsp_uri:
            self._editor.set_diagnostics(diagnostics)

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
        if self._lsp_active and self._lsp_uri:
            self._lsp_timer.stop()
            self._lsp.did_close(self._lsp_uri)
            self._lsp_uri = None

    def _load_file(self, path: Path):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            QMessageBox.critical(self, "エラー", f"テキストファイルではないため開けません:\n{path.name}")
            return
        except OSError as e:
            QMessageBox.critical(self, "エラー", f"ファイルを開けませんでした:\n{e}")
            return
        self._close_lsp_document()
        self._editor.setPlainText(text)
        self._file_path = path
        self._is_modified = False
        self._update_title()
        if self._lsp_active:
            self._lsp_uri = path.as_uri()
            self._lsp_version = 1
            self._lsp.did_open(self._lsp_uri, text)

    def _save_file(self) -> bool:
        if self._file_path is None:
            return self._save_file_as()
        return self._write_file(self._file_path)

    def _save_file_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "名前を付けて保存",
            "",
            "テキストファイル (*.txt);;すべてのファイル (*)",
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
            self._lsp.stop()
            event.accept()
        else:
            event.ignore()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Kaku")
    app.setStyleSheet("QToolTip { font-size: 13px; }")

    window = KakuEditor()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
