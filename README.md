# Kaku

Python向けの軽量テキストエディター。IDLEより少し高機能で、日本語環境での使用を想定しています。

## 機能

- **シンタックスハイライト** — tree-sitter による正確なPython解析、Catppuccin Latteテーマ
- **LSP連携** — Ruff（リント）と Ty（型チェック）による診断・補完・ホバー・シグネチャヘルプ
- **コード補完** — auto-import 対応
- **行番号・インデントガイド**
- **日本語対応フォント** — CJK等幅フォントの自動選択

## インストール

```
pip install kaku
```

Ruff と Ty がコマンドとして利用できる必要があります：

```
pip install ruff ty
```

## 起動

```
kaku [ファイルパス]
```
