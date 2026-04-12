from __future__ import annotations

import bisect

import tree_sitter_python as tspython
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from tree_sitter import Language, Node, Parser

from kaku.document import TextChange, TextDocument

# ───────────────────────── カラーテーマ（Catppuccin Latte） ─────────────────────────

# エディタ全体で参照できるテーマ色
LINE_NUMBER_BG = QColor("#e6e9ef")  # Mantle
LINE_NUMBER_FG = QColor("#9ca0b0")  # Overlay0
EDITOR_BG = QColor("#eff1f5")       # Base
EDITOR_FG = QColor("#4c4f69")       # Text

_FORMATS: dict[str, QTextCharFormat] = {}


def _fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    if italic:
        f.setFontItalic(True)
    return f


_FORMATS = {
    "keyword":    _fmt("#8839ef", bold=True),   # Mauve
    "string":     _fmt("#40a02b"),               # Green
    "comment":    _fmt("#8c8fa1", italic=True),  # Overlay1
    "number":     _fmt("#fe640b"),               # Peach
    "func_def":   _fmt("#1e66f5", bold=True),    # Blue
    "class_def":  _fmt("#df8e1d", bold=True),    # Yellow
    "decorator":  _fmt("#ea76cb"),               # Pink
    "builtin":    _fmt("#179299"),               # Teal
    "func_call":  _fmt("#209fb5"),               # Sapphire
    # レインボーブラケット（ネスト深さ順）
    "bracket_0":  _fmt("#1e66f5"),               # Blue
    "bracket_1":  _fmt("#ea76cb"),               # Pink
    "bracket_2":  _fmt("#df8e1d"),               # Yellow
    "bracket_3":  _fmt("#40a02b"),               # Green
    "bracket_4":  _fmt("#8839ef"),               # Mauve
    "bracket_5":  _fmt("#fe640b"),               # Peach
}

_BRACKET_DEPTH = 6
_OPEN_BRACKETS  = {"(", "[", "{"}
_CLOSE_BRACKETS = {")", "]", "}"}

_KEYWORDS = frozenset({
    "def", "class", "if", "elif", "else", "for", "while", "return",
    "import", "from", "as", "with", "try", "except", "finally", "raise",
    "pass", "break", "continue", "and", "or", "not", "in", "is",
    "None", "True", "False", "lambda", "yield", "del", "global",
    "nonlocal", "assert", "async", "await", "match", "case",
})

_BUILTINS = frozenset({
    "print", "len", "range", "enumerate", "zip", "map", "filter",
    "list", "dict", "set", "tuple", "str", "int", "float", "bool",
    "type", "isinstance", "issubclass", "hasattr", "getattr", "setattr",
    "open", "super", "object", "property", "staticmethod", "classmethod",
    "abs", "all", "any", "min", "max", "sum", "sorted", "reversed",
    "input", "repr", "id", "hash", "iter", "next",
})

# ───────────────────────── ノードウォーク ─────────────────────────

# (start_byte, end_byte, format_key)
_Span = tuple[int, int, str]


def _is_func_call(node: Node, parent: Node | None, parent_type: str) -> bool:
    if parent is None:
        return False
    # foo() — identifier が call の function フィールド
    if parent_type == "call":
        fn = parent.child_by_field_name("function")
        return fn is not None and fn.start_byte == node.start_byte
    # obj.method() — identifier が attribute の attribute フィールド、かつ親が call
    if parent_type == "attribute":
        attr = parent.child_by_field_name("attribute")
        if attr is None or attr.start_byte != node.start_byte:
            return False
        grandparent = parent.parent
        return grandparent is not None and grandparent.type == "call"
    return False


def _collect_bracket_spans(node: Node, spans: list[_Span], depth: list[int]) -> None:
    t = node.type
    # 文字列・コメント内は走査しない
    if t in ("string", "concatenated_string", "comment"):
        return
    if t in _OPEN_BRACKETS:
        spans.append((node.start_byte, node.end_byte, f"bracket_{depth[0] % _BRACKET_DEPTH}"))
        depth[0] += 1
    elif t in _CLOSE_BRACKETS:
        depth[0] = max(0, depth[0] - 1)
        spans.append((node.start_byte, node.end_byte, f"bracket_{depth[0] % _BRACKET_DEPTH}"))
    for child in node.children:
        _collect_bracket_spans(child, spans, depth)


def _collect_spans(node: Node, spans: list[_Span],
                   range_start: int = 0, range_end: int = -1) -> None:
    # 指定範囲外のサブツリーをスキップ（range_end == -1 は全収集）
    if range_end >= 0 and (node.end_byte <= range_start or node.start_byte >= range_end):
        return

    t = node.type

    if t == "comment":
        spans.append((node.start_byte, node.end_byte, "comment"))
        return  # 子を見ない

    if t in ("string", "concatenated_string"):
        spans.append((node.start_byte, node.end_byte, "string"))
        return

    if t in ("integer", "float"):
        spans.append((node.start_byte, node.end_byte, "number"))

    elif t == "identifier":
        name = node.text.decode() if node.text else ""
        parent = node.parent
        parent_type = parent.type if parent else ""
        if parent_type == "function_definition":
            spans.append((node.start_byte, node.end_byte, "func_def"))
        elif parent_type == "class_definition":
            spans.append((node.start_byte, node.end_byte, "class_def"))
        elif _is_func_call(node, parent, parent_type):
            spans.append((node.start_byte, node.end_byte, "func_call"))
        elif name in _KEYWORDS:
            spans.append((node.start_byte, node.end_byte, "keyword"))
        elif name in _BUILTINS:
            spans.append((node.start_byte, node.end_byte, "builtin"))

    elif t == "decorator":
        spans.append((node.start_byte, node.end_byte, "decorator"))
        return

    elif not node.is_named and t in _KEYWORDS:
        spans.append((node.start_byte, node.end_byte, "keyword"))

    for child in node.children:
        _collect_spans(child, spans, range_start, range_end)


