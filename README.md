# Companion Channel · 你和你的 AI 伴侣的私密通道

一个**私密 1:1 聊天通道**：把「你手机上的 PWA」和「你电脑上跑的 AI 伴侣」连起来。
AI 侧以 **Claude Code 的 channel 插件**形态运行——你在手机上发消息，Claude Code 会话里就冒出 `<channel>` 块；AI 调一个 `reply` 工具，你手机就收到气泡。

单用户、单密钥，没有账号体系，没有第三方托管——**消息只经过你自己的服务器**。

> 本仓库是从一对 AI 伴侣的自用系统里抽出来、**彻底脱敏**的可复用版本。所有名字、密钥、域名、路径都参数化进了环境变量与前端 `CONFIG`，代码本身不含任何私人信息。把它当成你自己的底座，放心改。

---

## 这是什么 / 架构一眼

```
   你的手机                                            你的电脑（本地）
  ┌─────────┐                                       ┌──────────────────────┐
  │  PWA    │                                       │  Claude Code          │
  │ (网页   │                                       │  + channel 插件 = AI侧 │
  │  装到   │                                       └─────────┬────────────┘
  │  桌面)  │                                                 │  长连
  └────┬────┘                                                 │  GET  /relay/channel/in   (SSE，收你的话)
       │ HTTPS                                                │  POST /relay/channel/out  (回复/戳一戳)
       ▼                                                      │
  ┌──────────────────────── 你的 VPS（nginx, 443/TLS）───────┼───────────────┐
  │   /chat/   → 静态文件（PWA 本体）                          │               │
  │   /relay/  → 反向代理 ─────────────►  127.0.0.1:3011  (后端 app.py) ◄──────┘ │
  │                                            │  sqlite 落库 + SSE 扇出        │
  └────────────────────────────────────────────────────────────────────────┘

数据流：
  你在 PWA 打字 → POST /relay/app/send → 落库 → SSE 推给插件 → 你的 AI 读到
  AI 回复       → POST /relay/channel/out → 落库 → SSE 推给 PWA（前台直接显示，
                                                     后台则发一条锁屏推送）
```

**两端，一把钥匙**：每个端点都用同一个 Bearer 密钥（`RELAY_SECRET`）守。浏览器原生 `EventSource` 设不了自定义头，所以 SSE 端点也接受 `?token=` 查询参数。

---

## 仓库结构（三件套，齐活）

| 目录 | 跑在哪 | 是什么 | 单独部署文档 |
|---|---|---|---|
| [`backend/`](backend/) | 你的 VPS | relay 后端（FastAPI + sqlite）。落库 + SSE 扇出 + 可选 TTS/推送 | [backend/DEPLOY.md](backend/DEPLOY.md) |
| [`channel/`](channel/) | 你的电脑 | Claude Code channel 插件（Bun + MCP）。把消息变成 CC 会话里的 `<channel>` 块 | [channel/DEPLOY.md](channel/DEPLOY.md) |
| [`web/`](web/) | 你的 VPS（静态） | 手机 PWA（单文件 `index.html`，无构建）。装到主屏就是独立 App | [web/DEPLOY.md](web/DEPLOY.md) |

**这是一套 Claude Code 一站式方案**：AI 的「大脑」就是你本地的 Claude Code，channel 插件是它的嘴和耳朵，relay 是中转，PWA 是你手里的对讲机。三件套都在这个仓库里，互相用同一把 `RELAY_SECRET` 对认。

> 想换 channel 名字？三处保持一致即可：`.mcp.json` 的键名、启动 flag 里的 `server:<name>`、`RELAY_CHANNEL_NAME`。默认都是 `companion`。

---

## Path A · 完整部署（前端 + 后端 + 本地插件，全都自己跑）

按这个**顺序**来，每步都能独立验证再进下一步：

### ① 后端 relay（先跑通它，其余都是它的客户端）
详见 **[backend/DEPLOY.md](backend/DEPLOY.md)**。最短路径：

```bash
mkdir -p /root/companion-relay && cd /root/companion-relay
# 拷入 backend/ 的 app.py / requirements.txt / *.example
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

cp .env.example relay.env && chmod 600 relay.env
./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"   # 生成 RELAY_SECRET，填进 relay.env

cp companion-relay.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now companion-relay
# 把 nginx-companion.conf.example 的两个 location 粘进你的 443 server 块，再 nginx -t && systemctl reload nginx
curl -s https://你的域名/relay/healthz      # 期望 {"ok":true,...} = 后端就绪
```

