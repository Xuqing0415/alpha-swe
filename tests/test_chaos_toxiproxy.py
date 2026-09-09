# -*- coding: utf-8 -*-
"""toxiproxy 真实网络故障注入（边界打磨第 4 步扩展）。

用真实 TCP 代理（toxiproxy-server）包裹本地 HTTP 上游，验证命令执行模块
（TerminalTool）在真实网络故障下的降级行为，而非仅靠 mock：
- 基线：curl 经代理正常拿到上游响应；
- 延迟注入（latency toxic）：curl 等待响应超过 TerminalTool 超时即被
  terminate->kill，返回 TRANSIENT 结构化错误；移除延迟后立即恢复；
- 断开注入（删除代理）：curl 快速失败返回明确错误，不挂起、不误报超时。

只在能找到 toxiproxy-server 时运行（chaos.yml 显式安装；本地缺失自动跳过，
保证 quality-gate 与本地离线收集不受影响）。

运行：python -X utf8 -m pytest tests/test_chaos_toxiproxy.py -q
"""
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.tools.base import ErrorCategory, ExecutionContext
from agent.tools.terminal import TerminalTool

pytestmark = pytest.mark.chaos

MARKER = "alpha-swe toxiproxy probe OK"


def _curl_path():
    """解析 curl 可执行路径：Windows 用 curl.exe 避开 PowerShell 别名。"""
    if os.name == "nt":
        return shutil.which("curl.exe") or shutil.which("curl")
    return shutil.which("curl")


class _ProbeHandler(BaseHTTPRequestHandler):
    """最小本地 HTTP 上游：GET 任意路径都返回固定标记。"""

    def do_GET(self):
        body = (MARKER + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(method, url, payload=None, timeout=5.0):
    """向 toxiproxy REST API 发请求；返回 (status, text)。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


class _Toxi:
    """toxiproxy REST API 的轻量封装（仅标准库，不依赖 toxiproxy-cli）。"""

    def __init__(self, api_port: int, upstream_port: int):
        self.api = "http://127.0.0.1:%d" % api_port
        self.upstream_port = upstream_port

    def wait_ready(self, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _request("GET", self.api + "/version", timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("toxiproxy-server 未在 %ss 内就绪" % timeout)

    def create_proxy(self, name: str) -> int:
        """创建指向本地上游的代理，返回代理监听端口。"""
        listen = _free_port()
        status, _ = _request(
            "POST", self.api + "/proxies",
            {"name": name, "listen": "127.0.0.1:%d" % listen,
             "upstream": "127.0.0.1:%d" % self.upstream_port,
             "enabled": True})
        assert status in (200, 201), "创建代理失败 status=%s" % status
        return listen

    def add_latency(self, name: str, latency_ms: int):
        status, _ = _request(
            "POST", self.api + "/proxies/" + name + "/toxics",
            {"type": "latency", "toxicity": 1.0,
             "attributes": {"latency": latency_ms}})
        assert status in (200, 201), "注入延迟失败 status=%s" % status

    def clear_toxics(self, name: str):
        _, text = _request("GET", self.api + "/proxies/" + name + "/toxics")
        for toxic in json.loads(text or "[]"):
            _request("DELETE",
                     self.api + "/proxies/%s/toxics/%s"
                     % (name, toxic.get("name")))

    def delete_proxy(self, name: str):
        try:
            _request("DELETE", self.api + "/proxies/" + name)
        except Exception:
            pass

    def delete_all(self):
        _, text = _request("GET", self.api + "/proxies")
        for name in json.loads(text or "{}"):
            self.delete_proxy(name)


@pytest.fixture(scope="module")
def toxi():
    """启动本地 HTTP 上游 + toxiproxy-server；二进制缺失则整体跳过。"""
    server_bin = (os.environ.get("TOXIPROXY_SERVER")
                  or shutil.which("toxiproxy-server"))
    if not server_bin:
        pytest.skip("未找到 toxiproxy-server（chaos.yml 会安装），跳过真实网络故障注入")
    if not _curl_path():
        pytest.skip("未找到 curl，跳过真实网络故障注入")

    upstream = ThreadingHTTPServer(
        ("127.0.0.1", _free_port()), _ProbeHandler)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    api_port = _free_port()
    # toxiproxy >= 2.9 已移除 --data-file：代理状态全内存管理，无需持久化文件
    proc = subprocess.Popen(
        [server_bin, "-host=127.0.0.1", "-port=%d" % api_port],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mgr = _Toxi(api_port, upstream.server_address[1])
    try:
        mgr.wait_ready()
        yield mgr
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        upstream.shutdown()
        upstream.server_close()


@pytest.fixture(autouse=True)
def _clean_proxies(toxi):
    yield
    toxi.delete_all()


async def _curl(listen_port: int, timeout: float, ws_tmp):
    tool = TerminalTool()
    ctx = ExecutionContext(workspace=str(ws_tmp))
    exe = _curl_path()
    if os.name == "nt":
        # PowerShell 需 & 调用运算符执行带引号路径（curl 非别名）
        cmd = '& "%s" -fsS -m 8 http://127.0.0.1:%d/ping' % (exe, listen_port)
    else:
        cmd = '"%s" -fsS -m 8 http://127.0.0.1:%d/ping' % (exe, listen_port)
    return await tool.execute({"command": cmd, "timeout": timeout}, ctx)


@pytest.mark.asyncio
async def test_curl_baseline_via_proxy_ok(ws_tmp, toxi):
    """基线：curl 经 toxiproxy 代理能正常拿到上游响应。"""
    listen = toxi.create_proxy("baseline")
    r = await _curl(listen, 15, ws_tmp)
    assert r.success, r.error
    assert MARKER in r.output


@pytest.mark.asyncio
async def test_latency_toxic_triggers_timeout_then_recovers(ws_tmp, toxi):
    """延迟注入触发 TerminalTool 超时熔断；移除后立即恢复。"""
    listen = toxi.create_proxy("latency")
    toxi.add_latency("latency", 4000)
    r = await _curl(listen, 1, ws_tmp)
    assert r.success is False
    assert r.metadata.get("timed_out") is True, "应走超时终止而非等待完成"
    assert r.error_category == ErrorCategory.TRANSIENT

    toxi.clear_toxics("latency")
    r2 = await _curl(listen, 15, ws_tmp)
    assert r2.success, r2.error
    assert MARKER in r2.output


@pytest.mark.asyncio
async def test_proxy_down_fails_fast_without_hang(ws_tmp, toxi):
    """删除代理（服务断开）：curl 快速失败并返回明确错误，不误报超时。"""
    listen = toxi.create_proxy("down")
    toxi.delete_proxy("down")
    r = await _curl(listen, 5, ws_tmp)
    assert r.success is False
    assert not r.metadata.get("timed_out"), "连接拒绝应快速失败而非超时"
    assert r.elapsed_ms < 3000, "断开场景应在超时前快速返回"
    assert r.error or r.output, "应有明确的失败信息"
