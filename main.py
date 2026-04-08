import sys
from collections.abc import Sequence
from pathlib import Path

from kaku.document import TextDocument

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QTextBlockFormat,
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
    QWidget,
)

_DEFAULT_FONT_SIZE = 13

_LINE_NUMBER_BG = QColor("#f0f0f0")
_LINE_NUMBER_FG = QColor("#888888")
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
        self._fixing_line_height = False
        self._line_height_override: int | None = None
        self._text_document = TextDocument(self.document())

        self.document().blockCountChanged.connect(self._update_line_number_area_width)
        self.document().contentsChanged.connect(self._fix_all_line_heights)
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

    def _fix_all_line_heights(self):
        if self._fixing_line_height:
            return
        self._fixing_line_height = True
        fmt = self._block_fmt()
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.setBlockFormat(fmt)
        cursor.endEditBlock()
        self._fixing_line_height = False

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

    def paint_line_numbers(self, event):
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), _LINE_NUMBER_BG)
        painter.setPen(_LINE_NUMBER_FG)

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

        self._setup_ui()
        self._setup_menu()
        self._update_title()

    def _setup_ui(self):
        self.setMinimumSize(800, 600)

        self._editor = CodeEditor()
        self._editor.setFont(self._default_font())
        self._editor.setTabStopDistance(40)
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

        undo_action = QAction("元に戻す(&Z)", self)
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        undo_action.triggered.connect(self._editor.undo)
        edit_menu.addAction(undo_action)

        redo_action = QAction("やり直し(&Y)", self)
        redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        redo_action.triggered.connect(self._editor.redo)
        edit_menu.addAction(redo_action)

        edit_menu.addSeparator()

        cut_action = QAction("切り取り(&X)", self)
        cut_action.setShortcut(QKeySequence.StandardKey.Cut)
        cut_action.triggered.connect(self._editor.cut)
        edit_menu.addAction(cut_action)

        copy_action = QAction("コピー(&C)", self)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.triggered.connect(self._editor.copy)
        edit_menu.addAction(copy_action)

        paste_action = QAction("貼り付け(&V)", self)
        paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        paste_action.triggered.connect(self._editor.paste)
        edit_menu.addAction(paste_action)

        edit_menu.addSeparator()

        select_all_action = QAction("すべて選択(&A)", self)
        select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
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
        self._editor.clear()
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

    def _load_file(self, path: Path):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            QMessageBox.critical(self, "エラー", f"テキストファイルではないため開けません:\n{path.name}")
            return
        except OSError as e:
            QMessageBox.critical(self, "エラー", f"ファイルを開けませんでした:\n{e}")
            return
        self._editor.setPlainText(text)
        self._file_path = path
        self._is_modified = False
        self._update_title()

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
            event.accept()
        else:
            event.ignore()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Kaku")

    window = KakuEditor()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
