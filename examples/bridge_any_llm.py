#!/usr/bin/env python3
"""
bridge_any_llm.py — 把「任意 LLM API」接到 companion relay 的 AI 侧 bridge。

这是 channel/ 插件(Claude Code 专用)的通用替代品:不依赖 Claude Code,
用任何 OpenAI 兼容的模型(GPT / DeepSeek / Gemini / GLM / Kimi / 通义 / 本地 vLLM …)
当「AI 大脑」。前端 PWA 和 relay 后端原样不动。

它是个「带工具的聊天」循环,不是会自己乱跑的自主 agent —— 只在收到人类
消息时动一次:

    ① SSE 长连  GET  {RELAY}/channel/in?since={cursor}   收人类消息(实时)
    ② 用内存维护的近期对话 + persona(system),调你的模型(OpenAI 格式)
    ③ POST       {RELAY}/channel/out  {"type":"reply","text":...}   回复回手机

首次启动会拉一次历史做「暖启动」上下文,并把游标设到当前最新一条 ——
所以不会回放/重答你过去的旧消息,只应答启动之后的新消息。重启则从上次游标
继续,补答断线期间漏掉的。

零第三方依赖(只用 Python 标准库,3.7+)。配置全走环境变量,可放在同目录
.env(见 .env.example)。跑起来:

    cp .env.example .env   &&   # 填好 RELAY_URL / RELAY_SECRET / LLM_* 三件
    python3 bridge_any_llm.py

⚠️ 单身体原则:同一时刻只跑一个 AI 侧。别同时开着 Claude Code channel 和
这个 bridge —— 两个都会收到同一条消息、都会回复,用户会看到双重回复。
"""

from __future__ import annotations  # 让类型注解不在运行时求值,兼容 Python 3.7+

import collections
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ———————————————————————————
# 配置(环境变量;也读同目录 .env)
# ———————————————————————————

def _load_dotenv(path: Path) -> None:
    """极简 .env 加载:KEY=VALUE 逐行;真实环境变量优先。"""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

_load_dotenv(Path(__file__).resolve().parent / ".env")

RELAY_URL    = os.environ.get("RELAY_URL", "").rstrip("/")       # 你的域名 + nginx /relay 前缀
SECRET       = os.environ.get("RELAY_SECRET", "")                # 必须和后端 relay.env 一致
CHAT_ID      = os.environ.get("RELAY_CHAT_ID", "me")             # 单用户通道,固定 "me"
HISTORY_N    = int(os.environ.get("HISTORY_N", "12"))             # 喂给模型的最近对话「轮」数
TEMPERATURE  = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
HTTP_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))

# Ombre Brain MCP 端点
OMBRE_MCP_URL   = os.environ.get("OMBRE_MCP_URL", "").rstrip("/")
OMBRE_MCP_TOKEN = os.environ.get("OMBRE_MCP_TOKEN", "")

# persona = 模型的人设(system prompt)。从 PERSONA 文本或 PERSONA_FILE 文件读。
PERSONA = os.environ.get("PERSONA", "").strip()
_persona_file = os.environ.get("PERSONA_FILE", "").strip()
if not PERSONA and _persona_file:
    try:
        PERSONA = Path(_persona_file).read_text(encoding="utf-8").strip()
    except OSError:
        pass
if not PERSONA:
    PERSONA = "你是对方的 AI 伴侣,在一个私密的一对一聊天里。说话自然、简短、有温度,像在用手机聊天,不要长篇大论。"
    print(">>> PERSONA:", repr(PERSONA[:100]))

# 模型链:主模型 + 可选兜底(LLM_*_2 / _3)。任一返回 FALLBACK_CODES 就顺次切下一个。
def _model_routes() -> list:
    routes = []
    for suffix in ("","_2", "_3"):
        base  = os.environ.get(f"LLM_API_BASE{suffix}","").rstrip("/")
        key   = os.environ.get(f"LLM_API_KEY{suffix}","")
        model = os.environ.get(f"LLM_MODEL{suffix}","")
        if base and model:
            routes.append({"base": base, "key": key, "model": model})
    return routes

MODEL_ROUTES   = _model_routes()
FALLBACK_CODES = {401, 403, 404, 408, 409, 429, 500, 502, 503, 504}

# 断线重连游标:只处理 id > cursor 的消息;重连带 ?since=cursor 让 relay 补发。
STATE_DIR   = Path(os.environ.get("BRIDGE_STATE_DIR", Path.home() / ".companion-bridge"))
CURSOR_FILE = STATE_DIR / "last_in_id"