# ───────────────────────── ハイライター ─────────────────────────

class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, text_doc: TextDocument) -> None:
        super().__init__(text_doc._doc)

        lang = Language(tspython.language())
        self._parser = Parser(lang)
        self._text_doc = text_doc
        self._tree = None

        # block_number -> [(start_in_block, length, fmt)]
        self._cache: dict[int, list[tuple[int, int, QTextCharFormat]]] = {}
        # 構文スパンをバイト順に保持（ブラケット除く）
        self._all_spans: list[_Span] = []

        text_doc.changed.connect(self._on_changed)
        self._reparse_full(text_doc.text)

    def _reparse_full(self, text: str) -> None:
        """初回または強制フルパース。"""
        encoded = text.encode()
        self._tree = self._parser.parse(encoded)
        self._all_spans = []
        _collect_spans(self._tree.root_node, self._all_spans)
        self._rebuild_cache(text)
        self.rehighlight()

    def _reparse_incremental(self, text: str, change: TextChange) -> None:
        """差分パース：変更範囲のスパンのみ差し替える。"""
        encoded = text.encode()

        old_tree = self._tree
        self._tree = self._parser.parse(encoded, old_tree)

        # ── スパンをインクリメンタル更新 ──────────────────────
        delta = change.new_end_byte - change.old_end_byte
        kept: list[_Span] = []
        for s, e, fmt in self._all_spans:
            if e <= change.start_byte:
                kept.append((s, e, fmt))           # 変更前：そのまま保持
            elif s >= change.old_end_byte:
                kept.append((s + delta, e + delta, fmt))  # 変更後：バイト位置を調整
            # else: 変更範囲にかかる → 破棄して再収集
        # 変更範囲のみ再収集（サブツリー刈り込みで高速）
        _collect_spans(self._tree.root_node, kept,
                       change.start_byte, change.new_end_byte)
        kept.sort(key=lambda s: s[0])
        self._all_spans = kept

        # ── キャッシュ差分更新 ────────────────────────────────
        old_cache = dict(self._cache)
        self._rebuild_cache(text)

        all_keys = set(old_cache) | set(self._cache)
        changed = {k for k in all_keys if old_cache.get(k) != self._cache.get(k)}
        # findBlockByNumber は O(N) なので N 回呼ぶと O(N²) になる。
        # ドキュメントを先頭から順走査して O(N) に抑える。
        if changed:
            block = self.document().begin()
            while block.isValid():
                if block.blockNumber() in changed:
                    self.rehighlightBlock(block)
                block = block.next()

    def _rebuild_cache(self, text: str) -> None:
        self._cache.clear()
        if self._tree is None:
            return

        # ブラケットスパンは深さが累積するため毎回全収集
        bracket_spans: list[_Span] = []
        _collect_bracket_spans(self._tree.root_node, bracket_spans, [0])

        spans = self._all_spans + bracket_spans

        encoded = text.encode("utf-8")
        lines = text.split("\n")

        # バイトオフセット → 文字インデックスの対応表を一度だけ構築 O(N)
        byte_to_char: list[int] = [0] * (len(encoded) + 1)
        char_idx = 0
        i = 0
        while i < len(encoded):
            b = encoded[i]
            width = 1 if b < 0x80 else (2 if b < 0xE0 else (3 if b < 0xF0 else 4))
            for k in range(width):
                byte_to_char[i + k] = char_idx
            char_idx += 1
            i += width
        byte_to_char[len(encoded)] = char_idx

        # 各行の先頭文字インデックスを計算 O(N)
        line_start_chars: list[int] = []
        acc = 0
        for line in lines:
            line_start_chars.append(acc)
            acc += len(line) + 1  # +1 for \n

        for start_byte, end_byte, fmt_key in spans:
            fmt = _FORMATS.get(fmt_key)
            if fmt is None:
                continue
            start_char = byte_to_char[start_byte]
            end_char   = byte_to_char[end_byte]

            # 行番号を二分探索で O(log N)
            start_line = bisect.bisect_right(line_start_chars, start_char) - 1
            end_line   = bisect.bisect_right(line_start_chars, end_char)   - 1

            for line_num in range(start_line, end_line + 1):
                line_text       = lines[line_num] if line_num < len(lines) else ""
                line_char_start = line_start_chars[line_num]

                seg_start = max(start_char, line_char_start) - line_char_start
                seg_end   = min(end_char, line_char_start + len(line_text)) - line_char_start
                if seg_end - seg_start <= 0:
                    continue

                self._cache.setdefault(line_num, []).append((seg_start, seg_end - seg_start, fmt))

    def _on_changed(self, change: TextChange) -> None:
        if self._tree is None:
            self._reparse_full(self._text_doc.text)
            return
        self._tree.edit(
            start_byte=change.start_byte,
            old_end_byte=change.old_end_byte,
            new_end_byte=change.new_end_byte,
            start_point=change.start_point,
            old_end_point=change.old_end_point,
            new_end_point=change.new_end_point,
        )
        self._reparse_incremental(self._text_doc.text, change)

    def highlightBlock(self, text: str) -> None:
        block_num = self.currentBlock().blockNumber()
        for start, length, fmt in self._cache.get(block_num, []):
            self.setFormat(start, length, fmt)
