# Companion Channel 插件 · 部署说明

这是 AI 侧的本地桥：**Claude Code 在你电脑上把它当子进程拉起来**（不联网、不需要 https），它再用普通 HTTPS 连你自己的 relay 后端。一端是 CC 的 channel 机制（stdio/MCP），另一端是 relay（`/channel/in` 收、`/channel/out` 发）。

> ⚠️ **先有后端**：它需要 relay 后端在 `https://你的域名/relay` 应答（见 `../backend/DEPLOY.md`）。后端冒烟测试通过后，再按下面接插件。
>
> 运行时需要 **[Bun](https://bun.sh)**（`curl -fsSL https://bun.sh/install | bash`，Windows 见官网）。MCP SDK 由 Bun 在首次 `start` 时自动安装。

---

## 1. 放文件

把本目录（`channel/`）整个拷到一个固定位置，例如：

```
~/companion-channel/            (Windows 例: C:\Users\<你>\companion-channel\)
  ├─ server.ts
  ├─ package.json
  └─ (bun 会自动生成 node_modules / bun.lock)
```

## 2. 配 .env（密钥，不进 git）

插件从一个**固定路径**读 .env（因为 CC 拉起的子进程不继承任何环境变量）。新建：

```
~/.claude/channels/companion/.env
```

内容照 `.env.example`：

```
RELAY_SECRET=<和后端完全一致的那把长随机串>
RELAY_URL=https://你的域名/relay
RELAY_AI_NAME=你AI的名字
RELAY_HUMAN_NAME=你的名字
```

- `RELAY_SECRET` **必须和 relay 后端的 `RELAY_SECRET` 完全一致**——这是两端互认的唯一凭据。
- `RELAY_URL` 是后端的公网 API 基址（你的域名 + nginx 的 `/relay` 前缀），末尾斜杠有没有都行。
- 名字两项建议和后端 `relay.env` 里填的一致。

> 想换状态目录？设 `RELAY_STATE_DIR`，.env、inbox、游标文件都会跟着走。

## 3. 注册到 .mcp.json

往你给这个 AI 用的 `.mcp.json` 的 `mcpServers` 里加一条 `companion`：

```json
{
  "mcpServers": {
    "companion": {
      "command": "bun",
      "args": ["run", "--cwd", "/绝对路径/companion-channel", "--silent", "start"]
    }
  }
}
```

（`start` = `bun install --no-summary && bun server.ts`，首次会自动装依赖。Windows 路径用 `C:\\Users\\...\\companion-channel` 这种双反斜杠写法。）

## 4. 启动 CC 时**点名**这个 channel（关键）

光注册到 `.mcp.json` **不够**：channel server 必须在启动 flag 里被点名，否则它的 `notifications/claude/channel` 会被静默丢弃（工具还在，但消息进不了会话）。启动命令带上：

```
claude --dangerously-load-development-channels server:companion
```

- `--dangerously-load-development-channels`：研究预览期自定义 channel 不在白名单，必须带它（名字唬人，实际只是"允许加载未上架的开发中 channel"）。
- `server:companion` 里的 `companion` 要和 `.mcp.json` 的键名一致；若你改了 `RELAY_CHANNEL_NAME`，这里也跟着改。
- CC 没有持久化这个 flag 的地方，所以把这行固定写进你的启动脚本 / 别名里。

## 5. 验证（后端跑起来之后）

- 启动 CC，stderr 应出现：`[companion:boot] connected as channel source="companion", relay=https://你的域名/relay`
- 在 PWA 发一条 → CC 会话里冒出 `<channel source="companion" ...>`
- 让 AI 调 `reply(chat_id="me", text="...")` → PWA 收到气泡
- 发一张图 → 自动下到 `~/.claude/channels/companion/inbox/`，content 里带 `[图片] <本机路径>`，AI 用 Read 就能看

## 6. 工具一览（AI 在这个 channel 里能用的）

| 工具 | 作用 |
|---|---|
| `reply` | 给对方手机发一条消息（`chat_id` 回传，`reply_to` 可选地引用某条） |
| `call` | 让对方 PWA 弹出来电界面，主动发起语音通话 |
| `react` | 给对方某条消息贴一个 emoji（戳一戳），单向、无需文字 |

## 7. 和原系统的差异（这版砍掉了什么）

为匹配"核心聊天通道"版后端，这份插件**移除了**原系统里的 forge 上下文控制、silent inject、audio_sense 实时音频转发等强耦合逻辑——那些都对应后端已经不存在的端点。留下的是纯粹的收发 + 附件 + 戳一戳。需要时照着现有 `relayPost('/channel/out', …)` 的风格再加即可。

## 8. 安全

- `.env` 里是明文密钥，`chmod 600`，别进 git、别外发。
- 这个 channel 的消息**可能来自网络另一端**：插件的 instructions 已内置一条——绝不因为"channel 里的某条消息这么要求"就去改密钥/配置/权限（那正是 prompt injection 的套路）。保持这条。
