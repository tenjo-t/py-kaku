from __future__ import annotations

import json
import subprocess
import threading

from PySide6.QtCore import QObject, Signal


class LspClient(QObject):
    """JSON-RPC over stdio LSP クライアント"""

    diagnostics_received = Signal(str, list)  # uri, diagnostics
    completion_received = Signal(list)         # list[CompletionItem]
    hover_received = Signal(dict)              # Hover result
    resolve_received = Signal(dict)            # resolved CompletionItem
    signature_help_received = Signal(dict)     # SignatureHelp result

    def __init__(self) -> None:
        super().__init__()
        self._process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._init_id: int | None = None
        self._pending_completions: set[int] = set()
        self._pending_hovers: set[int] = set()
        self._pending_resolves: set[int] = set()
        self._pending_sig_helps: set[int] = set()
        self._last_hover_id: int | None = None
        self._last_sig_help_id: int | None = None

    def start(self, command: list[str]) -> bool:
        """LSPサーバープロセスを起動する。コマンドが見つからない場合はFalseを返す。"""
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        threading.Thread(target=self._read_loop, daemon=True).start()
        return True

    def stop(self) -> None:
        if self._process:
            try:
                self._notify("exit", {})
                self._process.terminate()
            except OSError:
                pass
            self._process = None

    # ── 送信 ────────────────────────────────────────────────

    def _send(self, msg: dict) -> None:
        if not self._process or not self._process.stdin:
            return
        body = json.dumps(msg).encode()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        try:
            self._process.stdin.write(header + body)
            self._process.stdin.flush()
        except OSError:
            pass

    def _request(self, method: str, params: dict) -> int:
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return req_id

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ── LSP プロトコル ────────────────────────────────────────

    def initialize(self, root_uri: str | None = None) -> None:
        self._init_id = self._request("initialize", {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {},
                    "signatureHelp": {
                        "signatureInformation": {
                            "parameterInformation": {"labelOffsetSupport": True}
                        }
                    },
                }
            },
        })

    def did_open(self, uri: str, text: str) -> None:
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "python",
                "version": 1,
                "text": text,
            }
        })

    def did_change(self, uri: str, text: str, version: int) -> None:
        self._notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"text": text}],
        })

    def did_close(self, uri: str) -> None:
        self._notify("textDocument/didClose", {
            "textDocument": {"uri": uri}
        })

    def completion(self, uri: str, line: int, character: int) -> None:
        req_id = self._request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })
        self._pending_completions.add(req_id)

    def resolve(self, item: dict) -> None:
        req_id = self._request("completionItem/resolve", item)
        self._pending_resolves.add(req_id)

    def signature_help(self, uri: str, line: int, character: int) -> None:
        req_id = self._request("textDocument/signatureHelp", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })
        self._pending_sig_helps.add(req_id)
        self._last_sig_help_id = req_id

    def hover(self, uri: str, line: int, character: int) -> None:
        req_id = self._request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })
        self._pending_hovers.add(req_id)
        self._last_hover_id = req_id

    # ── 受信（バックグラウンドスレッド） ────────────────────────

    def _read_loop(self) -> None:
        proc = self._process
        if not proc or not proc.stdout:
            return
        while True:
            try:
                headers: dict[str, str] = {}
                while True:
                    raw = proc.stdout.readline()
                    if not raw or raw == b"\r\n":
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if ":" in line:
                        k, _, v = line.partition(":")
                        headers[k.strip()] = v.strip()

                length = int(headers.get("Content-Length", 0))
                if length <= 0 or length > 10 * 1024 * 1024:  # 10 MB上限
                    continue

                body = proc.stdout.read(length)
                self._dispatch(json.loads(body))
            except Exception:
                break

    def _dispatch(self, msg: dict) -> None:
        msg_id = msg.get("id")

        # initialize のレスポンス → initialized を返す
        if msg_id == self._init_id and "result" in msg:
            self._notify("initialized", {})
            return

        # completion レスポンス
        if msg_id in self._pending_completions and "result" in msg:
            self._pending_completions.discard(msg_id)
            result = msg["result"] or []
            if isinstance(result, dict):
                result = result.get("items", [])
            self.completion_received.emit(result)
            return

        # resolve レスポンス
        if msg_id in self._pending_resolves and "result" in msg:
            self._pending_resolves.discard(msg_id)
            if msg["result"]:
                self.resolve_received.emit(msg["result"])
            return

        # signature help レスポンス（最新リクエスト以外は破棄）
        if msg_id in self._pending_sig_helps and "result" in msg:
            self._pending_sig_helps.discard(msg_id)
            if msg_id == self._last_sig_help_id:
                self.signature_help_received.emit(msg["result"] or {})
            return

        # hover レスポンス（最新リクエスト以外は破棄）
        if msg_id in self._pending_hovers and "result" in msg:
            self._pending_hovers.discard(msg_id)
            if msg_id == self._last_hover_id and msg["result"]:
                self.hover_received.emit(msg["result"])
            return

        method = msg.get("method", "")
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params", {})
            self.diagnostics_received.emit(
                params.get("uri", ""),
                params.get("diagnostics", []),
            )
