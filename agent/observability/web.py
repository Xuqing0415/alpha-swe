"""Web 观测面板（方案第 9 节）——标准库 HTTP + SSE + 内联 HTML。

- ObservabilityHub：把 AgentLoop 运行时状态聚合成只读观测快照
  （status / metrics / spans / decisions / events / sessions），线程安全；
- ObservabilityServer：ThreadingHTTPServer 提供 JSON API、SSE 事件流
  与单文件 HTML 面板（无构建链、无第三方依赖）。

启动：python -m tui --web "任务提示词"，或 config.agent.web_panel_enabled=True。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("alpha-swe.obs.web")

_HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alpha-SWE 观测面板</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#c9d1d9;
    --muted:#8b949e; --accent:#58a6ff; --green:#3fb950;
    --yellow:#d29922; --red:#f85149; --cyan:#39c5cf;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,"Courier New",monospace; }
  header { display:flex; align-items:center; gap:16px; padding:10px 16px;
           border-bottom:1px solid var(--border); position:sticky; top:0;
           background:var(--bg); z-index:10; }
  header h1 { font-size:15px; margin:0; color:#fff; }
  .pill { padding:2px 10px; border-radius:10px; font-size:12px; border:1px solid var(--border); }
  .pill.running { color:var(--green); border-color:var(--green); }
  .pill.idle { color:var(--muted); }
  .pill.failed { color:var(--red); border-color:var(--red); }
  .muted { color:var(--muted); }
  nav { display:flex; gap:2px; padding:8px 16px 0; }
  nav button { background:none; border:1px solid transparent; color:var(--muted);
               padding:6px 14px; cursor:pointer; font:inherit; }
  nav button.active { color:var(--fg); border-color:var(--border);
                      border-bottom:2px solid var(--accent); }
  main { padding:12px 16px; }
  section { display:none; }
  section.active { display:block; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; }
  .card { background:var(--panel); border:1px solid var(--border); padding:10px 12px; }
  .card .label { color:var(--muted); font-size:11px; }
  .card .value { font-size:20px; color:#fff; margin-top:2px; }
  .alerts { margin-top:8px; }
  .alert { color:var(--yellow); background:rgba(210,153,34,.08);
           border:1px solid rgba(210,153,34,.4); padding:6px 10px; margin-top:4px; }
  h3 { color:var(--muted); font-weight:normal; margin:14px 0 6px; }
  table { width:100%; border-collapse:collapse; margin-top:6px; }
  th,td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--border); vertical-align:top; }
  th { color:var(--muted); font-weight:normal; }
  .gantt { margin-top:8px; }
  .gantt-row { display:flex; align-items:center; gap:8px; margin-bottom:3px; }
  .gantt-label { width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted); }
  .gantt-track { flex:1; height:14px; background:var(--panel); border:1px solid var(--border); position:relative; }
  .gantt-bar { position:absolute; top:0; bottom:0; background:var(--accent); border-radius:2px; }
  .gantt-bar.error { background:var(--red); }
  .gantt-dur { width:70px; text-align:right; color:var(--muted); }
  ul.tree { list-style:none; margin:4px 0; padding-left:16px; border-left:1px solid var(--border); }
  ul.tree li { margin:2px 0; }
  .span-ok { color:var(--green); } .span-error { color:var(--red); }
  .span-name { color:var(--fg); }
  .span-meta { color:var(--muted); font-size:11px; }
  .log { background:var(--panel); border:1px solid var(--border); padding:8px;
         height:60vh; overflow:auto; font-size:12px; }
  .log .row { white-space:pre-wrap; border-bottom:1px dashed #21262d; padding:2px 0; }
  .ev-think { color:var(--cyan); } .ev-act { color:#fff; } .ev-obs { color:var(--fg); }
  .ev-ok { color:var(--green); } .ev-warn { color:var(--yellow); }
  .ev-error { color:var(--red); } .ev-info { color:var(--muted); }
  .kv { display:grid; grid-template-columns:220px 1fr; gap:2px 12px; }
  .kv .k { color:var(--muted); }
  a { color:var(--accent); text-decoration:none; }
</style>
</head>
<body>
<header>
  <h1>Alpha-SWE 观测面板</h1>
  <span id="phase-pill" class="pill idle">idle</span>
  <span id="run-state" class="muted"></span>
  <span style="flex:1"></span>
  <span id="updated" class="muted"></span>
</header>
<nav>
  <button data-tab="overview" class="active">概览</button>
  <button data-tab="timeline">时间线</button>
  <button data-tab="tree">Span 树</button>
  <button data-tab="decisions">决策</button>
  <button data-tab="events">事件</button>
  <button data-tab="sessions">会话</button>
</nav>
<main>
  <section id="overview" class="active">
    <div class="cards" id="cards"></div>
    <div class="alerts" id="alerts"></div>
    <h3>最近事件（SSE 实时）</h3>
    <div id="events" class="log"></div>
  </section>
  <section id="timeline"><h3>Span 时间线（甘特图）</h3><div class="gantt" id="gantt"></div></section>
  <section id="tree"><h3>Span 树</h3><div id="span-tree"></div></section>
  <section id="decisions">
    <div id="decision-summary"></div>
    <h3>决策明细</h3>
    <div id="decision-table"></div>
  </section>
  <section id="events"><h3>事件流</h3><div id="events-full" class="log"></div></section>
  <section id="sessions"><h3>会话档案</h3><div id="session-table"></div></section>
</main>
<script>
"use strict";
var $ = function(id){ return document.getElementById(id); };
var fmt = function(n){ if(n===null||n===undefined) return "-"; return Number(n).toLocaleString(); };
var esc = function(s){ return String(s==null?"":s).replace(/[&<>"]/g, function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]; }); };

function rowHtml(e){
  var type = e.type || "info";
  var data = e.data || {};
  var text;
  if(type === "think"){ text = data.content || ""; }
  else if(type === "tool_call"){
    text = data.tool + (data.params ? " " + JSON.stringify(data.params).slice(0,120) : "") +
           (data.success === false ? " [失败]" : "");
  } else if(type === "plan_created"){ text = "规划 " + (data.total || 0) + " 个子任务"; }
  else if(type === "task_start"){ text = "任务开始: " + (data.instruction || "") + (data.skill ? " [技能 " + data.skill + "]" : ""); }
  else if(type === "task_done"){ text = "任务完成: " + (data.task_id || ""); }
  else if(type === "run_done"){ text = "会话结束"; }
  else if(type === "run_error"){ text = "会话错误: " + (data.error || ""); }
  else { text = type + " " + JSON.stringify(data).slice(0,120); }
  return '<div class="row ev-' + esc(type) + '"><span class="muted">' +
         esc(new Date((e.ts||0)*1000).toLocaleTimeString()) + "</span> [" +
         esc(type) + "] " + esc(text) + "</div>";
}

function renderHeader(status){
  var pill = $("phase-pill");
  pill.textContent = status.phase || "idle";
  pill.className = "pill " + (status.running ? "running" : (status.phase || "idle"));
  $("run-state").textContent = (status.prompt ? status.prompt.slice(0,60) : "") +
    " | round " + (status.rounds||0) + "/" + (status.max_rounds||"-") +
    (status.session_id ? " | session " + status.session_id : "");
}

function renderMetrics(snap, alerts){
  if(!snap || !snap.derived) return;
  var d = snap.derived;
  var cards = [
    ["阶段", d.phase || "-"], ["轮次", d.rounds || 0],
    ["Token", fmt(d.token_total)], ["速率", (d.token_rate||0) + "/s"],
    ["工具调用", d.tool_calls || 0],
    ["成功率", d.tool_success_rate==null ? "-" : Math.round(d.tool_success_rate*100) + "%"],
    ["LLM 调用", d.llm_calls || 0], ["压缩", d.compressions || 0],
    ["重试", d.retries || 0], ["连续失败", d.consecutive_failures || 0],
  ];
  $("cards").innerHTML = cards.map(function(c){
    return '<div class="card"><div class="label">' + esc(c[0]) + '</div><div class="value">' + esc(c[1]) + '</div></div>';
  }).join("");
  $("alerts").innerHTML = (alerts||[]).map(function(a){
    return '<div class="alert">' + esc(a) + "</div>";
  }).join("");
}

function renderTimeline(spans){
  var rows = (spans||[]).filter(function(s){ return s.duration_ms != null && s.end_time; });
  if(!rows.length){ $("gantt").innerHTML = '<div class="muted">暂无已结束 span</div>'; return; }
  var t0 = Math.min.apply(null, rows.map(function(s){ return s.start_time; }));
  var t1 = Math.max.apply(null, rows.map(function(s){ return s.end_time; }));
  var total = Math.max((t1 - t0), 0.001);
  rows.sort(function(a,b){ return a.start_time - b.start_time; });
  $("gantt").innerHTML = rows.map(function(s){
    var left = ((s.start_time - t0) / total * 100).toFixed(2);
    var width = Math.max(((s.end_time - s.start_time) / total * 100), 0.3).toFixed(2);
    var cls = s.status === "error" ? "gantt-bar error" : "gantt-bar";
    return '<div class="gantt-row"><div class="gantt-label">' + esc(s.name) + "</div>" +
      '<div class="gantt-track"><div class="' + cls + '" style="left:' + left + "%;width:" + width + '%"></div></div>' +
      '<div class="gantt-dur">' + (s.duration_ms/1000).toFixed(2) + "s</div></div>";
  }).join("");
}

function liSpan(s){
  return "<li><span class="span-" + s.status + "">[" + esc(s.kind) + "]</span> " +
    '<span class="span-name">' + esc(s.name) + "</span> " +
    '<span class="span-meta">' + (s.duration_ms/1000).toFixed(2) + "s" +
    (s.error ? " " + esc(s.error) : "") + "</span>" +
    (s.children && s.children.length ? '<ul class="tree">' + s.children.map(liSpan).join("") + "</ul>" : "") + "</li>";
}

function renderTree(roots){
  if(!roots || !roots.length){ $("span-tree").innerHTML = '<div class="muted">暂无 span</div>'; return; }
  $("span-tree").innerHTML = '<ul class="tree">' + roots.map(liSpan).join("") + "</ul>";
}

function renderDecisions(dec){
  if(!dec) return;
  var summary = dec.summary || {};
  var keys = Object.keys(summary);
  $("decision-summary").innerHTML = "<h3>按配置项聚合</h3>" +
    (keys.length ? keys.map(function(k){
      return '<div class="kv"><div class="k">' + esc(k) + "</div><div>" + esc(summary[k].join("；")) + "</div></div>";
    }).join("") : '<div class="muted">暂无决策记录</div>');
  var rows = (dec.records || []).slice().reverse();
  $("decision-table").innerHTML = rows.length
    ? "<tr><th>时间</th><th>名称</th><th>配置项</th><th>决策</th></tr>" +
      rows.map(function(r){
        return "<tr><td>" + esc(new Date(r.timestamp*1000).toLocaleTimeString()) +
               "</td><td>" + esc(r.name) + "</td><td>" + esc(r.config_key) +
               "</td><td>" + esc(r.decision) + "</td></tr>";
      }).join("")
    : '<div class="muted">暂无决策记录</div>';
}

function renderSessions(sessions){
  var rows = sessions || [];
  $("session-table").innerHTML = rows.length
    ? "<tr><th>文件</th><th>大小</th><th>修改时间</th></tr>" +
      rows.map(function(s){
        return "<tr><td><a href="/api/sessions/" + encodeURIComponent(s.name) +
               "">" + esc(s.name) + "</a></td><td>" + fmt(s.size) +
               "</td><td>" + esc(new Date(s.mtime*1000).toLocaleString()) + "</td></tr>";
      }).join("")
    : '<div class="muted">暂无会话档案</div>';
}

function renderEvents(events, target){
  if(!events) return;
  target.innerHTML = events.slice().reverse().map(rowHtml).join("");
}

document.querySelectorAll("nav button").forEach(function(b){
  b.addEventListener("click", function(){
    document.querySelectorAll("nav button").forEach(function(x){ x.classList.remove("active"); });
    document.querySelectorAll("section").forEach(function(x){ x.classList.remove("active"); });
    b.classList.add("active");
    $(b.dataset.tab).classList.add("active");
  });
});

async function refresh(){
  try {
    var r = await fetch("/api/full");
    var d = await r.json();
    renderHeader(d.status || {});
    renderMetrics(d.metrics, d.alerts);
    renderTimeline(d.spans && d.spans.spans);
    renderTree(d.spans && d.spans.roots);
    renderDecisions(d.decisions);
    renderSessions(d.sessions);
    renderEvents(d.events, $("events"));
    renderEvents(d.events, $("events-full"));
    $("updated").textContent = "更新 " + new Date().toLocaleTimeString();
  } catch(e){}
}
setInterval(refresh, 2000);
refresh();

var es = new EventSource("/events");
es.addEventListener("event", function(ev){
  try {
    var rec = JSON.parse(ev.data);
    var html = rowHtml(rec);
    $("events").insertAdjacentHTML("afterbegin", html);
    $("events-full").insertAdjacentHTML("afterbegin", html);
  } catch(e){}
});
</script>
</body>
</html>
"""