# 内存里的滚动对话上下文
convo: "collections.deque[dict]" = collections.deque(maxlen=max(HISTORY_N * 2, 8))

def log(tag: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", file=sys.stderr, flush=True)

def _require_config() -> None:
    missing = []
    if not RELAY_URL:    missing.append("RELAY_URL")
    if not SECRET:       missing.append("RELAY_SECRET")
    if not MODEL_ROUTES: missing.append("LLM_API_BASE + LLM_API_KEY + LLM_MODEL")
    if missing:
        log("fatal", "缺少配置:" + ",".join(missing) + " —— 填 .env(见 .env.example)再跑")
        sys.exit(1)

# ———————————————————————————
# Ombre Brain MCP 客户端
# ———————————————————————————

_mcp_session_id: str = ""
_mcp_req_id: int = 0

def _mcp_post(payload: dict) -> dict:
    """向 Ombre Brain /mcp 发 JSON-RPC 请求，处理 JSON 或 SSE 响应。"""
    global _mcp_session_id, _mcp_req_id
    _mcp_req_id += 1
    payload["jsonrpc"] = "2.0"
    payload["id"] = _mcp_req_id

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if OMBRE_MCP_TOKEN:
        headers["Authorization"] = f"Bearer {OMBRE_MCP_TOKEN}"
    if _mcp_session_id:
        headers["Mcp-Session-Id"] = _mcp_session_id

    req = urllib.request.Request(OMBRE_MCP_URL, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        # 保存 session id
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            _mcp_session_id = sid

        content_type = r.headers.get("Content-Type", "")
        body = r.read().decode("utf-8")

        # 纯 JSON 响应
        if "application/json" in content_type:
            return json.loads(body)

        # SSE 响应：提取 data: 行里的 JSON
        if "text/event-stream" in content_type or body.startswith("event:") or body.startswith("data:"):
            for line in body.splitlines():
                if line.startswith("data:"):
                    json_str = line[5:].strip()
                    if json_str:
                        try:
                            return json.loads(json_str)
                        except json.JSONDecodeError:
                            continue
            # fallback: 尝试整体解析
            return json.loads(body)

        # 未知格式，尝试直接解析
        return json.loads(body)

def mcp_init() -> None:
    """初始化 MCP 会话（启动时调一次）。"""
    if not OMBRE_MCP_URL:
        return
    resp = _mcp_post({
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "companion-bridge", "version": "1.0"}
        }
    })
    log("mcp", f"session initialized: {_mcp_session_id[:8] if _mcp_session_id else 'no-sid'}…")
    # 发 initialized 通知(不需要 id)
    notify_data = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}
    }, ensure_ascii=False).encode("utf-8")
    notify_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if OMBRE_MCP_TOKEN:
        notify_headers["Authorization"] = f"Bearer {OMBRE_MCP_TOKEN}"
    if _mcp_session_id:
        notify_headers["Mcp-Session-Id"] = _mcp_session_id
    notify_req = urllib.request.Request(OMBRE_MCP_URL, data=notify_data, method="POST", headers=notify_headers)
    try:
        with urllib.request.urlopen(notify_req, timeout=10) as r:
            pass
    except Exception:
        pass  # 通知失败不影响

def mcp_call_tool(name: str, arguments: dict) -> str:
    """调 Ombre Brain 的一个工具，返回文本结果。"""
    try:
        resp = _mcp_post({
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments}
        })
        result = resp.get("result", {})
        # MCP 工具返回格式: {"content": [{"type": "text", "text": "…"}]}
        contents = result.get("content", [])
        texts = [c.get("text", "") for c in contents if c.get("type") == "text"]
        return "".join(texts) or json.dumps(result, ensure_ascii=False)
    except Exception as e:
        log("mcp", f"tool {name} failed: {e}")
        return f"[工具调用失败: {e}]"

# ———————————————————————————
# OpenAI 格式的工具定义（记忆系统）+ 缓存标记
# ———————————————————————————

MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "breath",
            "description": "检索或浮现记忆。无 query 时浮现未解决的近期重要记忆；有 query 时按语义+关键词搜索。对话开始时应先调一次（无参）获取上下文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，留空则浮现模式"},
                    "domain": {"type": "string", "description": "限定领域，如 feel"},
                    "max_results": {"type": "integer", "description": "最大返回数，默认 20"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hold",
            "description": "存储一条记忆。对话中出现值得长期记住的事实、情感、事件时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的内容"},
                    "importance": {"type": "integer", "description": "重要度 1-10，默认 5"},
                    "tags": {"type": "string", "description": "标签，逗号分隔"},
                    "feel": {"type": "boolean", "description": "是否为自省感受"},
                    "source_bucket": {"type": "string", "description": "feel 模式时的源桶 ID"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grow",
            "description": "日记式归档：把一段长内容拆分成多条独立记忆存储。适合一次性整理一段对话或一天的事。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要拆分归档的长文本"}
                },
                "required": ["content"]
            }
        },
        # 最后一个工具上打 cache_control，整个 tools 数组都能被缓存
        "cache_control": {"type": "ephemeral"}
    }
]

