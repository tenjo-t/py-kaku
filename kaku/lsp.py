from __future__ import annotations

import json
import subprocess
import threading

from PySide6.QtCore import QObject, Signal


class LspClient(QObject):
    """JSON-RPC over stdio LSP クライアント"""

    diagnostics_received = Signal(str, list)  # uri, diagnostics

    def __init__(self) -> None:
        super().__init__()
        self._process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._init_id: int | None = None

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
                    "publishDiagnostics": {}
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
                if length == 0:
                    continue

                body = proc.stdout.read(length)
                self._dispatch(json.loads(body))
            except Exception:
                break

    def _dispatch(self, msg: dict) -> None:
        # initialize のレスポンス → initialized を返す
        if msg.get("id") == self._init_id and "result" in msg:
            self._notify("initialized", {})
            return

        method = msg.get("method", "")
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params", {})
            self.diagnostics_received.emit(
                params.get("uri", ""),
                params.get("diagnostics", []),
            )