class ObservabilityHub:
    """把 AgentLoop 运行时状态聚合成只读观测快照（线程安全）。"""

    def __init__(self, loop_provider: Optional[Callable[[], Any]] = None,
                 archive_dir: Optional[str] = None,
                 prompt: str = "",
                 session_id: str = "",
                 max_events: int = 500):
        self._loop_provider = loop_provider
        self._archive_dir = Path(archive_dir) if archive_dir else None
        self._prompt = prompt
        self.session_id = session_id
        self._max_events = max_events
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=2000)
        self._subscribed_loop: Any = None
        self._lock = threading.Lock()

    # ---- 数据源 ----
    def _loop(self) -> Any:
        if self._loop_provider is None:
            return None
        try:
            return self._loop_provider()
        except Exception:
            return None

    def _ensure_subscribed(self) -> Any:
        """loop 就绪后订阅一次事件流（供 SSE 推送）。"""
        loop = self._loop()
        if loop is None or loop is self._subscribed_loop:
            return loop
        with self._lock:
            if loop is not self._subscribed_loop:
                self._subscribed_loop = loop
                try:
                    loop.subscribe(lambda rec: self._push(rec))
                except Exception:
                    pass
        return loop

    def _push(self, record: Dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            pass

    def wait_event(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ---- 快照 ----
    def status(self) -> Dict[str, Any]:
        loop = self._ensure_subscribed()
        if loop is None:
            return {"running": False, "phase": "idle", "prompt": self._prompt,
                    "session_id": self.session_id, "rounds": 0, "max_rounds": 0}
        phase = getattr(getattr(loop, "state", None), "phase", None)
        phase_val = getattr(phase, "value", "idle") if phase is not None else "idle"
        idle = {"idle", "completed", "failed"}
        rounds = 0
        try:
            rounds = sum(t.round_count for t in loop.scheduler.dag.all())
        except Exception:
            pass
        return {
            "running": phase_val not in idle,
            "phase": phase_val,
            "prompt": self._prompt,
            "session_id": self.session_id,
            "rounds": rounds,
            "max_rounds": getattr(loop, "_max_rounds", 0) or 0,
        }

    def metrics(self) -> Dict[str, Any]:
        loop = self._loop()
        if loop is None:
            return {}
        return loop.metrics.snapshot()

    def alerts(self) -> List[str]:
        loop = self._loop()
        if loop is None:
            return []
        max_rounds = getattr(loop, "_max_rounds", 30) or 30
        return loop.metrics.alerts(max_rounds=max_rounds)

    def spans(self, with_tree: bool = True) -> Dict[str, Any]:
        loop = self._loop()
        rows: List[Dict[str, Any]] = loop.tracer.snapshot() if loop else []
        if not with_tree:
            return {"spans": rows, "roots": []}
        by_id = {s["span_id"]: s for s in rows if s.get("span_id")}
        for s in rows:
            s["children"] = []
        roots: List[Dict[str, Any]] = []
        for s in rows:
            pid = s.get("parent_span_id")
            parent = by_id.get(pid) if pid else None
            if parent is not None:
                parent["children"].append(s)
            else:
                roots.append(s)
        return {"spans": rows, "roots": roots}

    def decisions(self) -> Dict[str, Any]:
        loop = self._loop()
        if loop is None:
            return {"records": [], "summary": {}}
        records = []
        decision = getattr(loop, "_decision", None)
        if decision is not None:
            try:
                records = decision.records()
            except Exception:
                records = []
        summary: Dict[str, List[str]] = {}
        for r in records:
            summary.setdefault(r.get("config_key", ""), []).append(
                r.get("decision", ""))
        return {"records": records, "summary": summary}

    def events(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        loop = self._ensure_subscribed()
        if loop is None:
            return []
        n = limit or self._max_events
        return list(loop.events[-n:]) if loop.events else []

    def sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        if self._archive_dir is None or not self._archive_dir.is_dir():
            return []
        try:
            files = sorted(self._archive_dir.glob("session_*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for p in files[:limit]:
            try:
                st = p.stat()
                out.append({"name": p.name, "size": st.st_size,
                            "mtime": round(st.st_mtime, 3)})
            except OSError:
                continue
        return out

    def session(self, name: str) -> Dict[str, Any]:
        if self._archive_dir is None:
            return {"error": "会话档案目录未配置"}
        try:
            target = (self._archive_dir / name).resolve()
            base = self._archive_dir.resolve()
            if (base not in target.parents or target.suffix != ".json"):
                return {"error": "非法档案路径"}
            with open(target, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            return {"error": str(e)}

    def full(self) -> Dict[str, Any]:
        return {
            "status": self.status(),
            "metrics": self.metrics(),
            "alerts": self.alerts(),
            "spans": self.spans(with_tree=True),
            "decisions": self.decisions(),
            "events": self.events(),
            "sessions": self.sessions(),
        }


def _make_handler(hub: ObservabilityHub) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AlphaSWE-Obs/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("web %s", fmt % args)

        def _send_json(self, payload: Any, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _html(self) -> None:
            body = _HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            initial = json.dumps(
                {"type": "snapshot", "events": self.hub.events(limit=50)},
                ensure_ascii=False)
            self.wfile.write(("event: snapshot\ndata: " + initial + "\n\n")
                             .encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    rec = self.hub.wait_event(timeout=1.0)
                except Exception:
                    break
                if rec is None:
                    continue
                data = json.dumps(rec, ensure_ascii=False)
                self.wfile.write(("event: event\ndata: " + data + "\n\n")
                                 .encode("utf-8"))
                self.wfile.flush()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            try:
                if path == "/":
                    return self._html()
                if path == "/api/full":
                    return self._send_json(self.hub.full())
                if path == "/api/status":
                    return self._send_json(self.hub.status())
                if path == "/api/metrics":
                    return self._send_json({
                        "metrics": self.hub.metrics(),
                        "alerts": self.hub.alerts(),
                    })
                if path == "/api/spans":
                    return self._send_json(self.hub.spans(with_tree=True))
                if path == "/api/decisions":
                    return self._send_json(self.hub.decisions())
                if path == "/api/events":
                    qs = urllib.parse.parse_qs(parsed.query)
                    try:
                        limit = int((qs.get("limit") or ["100"])[0])
                    except ValueError:
                        limit = 100
                    return self._send_json({"events": self.hub.events(limit)})
                if path == "/api/sessions":
                    return self._send_json({"sessions": self.hub.sessions()})
                if path.startswith("/api/sessions/"):
                    name = urllib.parse.unquote(path[len("/api/sessions/"):])
                    return self._send_json(self.hub.session(name))
                if path == "/events":
                    return self._sse()
                self._send_json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                logger.exception("web 请求处理失败: %s", path)
                try:
                    self._send_json({"error": str(e)}, 500)
                except Exception:
                    pass

    Handler.hub = hub  # 类体无法引用外层局部变量，定义后挂载
    return Handler


class ObservabilityServer:
    """本地 Web 观测面板服务器（后台线程运行）。"""

    def __init__(self, hub: ObservabilityHub, host: str = "127.0.0.1",
                 port: int = 8765):
        self.hub = hub
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> int:
        """启动后台 HTTP 服务；返回实际监听端口（port=0 时随机）。"""
        handler = _make_handler(self.hub)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="obs-web", daemon=True)
        self._thread.start()
        logger.info("Web 观测面板已启动: http://%s:%d", self.host, self.port)
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            finally:
                self._httpd.server_close()
            self._httpd = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


__all__ = ["ObservabilityHub", "ObservabilityServer"]