# ———————————————————————————
# relay I/O
# ———————————————————————————

def _auth() -> dict:
    return {"Authorization": f"Bearer {SECRET}"}

def relay_get_json(path: str):
    req = urllib.request.Request(RELAY_URL + path, headers=_auth())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def relay_post_json(path: str, body: dict):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        RELAY_URL + path, data=data, method="POST",
        headers={**_auth(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = r.read().decode("utf-8")
        return json.loads(txt) if txt else {}

def send_reply(text: str) -> None:
    """AI 的回复 → 落库 + 扇出到 PWA。"""
    out = relay_post_json("/channel/out", {
        "type":    "reply",
        "chat_id": CHAT_ID,
        "text":    text,
        "ts":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    log("out", f"replied (id={out.get('id')})")

# ———————————————————————————
# 历史 → 内存上下文
# ———————————————————————————

def _row_to_msg(m: dict):
    """把一条 relay 历史/消息转成 OpenAI message;不该进上下文的返回 None。"""
    text = (m.get("text") or "").strip()
    if not text or m.get("kind") == "call":  # 跳过通话开始/结束这类系统事件
        return None
    if m.get("from") == "human":
        return {"role": "user", "content": text}   # 含语音转写(🎤 …)
    if m.get("from") == "ai" and m.get("kind") == "reply":
        return {"role": "assistant", "content": text}
    # 跳过 thinking/act 等中间态
    return None

def load_history() -> tuple:
    """翻页拉全部历史 → (近期对话 messages, 最新一条的 id)。"""
    rows, since = [], 0
    while True:
        page = relay_get_json(f"/app/history?since={since}&limit=500").get("messages", [])
        if not page:
            break
        rows.extend(page)
        since = page[-1]["id"]
        if len(page) < 500:
            break
    max_id = rows[-1]["id"] if rows else 0
    msgs = [mm for m in rows if (mm := _row_to_msg(m))]
    return msgs[-convo.maxlen:], max_id

def build_messages() -> list:
    """构建带 Anthropic prompt cache 标记的消息列表。
    断点策略：
      BP1 = system prompt（人设 + 工具说明，每次不变）→ cache_control
      BP2 = 对话历史中较早的一个 assistant 消息 → cache_control
    这样 system + 早期对话都走缓存读取（写入价的 1/10），只有最近几条付全价。
    """
    # System prompt → content block 格式 + cache_control (BP1)
    system_msg = {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": PERSONA,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    }

    # 复制对话历史（不改动 convo 原始数据）
    msgs = []
    for m in convo:
        msgs.append({"role": m["role"], "content": m["content"]})

    # BP2: 在历史里找断点 — 跳过最近 4 条，往前找第一个 assistant 消息
    # 给它加 cache_control，这样断点之前的所有内容可被跨轮缓存复用
    if len(msgs) > 4:
        for i in range(len(msgs) - 5, -1, -1):
            if msgs[i]["role"] == "assistant":
                text = msgs[i]["content"]
                msgs[i] = {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": text,
                            "cache_control": {"type": "ephemeral"}
                        }
                    ]
                }
                break

    return [system_msg] + msgs

# ———————————————————————————
# 调模型(OpenAI chat/completions;带 tools 循环 + fallback 链)
# ———————————————————————————

MAX_TOOL_ROUNDS = 6  # 防止无限工具循环

def _one_call(route: dict, messages: list) -> str:
    """调 LLM，处理 tool_calls 循环，返回最终文本回复。"""
    tools_payload = MEMORY_TOOLS if OMBRE_MCP_URL else None

    for _round in range(MAX_TOOL_ROUNDS):
        body = {
            "model": route["model"],
            "messages": messages,
            "temperature": TEMPERATURE,
        }
        if tools_payload:
            body["tools"] = tools_payload
        # 告诉 OpenRouter 透传 Anthropic prompt cache 标记
        body["extra"] = {"anthropic": {"cache_control": True}}

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            route["base"] + "/chat/completions",
            data=data, method="POST",
            headers={"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))

        choice = resp["choices"][0]
        msg = choice["message"]

        # 没有 tool_calls → 直接返回文本
        log("llm", f"round={_round} tool_calls={bool(msg.get('tool_calls'))} content={bool(msg.get('content'))}")
        if not msg.get("tool_calls"):
            return (msg.get("content") or "").strip()

        # 有 tool_calls → 执行每个工具，把结果追加到 messages
        messages.append(msg)  # 把 assistant 的 tool_calls 消息加进去

        for tc in msg["tool_calls"]:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                fn_args = {}
            log("tool", f"calling {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:100]})")

            result = mcp_call_tool(fn_name, fn_args)
            log("tool", f"{fn_name} returned {len(result)} chars")

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result
            })

    # 超过轮数限制，强制要一个无工具回复
    log("tool", f"reached max {MAX_TOOL_ROUNDS} rounds, forcing final reply")
    body = {
        "model": route["model"],
        "messages": messages,
        "temperature": TEMPERATURE,
        "extra": {"anthropic": {"cache_control": True}}
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        route["base"] + "/chat/completions",
        data=data, method="POST",
        headers={"Authorization": f"Bearer {route['key']}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return (resp["choices"][0]["message"].get("content") or "").strip()

def call_llm(messages: list) -> str:
    last_err = None
    for route in MODEL_ROUTES:
        try:
            return _one_call(route, messages)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in FALLBACK_CODES:
                log("llm", f"{route['model']} HTTP {e.code} → 切下一个")
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            log("llm", f"{route['model']} 连接失败({e}) → 切下一个")
            continue
    raise RuntimeError(f"所有模型都失败,最后错误: {last_err}")

# ———————————————————————————
# 一条消息的处理
# ———————————————————————————

def handle_human_message(msg: dict) -> None:
    content = (msg.get("content") or "").strip()
    atts = msg.get("attachments") or []
    if atts:
        names = ",".join(a.get("name") or "file" for a in atts)
        content = (content + "\n" if content else "") + f"(对方发来 {len(atts)} 个附件: {names})"
    if not content:
        return
    log("in", f"#{msg.get('id')}: {content[:60]}")
    convo.append({"role": "user", "content": content})
    try:
        reply = call_llm(build_messages())
        log("debug", "reply=" + str(reply)[:100])
    except Exception as e:
        log("err", "生成失败: " + str(e))
        return
    if reply:
        convo.append({"role": "assistant", "content": reply})
        send_reply(reply)

# ———————————————————————————
# SSE 入站流:GET /channel/in(断线自动重连)
# ———————————————————————————

def read_cursor() -> int:
    try:
        return int(CURSOR_FILE.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0

def write_cursor(i: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CURSOR_FILE.write_text(str(i))
    except OSError:
        pass

def stream_inbound(cursor: int) -> None:
    backoff = 1
    while True:
        try:
            url = f"{RELAY_URL}/channel/in?since={cursor}"
            req = urllib.request.Request(url, headers={**_auth(), "Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                log("in", f"stream connected (since={cursor})")
                backoff = 1
                data_lines: list = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif line == "":  # 空行 = 一帧结束
                        if not data_lines:
                            continue
                        payload = "\n".join(data_lines)
                        data_lines = []
                        try:
                            m = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if m.get("type") == "ping" or "id" not in m:
                            continue
                        mid = int(m.get("id") or 0)
                        if mid <= cursor:  # 重连补发里已处理过的,跳过
                            continue
                        handle_human_message(m)
                        cursor = mid
                        write_cursor(cursor)  # 只在处理后推进游标
            log("in", "stream ended → reconnect")
        except Exception as e:
            log("in", f"disconnected ({e}) → retry in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 15)

def main() -> None:
    _require_config()
    log("boot", f"relay={RELAY_URL} models={[r['model'] for r in MODEL_ROUTES]} history={HISTORY_N}")

    # 初始化 Ombre Brain MCP 会话
    if OMBRE_MCP_URL:
        try:
            mcp_init()
            log("boot", f"ombre brain connected: {OMBRE_MCP_URL}")
        except Exception as e:
            log("boot", f"ombre brain init failed ({e}), running without memory")
    else:
        log("boot", "no OMBRE_MCP_URL set, running without memory")

    cursor = read_cursor()
    # 暖启动:拉历史填上下文,并把全新部署的游标设到「当前最新」
    try:
        ctx, max_id = load_history()
        convo.extend(ctx)
        if cursor == 0:
            cursor = max_id
            write_cursor(cursor)
        log("boot", f"warm-start: {len(convo)} msgs in context, cursor={cursor}")
    except Exception as e:
        log("boot", f"history warm-start skipped ({e})")
    stream_inbound(cursor)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
