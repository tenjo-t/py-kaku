import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QStatusBar,
)


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

        self._editor = QPlainTextEdit()
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
            self, "ファイルを開く", "", "テキストファイル (*.txt);;すべてのファイル (*)"
        )
        if not path:
            return
        self._load_file(Path(path))

    def _load_file(self, path: Path):
        try:
            text = path.read_text(encoding="utf-8")
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
            self, "名前を付けて保存", "", "テキストファイル (*.txt);;すべてのファイル (*)"
        )
        if not path:
            return False
        return self._write_file(Path(path))

    def _write_file(self, path: Path) -> bool:
        try:
            path.write_text(self._editor.toPlainText(), encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "エラー", f"ファイルを保存できませんでした:\n{e}")
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
