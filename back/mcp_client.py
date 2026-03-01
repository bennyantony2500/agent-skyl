import os
import sys
import json
import asyncio
import threading
import queue
from typing import Optional


class MCPClient:
    def __init__(self, server_script: str, env: dict = None):
        self.server_script = server_script
        self.env = env or {}
        self._process = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._response_queue = queue.Queue()
        self._reader_thread = None
        self._running = False

    async def start(self):
        import subprocess
        full_env = {**os.environ, **self.env}

        self._process = subprocess.Popen(
            [sys.executable, self.server_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            bufsize=0,  
        )

        self._running = True

        self._reader_thread = threading.Thread(
            target=self._read_stdout, daemon=True
        )
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True
        )
        self._stderr_thread.start()

        await asyncio.sleep(0.5)

        if self._process.poll() is not None:
            raise RuntimeError(f"MCP server crashed immediately on start")

        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "monday-bi-agent", "version": "1.0.0"}
        })

        await self._send_notification("notifications/initialized")
        await asyncio.sleep(0.2)

    def _read_stdout(self):
        try:
            while self._running and self._process and self._process.poll() is None:
                line = self._process.stdout.readline()
                if not line:
                    break
                line = line.decode("utf-8").strip()
                if line:
                    try:
                        obj = json.loads(line)
                        self._response_queue.put(obj)
                    except json.JSONDecodeError:
                        pass  # skip non-JSON lines
        except Exception as e:
            print(f"[MCP reader] error: {e}", file=sys.stderr)

    def _read_stderr(self):
        try:
            while self._running and self._process and self._process.poll() is None:
                line = self._process.stderr.readline()
                if not line:
                    break
                print(f"[MCP server] {line.decode('utf-8').rstrip()}", file=sys.stderr, flush=True)
        except Exception:
            pass

    async def stop(self):
        self._running = False
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()
            self._process = None

    async def list_tools(self) -> list[dict]:
        try:
            result = await self._send_request("tools/list", {})
            return result.get("tools", [])
        except Exception as e:
            print(f"[MCP] list_tools failed: {e}", file=sys.stderr)
            return []

    async def call_tool(self, name: str, arguments: dict = None) -> dict:
        result = await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments or {}
        })
        content = result.get("content", [])
        text_parts = [c["text"] for c in content if c.get("type") == "text"]
        combined = "\n".join(text_parts)
        try:
            return json.loads(combined)
        except json.JSONDecodeError:
            return {"raw": combined}

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _write_request(self, obj: dict):
        line = json.dumps(obj) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        self._process.stdin.flush()

    async def _send_request(self, method: str, params: dict) -> dict:
        if not self._process:
            raise RuntimeError("MCP client not started")

        req_id = self._next_id()
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._write_request, request)

            deadline = asyncio.get_event_loop().time() + 30.0
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise RuntimeError(f"Timeout waiting for response to {method}")

                try:
                    response = self._response_queue.get_nowait()
                    if response.get("id") == req_id:
                        if "error" in response:
                            raise RuntimeError(f"MCP error: {response['error']}")
                        return response.get("result", {})
                    else:
                        self._response_queue.put(response)
                        await asyncio.sleep(0.05)
                except queue.Empty:
                    await asyncio.sleep(0.05)

    async def _send_notification(self, method: str, params: dict = None):
        notification = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_request, notification)




_mcp_client: Optional[MCPClient] = None


def get_mcp_client() -> Optional[MCPClient]:
    return _mcp_client


async def init_mcp_client(server_script: str, env: dict):
    global _mcp_client
    if _mcp_client:
        await _mcp_client.stop()
    client = MCPClient(server_script, env)
    await client.start()
    _mcp_client = client
    return client


async def shutdown_mcp_client():
    global _mcp_client
    if _mcp_client:
        await _mcp_client.stop()
        _mcp_client = None
