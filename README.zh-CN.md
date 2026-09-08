# LightAIBox · 轻量化 AI 工具箱

中文 | [English](README.md)

基于 **PySide6** 的桌面端轻量级 AI 网关工具：把多个大模型 API 提供方（OpenAI 兼容 / Anthropic）统一到一个本地入口，提供调度、配额、调用记录等能力，在本地集中管理并复用多套大模型密钥。

![Python](https://img.shields.io/badge/Python-3.8+-blue) ![GUI](https://img.shields.io/badge/GUI-PySide6-green)

![LightAIBox 主界面](homepage.zh-CN.png)

## 特性

- **统一 API 代理**：`chat` / `chat_stream` 一个接口屏蔽 OpenAI 与 Anthropic 协议差异，支持指定模型与按策略自适应、一次性与流式输出。
- **Claude Code 直连**：Anthropic 兼容端点完整透传 `tools` 与多轮 `tool_result`，流式 tool_use 遵循官方协议，可直接承载多步 agent 循环。
- **内置对话**：应用内即含微信式**对话**页，流式回复、Markdown + 离线 MathJax 渲染 LaTeX 公式与 mermaid 图表、每条回复可「原文 / 渲染」切换、思考模式折叠展示、可发送图片做多模态对话、可开启**智能体模式**走 LightAgents SuperAgent 编排（意图路由 + 工具发现 + 流式汇总）并配内置桌面工具、**沙箱文件产出（docx / xlsx）**、**浏览器自动化（开网页 / 点击 / 截图）**与**受闸门控制的 SSH 远程执行**、带时间分隔条（详见[内置对话](#内置对话)）。
- **多 Provider 管理**：可视化增删改查，支持**JSON 导入 / 导出**整份 Provider 配置（备份 / 迁移 / 分享），后台测连不卡界面、无弹窗；每个 Provider 可标记「支持多模态（图片）」，列表以 🖼 标识。
- **智能调度**：按策略（长输入优先 / 短输入优先）挑选，单 Provider 失败自动降级重试；含图片的请求只路由到标记为多模态的 Provider（无匹配则明确报错，不静默丢图），误标多模态的 Provider 发图连续失败后自动撤销标记。
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

点击 Provider 列表「新增」，填写名称（唯一）、协议类型、Base URL（末尾 `/v1` 可省略）、API Key、模型，按需勾选「支持多模态（图片）」。启用 / 停用统一由列表行右侧按钮控制（编辑对话框不改启用状态）。随后可在列表中编辑 / 复制 / 删除 / 重置配额 / 测连；也支持「导入 / 导出」以 JSON 整体替换或备份 Provider 配置。

## 统一 API 服务（HTTP）

本地 HTTP 服务**随应用启动自动拉起**，开箱即用（默认 `127.0.0.1:8765`）；**监听地址与端口在重启间记忆**，可在「统一 API」页修改。

- OpenAI 兼容：`POST /v1/chat/completions`、`GET /v1/models`
- Anthropic 兼容：`POST /v1/messages`

> **局域网访问**：在「统一 API」页把**监听地址**切到 `0.0.0.0`（所有网卡），同网段其它设备即可访问——页面会自动显示当前局域网 IP，用它替换下面的 `127.0.0.1`。⚠️ 该服务当前**不带鉴权**：绑 `0.0.0.0` 会把「用你配置的 provider 密钥代为转发」的代理暴露给任何能连到该端口的设备，仅建议在可信内网临时开放。

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
- **思考模式**：顶栏「思考：开 / 关」按钮（偏好持久化）。开启后模型先输出思考过程，
  气泡内以可折叠块展示，默认折叠、点击展开；随下一批消息生效，不回改历史。
- **多模态图片**：输入区「🖼 图片」按钮选择本地图片（png / jpg / jpeg / gif / webp，
  可多选），读成 base64 随下一条消息发送，气泡内以缩略图展示；`auto` 调度自动把含图
  请求路由到标记为多模态的 Provider，纯文本对话不受影响。
- **智能体模式**：顶栏「智能体：开 / 关」按钮（偏好持久化）。开启后回复由
  **LightAgents SuperAgent 编排循环**驱动而非单次补全：意图路由 → 子代理分发 → 工具
  发现 → 流式汇总。模型可调用内置只读桌面工具、产出沙箱文件（docx / xlsx，每次写入
  需确认）、观察结果、继续推理直至给出最终答案。工具活动（调了什么、参数、成败）以
  状态行呈现在气泡内、思考块上方，且随会话重放保留。详见[智能体模式](#智能体模式)。
- **主题自适应**：气泡 / 头像 / 时间颜色随暗 / 亮主题切换；对话画布透明化，换肤即时生效
  无「滞后一帧」，并配 6px 细滚动条。

### 智能体模式

顶栏开启**智能体：开**后，对话从单次流式补全切换为经同一本地网关驱动的
**LightAgents SuperAgent** 编排循环：

1. 模型收到对话历史与可用工具的 JSON Schema；
2. SuperAgent 理解意图、发现并分发相关工具（或子代理），把每一步随流式呈现；
3. 判断到需要外部信息 / 计算 / 产出文件时发出工具调用，应用**在本地执行该工具**并把
   观察结果回喂模型；模型基于观察继续推理（至多 5 步），最终汇总逐 token 刷入气泡。

关键特性：

- **协议感知**：循环同时支持两种 function calling 方言——OpenAI 的 `tool_calls` /
  `role: "tool"` 消息，与 Anthropic 的 `tool_use` / `tool_result` 内容块。首轮由网关
  自适应调度选 Provider，拿到工具调用后即**锁定该 Provider**，保证工具消息永不跨协议
  混传（上游会直接 400）。底层由 `GatewayLLM` 鸭子类型适配器（`app/gateway_llm.py`）
  把多 provider 网关伪装成 LightAgents 眼中的单一模型。
- **只读桌面工具**：`get_current_time`（本地日期 / 时间 / 时区 / 地点，由本地时区离线
  推断，不联网）、`calculator`（算术表达式，AST 白名单解析，`__import__` / `exec` 等
  一律拒绝）、`get_gateway_status`（已配置的 Provider、模型、用量 / 配额与速度，回答
  「现在能用什么模型」很方便）。
- **沙箱文件产出**：`write_text` / `write_docx` / `write_xlsx` 让智能体能真正把成果写
  成文件。用 python-docx / openpyxl **结构化生成**（不执行任意脚本、不依赖外部 CLI），
  仅写入会话沙箱目录 `文档/LightAIBoxOutputs/<日期>/<会话>/`（`app/config.py`
  `OUTPUT_ROOT`），上限 20 MB；**每次写入前都弹窗确认**（路径 / 类型 / 大小），确认后
  才落盘（`app/approval.py` + `app/file_tools.py`）。产出的文件以 `🔗 打开` 链接呈现，
  点击即在系统默认应用中打开。
- **天气 / 定位**：`weather`（联网）返回某城市多日天气预报（也可只报地名、自动解析出城市），
  `get_current_location` 按 IP 给出当前位置。两者均**按需发现**——不常驻模型工具表，问天气 /
  问我在哪时经 FindTools 检索后注入。
- **浏览器自动化（Playwright）**：`browser` 工具能真实**打开网页**、抓取可见文本、点击 /
  填表 / 滚动 / 按键、执行 JS 与截图，联动本机 Google Chrome，并以**有头（可见）窗口**呈现。
  同样**按需发现**（说要「打开浏览器 / 访问网址 / 上某网站查询」时命中）；**改变页面状态的
  动作**（`goto` / `click` / `fill` 等）在回答气泡内弹「确认」提示（明示动作与参数）闸门，
  而只读抓取（text / 截图）自由放行。在基础动作之上，它：
  - **认证界面提示**：页面需要登录 / 验证码时提示在 Chrome 窗口手动完成后再继续；
  - **持久专用 profile 复用**：优先接管 / 重新拉起**持久专用 Chrome profile**（若按工具调试
    端口运行可径直接管该实例），登录态跨会话保留——不再每次都以「零信任新设备」身份访问，
    从而显著降低安全 / 人机验证频率；
  - **去除自动化指纹**：有头模式不带 `--no-sandbox` / `--disable-dev-shm-usage` 等被百度等
    站点识别为脚本的标志，避免反复弹验证；
  - **关应用不杀浏览器**：退出 LightAIBox 时断开接管而非连带关闭，网页继续留在桌面供阅读。
- **SSH 远程执行（Python）**：经审批配置了远端主机后，`ssh_exec` 可在服务器上经 SSH 执行命令；
  **每次执行前都弹窗确认「主机 + 命令」**才运行，凭据来自已配置会话、模型不可见。按需发现
  （说「执行命令 / 连服务器 / 部署」时命中）。
- **失败不中断**：工具报错转为观察结果（`❌ …`）回喂模型，可重试或如实说明，而非直接
  终止循环。
- **透明 UI**：每一步都随流式呈现——工具调用与结果以 `🔧 ✓ calculator 2+3*4 = 14`
  状态行出现在气泡内，思考段进可折叠块（若开启思考模式），最终答案按常规 markdown
  渲染。
- **结构化子任务渲染**：智能体的产出按「子任务 → 各步骤」分块展示。单任务时直接呈现
  行动与结论（不再带冗余编号）；而同时含「分析/规划 + 产出/撰写」的复合诉求（如「对比
  X 和 Y，并写成报告」）会自动拆成多个子任务——每个以 `⚙ 子任务 N · 类型` 标题展示——
  末尾再以 LLM 汇总生成最终回答。

底层编排来自外部 LightAgents 框架（`../LightAgents`，SuperAgent + 子代理 +
ToolCatalog / FindTools 工具发现），经 `app/agent_bridge.py` 流式接入；桌面工具注册表在
`app/gateway_llm.py` + `app/agent_tools.py`，其中浏览器 / SSH / 天气工具来自 LightAgents
内置的 `browser_tool` / `ssh_exec_tool` / `weather_tool`（Playwright 与 paramiko 为可选
依赖，缺失时自动跳过不注册）。

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
├── agent_bridge.py  # 智能体模式：LightAgents SuperAgent 编排，经网关流式接入
├── gateway_llm.py   # GatewayLLM（网关伪装成单一 LightAgents LLM）+ StreamingSuperAgent + 桌面工具注册表
├── agent_tools.py   # 内置只读桌面工具 + JSON Schema 导出
├── approval.py      # 跨线程写入确认协调器（提案 + 每次写入弹窗确认）
├── file_tools.py    # 沙箱文件产出（write_text / write_docx / write_xlsx）
└── ui/              # PySide6 界面
    ├── chat_page.py # 「对话」页：微信式气泡 + MathJax 公式渲染 + 智能体模式
    ├── providers_bar.py # 悬浮置顶面板：标题条 + 运行中 Provider + 用量/余量（模型/用量右对齐，锁定/关闭按钮）
    └── resources/chat/  # 对话容器 HTML + 本地打包 MathJax v3 + mermaid（离线）
```

> LightAgents 智能体框架现引用外部源码库 `../LightAgents`（此前内嵌于 vendor/）。

## 许可证

本项目采用 [MIT 许可证](LICENSE)，可自由使用、修改与分发，含商业用途。

## 特别鸣谢

感谢**老婆大人**提供的阿里云账号，让我能多蹭一份免费的大模型 token。💖