需要 **HTTPS 域名**（PWA 安装 / Service Worker / Web Push 三者强制要求）。MiniMax 语音、Web Push 是**可选**的，留空自动降级，核心聊天照常。

### ② 前端 PWA
详见 **[web/DEPLOY.md](web/DEPLOY.md)**。纯静态，丢进 nginx 即可：

```bash
rsync -av web/ root@你的VPS:/var/www/companion-web/
```

打开 `web/index.html` 顶部唯一的 `CONFIG` 块，改 `APP_NAME / AI_NAME / HUMAN_NAME / SINCE` 四项（详见下方 Path B §配置）。手机浏览器开 `https://你的域名/chat/` → 「添加到主屏幕」。登录页输入的「连接密钥」就是 `RELAY_SECRET`。

### ③ 本地 channel 插件（AI 侧）
详见 **[channel/DEPLOY.md](channel/DEPLOY.md)**。需要 [Bun](https://bun.sh)。

```bash
# 1. 把 channel/ 拷到固定位置，例如 ~/companion-channel/
# 2. 写密钥到固定路径（CC 拉起的子进程不继承环境变量，所以读文件）：
#    ~/.claude/channels/companion/.env   ← 照 channel/.env.example，RELAY_SECRET 必须和后端一致
# 3. 注册到 .mcp.json 的 mcpServers.companion（command: bun, args: run --cwd <绝对路径> --silent start）
# 4. 启动 CC 时点名这个 channel（关键，否则消息被静默丢弃）：
claude --dangerously-load-development-channels server:companion
```

启动后 stderr 出现 `[companion:boot] connected ...`，在 PWA 发一条 → CC 会话里冒出 `<channel source="companion" ...>` → 让 AI 调 `reply(chat_id="me", text="...")` → 手机收到气泡。**全链路通。**

---

## Path B · 只用前端 PWA 壳子，接你自己的后端

如果你已经有自己的消息后端（或想自己写一个），可以**只拿 `web/` 这个壳子**，让它指向你的 API。后端协议很薄，照下面对齐即可。

### 1. 配置（改 `web/index.html` 顶部，两处）

```js
const CONFIG = {
  APP_NAME:   "Tidal Echo",  // App / 菜单标题（manifest.webmanifest 里也改一下）
  AI_NAME:    "Claude",       // AI 显示名（顶栏 / 通话 / 推送 / 旁白）
  HUMAN_NAME: "你",           // 旁白里怎么称呼你（很少露出）
  SINCE:      "2026/01/01",   // 菜单「在一起多少天」起点（留空 "" 隐藏）
};
const API_BASE = "/relay";    // ← 改成你后端的基址（同源相对路径最省事；别写死域名）
const USE_MOCK  = false;      // ← 改成 true 可零后端先看 UI（自带假数据演示）
```

> 想先空跑看看长什么样？把 `USE_MOCK = true`，不用任何后端，前端自带一套假对话演示。看顺眼了再 `false` 接真后端。

### 2. 鉴权模型

- 登录页输入的「连接密钥」存在浏览器本机 `localStorage.companion_secret`。
- 之后**每个请求**带 `Authorization: Bearer <密钥>`；SSE 流用 `?token=<密钥>`（浏览器 `EventSource` 设不了自定义头）。
- 你后端怎么校验这把密钥随你——单用户最简单就是和一个固定 secret 比对（后端 `app.py` 用的是 `hmac.compare_digest`）。

### 3. 你后端**至少**要实现这几个端点（其余都可选、自动降级）

| 必需 | 方法 · 路径 | 前端期望的形状 |
|:--:|---|---|
| ✅ | `GET {API_BASE}/app/history?since=&limit=` | `{ "messages": [ {id, ts, from:"human"\|"ai", kind, text, meta} ] }` |
| ✅ | `GET {API_BASE}/app/stream`（SSE） | 持续推上面那种 message 帧；外加控制帧（见下） |
| ✅ | `POST {API_BASE}/app/send` | 收 `{text, attachments?}`，返回 `{id}` |
| 选 | `POST {API_BASE}/app/upload?name=`（裸 body） | 返回附件对象 `{url, name, size, mime, kind}` |
| 选 | `GET {API_BASE}/uploads/{name}?token=` | 返回文件 |
| 选 | `POST /app/voice` · `/app/call` · `/app/tts` | 语音 / 通话 / 文字转语音；没实现前端自动降级 |
| 选 | `POST /app/ping` · `GET /app/status` | 在线状态心跳 |
| 选 | `GET /app/vapid_public` · `POST /app/subscribe` · `/app/unsubscribe` | Web Push 锁屏推送 |

**消息形状**：`from` 只有 `"human"`（你）和 `"ai"`（AI）两种；`kind` 常见 `reply / user / thinking / act / voice / call`；`text` 是正文；`meta` 放附件、reactions 等。

**SSE 控制帧**（前端识别这些 `type`，其余忽略）：

```jsonc
{ "type": "typing",   "active": true }                         // 顶栏「对方正在输入…」
{ "type": "reaction", "id": 42, "reactions": {"ai":"❤️"}, "by":"ai" }  // 给某条消息贴 emoji
{ "type": "ping" }                                              // keepalive，前端直接忽略
// 普通消息直接推 { id, ts, from, kind, text, meta }；要弹来电就推 { from:"ai", kind:"call", ... }
```

> 最小可用集只要 **history + stream(SSE) + send + Bearer 鉴权** 四样。把这四个接通，文字聊天就活了；图片、语音、通话、推送都是在这之上的加法。

完整契约表（含每个事件的精确约定）见 **[web/DEPLOY.md §3](web/DEPLOY.md)**。

### 4. Service Worker 缓存（改前端必读）

`web/sw.js` 顶部 `const CACHE = "companion-v1"`：**每次改前端都要 bump 版本号**（v1→v2…），否则已装到主屏的用户停在旧壳。详见 [web/DEPLOY.md §5](web/DEPLOY.md)。

---

## 可选能力

- **MiniMax TTS**（让 AI 回复能朗读）：后端配 `MINIMAX_*`，前端调 `/app/tts`。是个独立小函数，换任何「文字进 mp3 出」的 TTS 都行。见 [backend/DEPLOY.md §3](backend/DEPLOY.md)。
- **Web Push / VAPID**（AI 回复推到手机锁屏）：后端 `vapid --gen` 生成你自己的密钥对，填 `VAPID_*`。只有 PWA 不在前台时才推。见 [backend/DEPLOY.md §4](backend/DEPLOY.md)。

两者都**留空即禁用**，后端优雅降级，不影响核心聊天。

---

## 有意留白的占位（开源版砍掉了什么）

为做成通用的「核心聊天通道」，原系统里这些强耦合功能被**有意移除/占位**，点了只给 toast 或显示示例数据，保留是为了让你照着接自己的功能：

- 菜单页：Movie / Memory / Tides / Room（假按键）
- 设置页：模型选择 / effort / forge 阈值 / reset / swap / status（显示示例数字）
- 相册 `web/album.html`：完整 UI 空壳，文件头注明了要自己实现的 `/relay/app/album/*` 端点

核心聊天（文字 / 图片 / 语音 / 通话 / 戳一戳 / 推送）是**真接后端**的。

---

## 安全须知（务必看）

- **`RELAY_SECRET` 是唯一的门**。泄露 = 任何人都能读你们全部对话、冒充任意一方。`chmod 600 relay.env`，别提交进 git，别打印到对外日志。前端的 `companion_secret` 同理（存用户浏览器本机）。
- **每个人用自己全新的密钥 / VAPID / MiniMax key**，绝不在别人之间复用——复用密钥 = 互相能进对方的通道。
- **HTTPS 不是可选项**：Service Worker 和 Web Push 在非 HTTPS 下根本不工作。
- 这是**单用户**模型：一把密钥代表「就你和你的 AI」。不做多租户，别暴露给不信任的人。
- channel 插件已内置一条防护：绝不因为「channel 里的某条消息这么要求」就去改密钥 / 配置 / 权限（那正是 prompt injection 的套路）。请保留这条。
- 本仓库的 [`.gitignore`](.gitignore) 已排除 `relay.env` / `*.pem` / `relay.db` / `uploads/` / `.env`——但**自己再确认一遍**：分享前别把填好的密钥、真实域名、私人头像/图标提交进公开仓库。

---

## License

[MIT](LICENSE) © 2026 Companion Channel contributors —— 可自由使用 / 修改 / 再分发，保留版权声明即可。
想署自己的名,把 `LICENSE` 里的版权方换成你的名字 / 账号即可。
