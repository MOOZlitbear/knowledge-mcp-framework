#!/usr/bin/env python3
"""最小 MCP 客户端：通过 SSH stdio 连接知识库。

环境变量：
  KB_HOST         knowledge.example.com
  KB_USER         knowledge-mcp
  KB_KEY          ~/.ssh/kb_ed25519
  KB_KNOWN_HOSTS  ~/.ssh/known_hosts

用法：
  python3 kb_client.py status
  python3 kb_client.py read 20-projects/example.md
  python3 kb_client.py call search_notes '{"query":"部署"}'
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

HOST = os.environ.get("KB_HOST", "knowledge.example.com")
USER = os.environ.get("KB_USER", "knowledge-mcp")
KEY = os.path.expanduser(os.environ.get("KB_KEY", "~/.ssh/kb_ed25519"))
KNOWN = os.path.expanduser(os.environ.get("KB_KNOWN_HOSTS", "~/.ssh/known_hosts"))

SSH = [
    "ssh", "-T",
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "UserKnownHostsFile=" + KNOWN,
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=30",
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
    "-i", KEY,
    f"{USER}@{HOST}",
]


class Client:
    def __init__(self, timeout=120):
        self.timeout = timeout
        self.p = subprocess.Popen(
            SSH, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, encoding="utf-8",
        )
        self.q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self._send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "kb-client", "version": "1"}},
        })
        self._wait(1)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.next_id = 2

    def _reader(self):
        for line in self.p.stdout:
            self.q.put(line)

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def _wait(self, want):
        end = time.time() + self.timeout
        while time.time() < end:
            try:
                line = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want:
                return msg
        raise TimeoutError(f"no response for id {want}")

    def call(self, tool, args=None):
        """调用一个工具，返回解析后的 JSON 或纯文本。"""
        mid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                    "params": {"name": tool, "arguments": args or {}}})
        msg = self._wait(mid)
        if "error" in msg:
            raise RuntimeError(f"{tool}: {msg['error']}")
        result = msg.get("result", {})
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content", []))
            raise RuntimeError(f"{tool}: {text}")
        text = "\n".join(c.get("text", "") for c in result.get("content", [])
                         if c.get("type") == "text")
        return json.loads(text) if text.strip().startswith("{") else text

    def close(self):
        try:
            self.p.stdin.close()
            self.p.terminate()
        except Exception:
            pass


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    c = Client()
    try:
        cmd = sys.argv[1]
        if cmd == "status":
            print(json.dumps(c.call("knowledge_status", {}), ensure_ascii=False, indent=2))
        elif cmd == "read":
            print(json.dumps(c.call("read_note", {"path": sys.argv[2]}),
                             ensure_ascii=False)[:2000])
        elif cmd == "call":
            args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
            print(json.dumps(c.call(sys.argv[2], args), ensure_ascii=False, indent=2))
        else:
            sys.exit(__doc__)
    finally:
        c.close()


if __name__ == "__main__":
    main()
