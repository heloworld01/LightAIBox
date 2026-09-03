# LightAIBox · 轻量化 AI 工具箱

中文 | [English](README.md)

基于 **PySide6** 的桌面端轻量级 AI 网关工具：把多个大模型 API 提供方（OpenAI 兼容 / Anthropic）统一到一个本地入口，提供调度、配额、调用记录等能力，在本地集中管理并复用多套大模型密钥。

![Python](https://img.shields.io/badge/Python-3.8+-blue) ![GUI](https://img.shields.io/badge/GUI-PySide6-green)

![LightAIBox 主界面](homepage.zh-CN.png)

## 特性

- **统一 API 代理**：`chat` / `chat_stream` 一个接口屏蔽 OpenAI 与 Anthropic 协议差异，支持指定模型与按策略自适应、一次性与流式输出。
- **Claude Code 直连**：Anthropic 兼容端点完整透传 `tools` 与多轮 `tool_result`，流式 tool_use 遵循官方协议，可直接承载多步 agent 循环。
- **内置对话**：应用内即含微信式**对话**页，流式回复、Markdown + 离线 MathJax 渲染 LaTeX 公式与 mermaid 图表、每条回复可「原文 / 渲染」切换、带时间分隔条（详见[内置对话](#内置对话)）。
- **多 Provider 管理**：可视化增删改查，后台测连不卡界面、无弹窗。
- **智能调度**：按策略（长输入优先 / 短输入优先）挑选，单 Provider 失败自动降级重试。
- **配额控制**：按调用次数或 token 数设限，超配额自动停用、可一键重置。
- **调用记录与统计**：SQLite 持久化，支持按日期 / Provider 过滤并汇总调用次数、成功率与 token 用量。
- **悬浮 Provider 面板**：独立、总在最前的半透明面板，悬浮于屏幕底部，顶部「用量统计」标题条逐行列出全部「运行中」的 provider 及其剩余配额（模型、用量 / 余量靠右展示）；主窗口最小化到托盘时依然可见。可拖到任意位置；标题条右上角「锁定 / 已锁定」按钮锁定防误拖（锁定态变绿）、「关闭」按钮收起面板。每次启动默认隐藏；开关位于标签栏右上角（仅当次会话生效），文案随中英文切换。
- **后台常驻**：关闭窗口即最小化到系统托盘，统一 API 服务继续在后台运行；单击托盘图标显示 / 隐藏窗口，右键菜单可显示或彻底退出。

## 推荐使用自动模式的场景

`auto` 模型（自适应调度）尤其推荐在以下场景使用：

1. **大模型 API 存在限额**：某个 Provider 超出 token 限额后自动切换到下一个可用 Provider，避免 AI 编程中途中断。
2. **多套免费 / 受限账号轮换**：按优先级 / 限额 / 输入长度自动挑选模型，把每一份免费 token 都榨干用尽，赛博乞丐友好。🫙

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python -m app.main
```

首次运行自动在用户数据目录创建数据库（Windows 为 `%APPDATA%/LightAIBox/lightbox.db`，Linux 为 `~/.local/share/LightAIBox/`，macOS 为 `~/Library/Application Support/LightAIBox/`）。

### 添加 Provider

点击 Provider 列表「新增」，填写名称（唯一）、协议类型、Base URL（末尾 `/v1` 可省略）、API Key、模型。随后可在列表中编辑 / 复制 / 删除 / 启用停用 / 重置配额 / 测连。

## 统一 API 服务（HTTP）

本地 HTTP 服务**随应用启动自动拉起**，开箱即用（默认 `127.0.0.1:8765`）；也可在「统一 API」页手动停止 / 启动。

- OpenAI 兼容：`POST /v1/chat/completions`、`GET /v1/models`
- Anthropic 兼容：`POST /v1/messages`

```bash
curl -s http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'
```

### 接入 Claude Code

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8765 \
ANTHROPIC_API_KEY=anything \
ANTHROPIC_MODEL=<模型名> \
claude
```

## 内置对话

「对话」页是应用内的完整聊天窗口，视觉对齐微信、底层复用同一网关，可使用任意已配置
的 Provider（或 `auto` 调度）：

- **微信式消息流**：圆角矩形头像（「AI」/「我」，沿用应用靛蓝主色）+ 带方向尾巴的气泡
  + 非对称圆角；居中时间分隔条（首条消息显示，且任意相邻两条间隔 > 5 分钟时也显示）。
- **实时流式**：回复边生成边刷进气泡；「停止」按钮可随时取消本次生成。**智能滚动**：
  流式过程中仅当你已「贴底」才自动跟帖；若上翻阅读历史则保持原位，不被每个 token 拽回底部。
- **Markdown + LaTeX**：回复按 Markdown 渲染，公式由**本地打包的 MathJax v3（完全离线）**
  排版，支持 `\(...\)` / `\[...\]`（也支持 `$...$` / `$$...$$`）。围栏语言声明为
  `latex` / `tex` / `math` 的代码块会渲染成居中式公式（自动剥离 `%` 注释）；其余代码
  围栏（如 `python`）仍显示为带样式的代码块。
- **Mermaid 图**：`` ```mermaid `` 围栏由**本地打包的 mermaid v10（完全离线）**渲染成
  SVG，配色跟随当前暗 / 亮主题。
- **原文 / 渲染切换**：每条助手回复下方有链接，可在原始 markdown 与渲染视图间切换。
- **主题自适应**：气泡 / 头像 / 时间颜色随暗 / 亮主题切换；对话画布透明化，换肤即时生效
  无「滞后一帧」，并配 6px 细滚动条。

## 目录结构

```
app/
├── main.py          # 程序入口（初始化 DB + 拉起统一 API + 启动窗口）
├── config.py        # 配置与常量
├── models.py        # 领域数据模型
├── client.py        # 统一大模型客户端
├── gateway.py       # 网关：调度 + 配额 + 调用记录
├── server.py        # 统一 API 服务（FastAPI + uvicorn）
├── db.py            # SQLite 持久化
├── chat_session.py  # 对话会话：多轮消息历史 + 展示用时间戳
└── ui/              # PySide6 界面
    ├── chat_page.py # 「对话」页：微信式气泡 + MathJax 公式渲染
    ├── providers_bar.py # 悬浮置顶面板：标题条 + 运行中 Provider + 用量/余量（模型/用量右对齐，锁定/关闭按钮）
    └── resources/chat/  # 对话容器 HTML + 本地打包 MathJax v3 + mermaid（离线）
```

## 许可证

本项目采用 [MIT 许可证](LICENSE)，可自由使用、修改与分发，含商业用途。

## 特别鸣谢

感谢**老婆大人**提供的阿里云账号，让我能多蹭一份免费的大模型 token。💖
