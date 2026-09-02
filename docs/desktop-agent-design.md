# LightAIBox「桌面智能体（Agentic Desktop）」扩展设计

> 状态：设计草案（v0.1） · 面向 Windows · 目标版本 v1.1.x
> 关联文档：`UI_SPEC.md`、`RELEASE_NOTES_v1.0.2.md`

---

## 1. 目标与背景

LightAIBox 目前是一个**本地 AI 网关**：进程内跑着一个绑定 `127.0.0.1:8765` 的 uvicorn 服务，对外暴露 OpenAI / Anthropic 兼容接口，对内用调度 + 配额 + 记录管理多个上游 provider；同时配有一个 PySide6 桌面 GUI（托盘后台运行）。

用户希望评估：是否能让 LightAIBox 更进一步，成为类似 OpenAI Codex / Claude Code 的「桌面智能体」——**不只是替外部工具转发模型调用，而是自己驱动浏览器和其它 Windows 应用，辅助完成真实工作**（例如：打开浏览器 → 搜索 → 读取页面 → 操作表单 → 汇总结果；或操作 Excel / 文件 / 桌面窗口）。

### 结论（TL;DR）

**完全可行，且有一个非常自然、低风险的落点**：把 LightAIBox 作为「**本地 MCP / 工具执行宿主**」+「**面向 Claude Code 这类外部 agent 的能力扩展**」。理由有二：

1. **协议与数据模型已经为工具调用预留好了**。`app/models.py` 的 `ContentBlock(type="tool_use"/"tool_result")` 是协议无关的中性表示，`app/server.py` 的 Anthropic 流式回放已经完整支持 `tool_use` / `input_json_delta` 块——这正是为了让外部 agent（Claude Code）跑多步工具循环而写的。换句话说，**「让模型调用工具」这件事，当前代码已经能透传，只是还没有「工具」可调用。**
2. **进程形态恰好是一个常驻 Windows 图形应用**，主线程跑 Qt、后台守护线程跑 HTTP。驱动 Windows 应用所需的 Win32 / UI Automation 调用，完全可以在本进程内安全地暴露成 MCP 工具或本地 HTTP 端点。项目里已用了 `ctypes.windll` 做 AppUserModelID，说明「原生 Win32 集成」是既定且被接受的模式。

---

## 2. 现状盘点（可复用资产与约束）

### 2.1 可直接复用的部分

| 资产 | 位置 | 如何复用 |
|---|---|---|
| 中性工具调用模型 | `app/models.py: ContentBlock`（`tool_use/tool_result/thinking`） | 桌面工具的执行结果可直接封装为 `tool_result` 块回传 |
| Anthropic 工具流式回放 | `app/server.py: _anthropic_stream` | 已有 `tool_use`/`input_json_delta` 回放能力，桌面智能体可吃同一套 SSE |
| OpenAI/Anthropic 兼容层 | `app/server.py: build_app` | 桌面智能体可直接 `POST /v1/messages`，复用全部协议翻译 |
| 调度/配额/记录 | `app/gateway.py: Gateway`、`app/db.py` | 「每次桌面工具带来的模型调用」也走 Gateway，自动计入配额与调用记录 |
| 常驻进程 + 托盘 | `app/main.py` | 智能体宿主即本进程，无需另起服务 |
| Win32 集成先例 | `app/main.py: _setup_app_user_model_id`（`ctypes.windll`） | 证明原生 Windows API 集成路径可用 |
| i18n 框架 | `app/ui/i18n.py` | 新 UI 与错误信息可复用中/英文案 |

### 2.2 需要新增的部分

- **能力执行层（capability layer）**：真正去驱动浏览器 / Excel / 文件系统 / 剪贴板 / 窗口的代码。当前完全没有。
- **工具协议网关**：把上述能力暴露给模型的机制。当前有 LLM 协议（OpenAI/Anthropic），但**没有 MCP 服务端、没有工具注册表**。
- **安全与授权模型**：驱动真实应用 = 高风险副作用操作，必须有确认、白名单、沙箱边界。
- **UI**：工具清单、权限开关、执行日志/回放。

### 2.3 关键约束

1. **刻意绑定 `127.0.0.1`（安全设计）**。任何要跨机器/跨用户的能力都不能破坏这个边界；桌面能力必须只对本机、当前用户会话可见。
2. **PySide6 + 守护线程的单一进程模型**。drive 浏览器/窗口的能力要么在 Qt 主线程（有事件循环），要么在后台线程（需谨慎处理 COM 线程模型，见 §6）。
3. **Windows-only 现状**（manifest、`ctypes.windll`、AppUserModelID）。本设计第一版只做 Windows，暂不考虑跨平台。
4. **打包是 onedir PyInstaller**。新增原生依赖（如 pywin32 / pywinauto / uiautomation）需确认能否被 PyInstaller 正确收集进 `_internal`（见 §9）。

---

## 3. 能力范围：一个「Codex 式桌面智能体」应该做什么

按从低到高风险 / 从易到难分层，建议**分三批落地**：

### 第一批（P0，无副作用、纯只读，快速建立信任）
- 剪贴板读写（`get/set`）
- 文件系统（`list` / `read`，限制在用户许可的目录白名单内）
- 打开默认浏览器到指定 URL（`shell open`）
- 截图（当前屏幕 / 指定窗口，回传给模型做视觉理解）
- 键盘键入 / 鼠标点击前的「dry-run 预览」（只描述不执行）

### 第二批（P1，有界副作用，引入确认机制）
- 浏览器自动化：在已打开标签页/窗口上读取 DOM、点击、填表单、提取文本（经 WebDriver / DevTools 协议 / UI Automation）
- 剪贴板 + 键入 + 点击组合成「宏」执行
- 读写常见办公文件（Excel / Word，经 COM 自动化）
- 应用窗口操作：枚举 / 聚焦 / 最小化 / 关窗（仅限白名单的应用）

### 第三批（P2，进阶，视反馈决定）
- 后台浏览器控制（Playwright / Selenium 驱动的无头或有头新实例）
- 长任务编排：多步任务队列、断点续跑、用户批准流
- 权限策略精细化（按目录 / 按应用 / 按域名授权，可审计）

> 每批能力的**授权与确认策略**见 §7。

---

## 4. 总体架构

```
                        ┌──────────────────────────────────────────────┐
                        │              外部 agent（Claude Code 等）        │
                        │       POST /v1/messages  (Anthropic 兼容)      │
                        └──────────────────────┬───────────────────────┘
                                               │ SSE（文本 + tool_use 块）
┌──────────────────────────────────────────────▼─────────────────────────┐
│                         LightAIBox 进程（常驻）                           │
│                                                                          │
│  ┌──────────────┐   ┌───────────────────────────────────────────────┐   │
│  │  PySide6 GUI  │   │  uvicorn 守护线程                              │   │
│  │  （主线程）    │   │   • /v1/messages（Anthropic）                  │   │
│  │               │   │   • /v1/chat/completions（OpenAI）             │   │
│  │  新增：        │   │   • /mcp  （新增：MCP over SSE 或 streamable  │   │
│  │  - 工具清单页  │   │     HTTP，可选）                               │   │
│  │  - 权限面板    │   │   • /desktop/* （新增：本地桌面能力 HTTP API）  │   │
│  │  - 执行日志    │   └───────────────┬───────────────────────────────┘   │
│  └───────┬───────┘                   │                                     │
│          │ 事件/信号                   │                                     │
│  ┌───────▼──────────────────────────▼─────────────────────────────────┐ │
│  │                    Agent Orchestrator（新增）                       │ │
│  │   工具注册表 + 工具执行器 + 授权策略 + 会话/任务状态                   │ │
│  └───────┬────────────────────────────────────────────────────────────┘ │
│          │ 调用能力执行层                                                  │
│  ┌───────▼──────────────────────────────────────────────────────────────┐│
│  │ Capability Layer（新增，app/capabilities/）                           ││
│  │  • clipboard   • filesystem   • browser(CDP/WebDriver)               ││
│  │  • office(COM) • window(UIA)  • screen(screenshot)                   ││
│  └──────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.1 两种接入方式（推荐主从两用）

**方式 A：纯「工具执行宿主」——只提供工具，agent 逻辑留在 Claude Code / Codex 里（首选 P0）**

这是**最契合现状、风险最低**的做法，也是 Codex/Claude Code 的真实工作方式：**agent 循环（思考 → 选工具 → 拿结果 → 再思考）由客户端做，LightAIBox 只负责提供「桌面工具」本身，作为 MCP 服务端或一组附加 HTTP 工具端点**。

- LightAIBox 暴露一个 **MCP server**（Model Context Protocol）。用户在 Claude Code 配置里加 `lightaibox` MCP server，即可让 Claude Code 获得「操作你 Windows 桌面/浏览器」的工具。
- 也可退化为**纯 Anthropic 工具扩展**：在 `/v1/messages` 的上游转发之外，把本机工具也注册进 Anthropic 协议的 `tools` 里，让**任何**通过本网关的 agent 请求都能触发桌面工具（由网关在收到 `tool_use` 时执行本机工具、把 `tool_result` 回填给上游）。这一点 §5.2 详述。

**方式 B：自研「完整桌面智能体」——LightAIBox 自己跑 agent 循环（P1/P2 进阶）**

在 A 的基础上，新增一个内置 orchestrator，自己维持对话上下文 + 工具调用循环 + 前台执行。这是「把 Claude Code 塞进 LightAIBox」的形态，工程量大、风险高，建议只在 P0 验证有效需求后做。

---

## 5. 工具协议设计（核心）

### 5.1 工具注册表

新增 `app/capabilities/registry.py`，统一描述一个工具的元数据（JSON Schema 风格，便于映射到 MCP Tool 与 Anthropic `tool` 定义）：

```python
@dataclass
class ToolSpec:
    name: str                 # 如 "browser_open"
    description: str          # 给模型看的说明
    input_schema: dict        # JSON Schema，约束参数
    handler: Callable         # 实际执行函数（同步/异步）
    risk: str = "read"        # read | write | side_effect
    requires_approval: bool = True
    scopes: list = field(default_factory=list)  # 需要的权限 scope，如 ["filesystem:read"]
```

注册表在启动时装配一次，供两种协议共用：

- **MCP 形态**：`ToolSpec` → MCP `Tool`（`name/description/inputSchema`）。
- **Anthropic 形态**：`ToolSpec` → `{"name", "description", "input_schema"}`，作为 `/v1/messages` 转发给上游时的 `tools` 数组。

### 5.2 让工具进入「模型可调用」的三种通道

按实现成本递增：

1. **MCP Server（首选、解耦）**
   `app/mcp/server.py`：实现一个 MCP 服务端，`tools/list`、`tools/call` 背后调用 `registry`。
   - 通过标准 MCP 客户端接入 Claude Code / Codex，无需改动既有模型协议。
   - 传输层用 **streamable HTTP** 或 **stdio**。stdio 需要独立打包一个可执行入口；HTTP 则可挂在现有 uvicorn 线程里，复用进程（本设计首选 HTTP，对齐「常驻进程」定位）。

2. **网关内工具回填（Agent 无感、对任意 agent 生效）**
   在 `app/server.py` 的 `/v1/messages`（Anthropic）与 `/v1/chat/completions`（OpenAI）里：
   - 若请求带了 `tools`，且工具名命中 `registry` 里的**本地工具**，则**不转发给上游**，由网关本地执行 → 生成 `tool_result` 块 → 回放给客户端。
   - 这是最精巧也最有侵入性的一步：它让「Codex / Claude Code 通过 LightAIBox 调模型」时，模型返回的 `tool_use`（针对桌面工具）会落在本机执行。当前 `_anthropic_stream` 已经能透传 `tool_use`，此处只需在这一层「截获本地工具名并本地执行、把结果作为 `tool_result` 内容块注入对话」。

   > ⚠️ 注意：这一步要求网关在流式过程中**再发一次上游请求**（把 `tool_result` 回填后取得模型下一步输出），即网关需要做 agent 循环的部分职责。复杂度高，建议放在 P1。

3. **独立本地 HTTP `/desktop/*` 端点（最朴素）**
   一条条工具对应成 `POST /desktop/browser/open`、`POST /desktop/clipboard/get` 等。仅作内部/调试用，或给非 MCP 的简单脚本调用。不参与 agent 协议，作为 P0 的落地与联调手段。

### 5.3 工具调用时序（MCP 通道，P0 目标态）

```
外部 agent(Claude Code)                 LightAIBox（本进程）
       │                                      │
       │  initialize / tools/list             │
       │ ───────────────────────────────────► │ registry 汇总 → Tool[] 返回
       │  tools/call {name:"browser_open",    │
       │             arguments:{url:...}}     │
       │ ───────────────────────────────────► │ 授权检查(§7)
       │                                      │  → 通过: 调用 Capability 执行
       │  ←────────────── {content:[...]} ────│  → 被拒/需确认: 返回需确认提示
       │                                      │
```

---

## 6. 能力执行层：如何驱动 Windows 应用

### 6.1 浏览器自动化 —— 首选 CDP（Chrome DevTools Protocol）

- 推荐用 **Chrome/Edge 的 DevTools 远程调试端口**（`--remote-debugging-port`）直接连接，避免引入 Playwright/Selenium 重依赖。
- 方案对比：

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| CDP over WebSocket（`raw websocket` + `json`） | 零重依赖、可复用现有 `websockets`/`httptools` | 需自己实现少量协议消息 | **P0 首选**（已有 websockets 依赖，见 `lightaibox.spec` hiddenimports） |
| Playwright（`playwright` 包 + 下载驱动） | 高层 API、跨浏览器、稳定 | 体积大、每次需 `playwright install`、打包复杂 | P1 备选 |
| Selenium + chromedriver | 生态成熟 | 额外驱动进程、打包与版本匹配麻烦 | 不推荐 |
| UI Automation 读屏幕 + 模拟点击 | 无需浏览器配合 | 脆、慢、无 DOM 语义 | 仅作兜底/无法用 CDP 时 |

- CDP 关键能力映射到工具：`Page.navigate`（打开）、`Runtime.evaluate`（读/操作 DOM）、`DOM.getDocument` + `DOM.querySelector`（定位元素）、`Input.dispatchMouseEvent/dispatchKeyEvent`（点击键入）、`Page.captureScreenshot`（截图）。

### 6.2 桌面窗口 / 普通 Windows 应用 —— UI Automation (UIA)

- 用 `uiautomation`（纯 Python、含 UIA COM 封装）或直接 `ctypes` 调 `UIAutomationCore.dll`。
- 能力：枚举窗口、读窗口标题/控件树、读控件值、触发 `Invoke` 等。
- **线程模型注意**：UIA 的 COM 需要消息循环。跨线程调用要在**初始化 COM 的 STA 线程**上执行（可用一个专门的后台线程跑 `CoInitializeEx`），或经 Qt 主线程的信号槽调度到 GUI 线程。这是本项目单进程模型下最需要仔细处理的技术点，设计上把「所有 UIA 调用」收敛到一个 `CapabilityWorker` 后台线程串行执行，避免与 Qt/uvicorn 线程竞争 COM。

### 6.3 办公文件 —— COM 自动化（Excel/Word）

- `pywin32`（`win32com.client`）驱动已安装的 Office；或用不依赖 Office 的 `openpyxl`（读）/ `python-docx` 生成。
- 风险：COM 对象生命周期需显式管理，且 Office 是否安装是环境相关。建议工具显式探测可用性并在 `tools/list` 里动态隐藏不可用工具。

### 6.4 其它基础能力

- 剪贴板：Qt `QClipboard`（已在 GUI 线程可用）或 `win32clipboard`。
- 截图：`PIL.ImageGrab`（Windows 支持）或 Qt `QScreen.grabWindow`。
- 键入/点击：`pyautogui` 或 Win32 `SendInput`。**这是最高风险能力**，必须 P2 且强确认。

---

## 7. 安全与授权模型（重点）

驱动真实应用 = 提升权限 + 破坏性副作用风险。必须内建以下机制：

### 7.1 能力分级与默认策略

| 级别 | 示例 | 默认行为 |
|---|---|---|
| `read`（只读） | 读剪贴板、列文件、读 DOM、截图 | 允许（可在设置里关闭） |
| `write`（写，低风险） | 写剪贴板、写白名单内文件 | 首次需授权，可「本会话记住」 |
| `side_effect`（副作用） | 打开浏览器、聚焦窗口 | 需确认，可「按域名/按应用」记住 |
| `dangerous`（危险） | 模拟键鼠、关闭进程、删除文件 | **每次人工确认**，且默认关闭，需显式开启 |

### 7.2 目录/应用/域名白名单

- **文件系统**：默认只开放若干安全目录（如 `~/.lightaibox/workspace`、用户主动授权的目录），其余一律拒绝。`list/read` 与 `write/delete` 分开授权。
- **应用**：可操作的窗口/进程限制在白名单应用名（如仅浏览器、Office），避免误控系统进程。
- **域名**：浏览器自动化默认限制为「用户当前已打开的标签」或「用户授权的域名」，禁止任意跳转（防钓鱼/恶意站点注入）。

### 7.3 确认 UI

- 副作用/危险操作执行前，经 Qt 主线程弹**批准对话框**（复用现有 `MessageBox` 风格 + i18n），显示：工具名、参数、风险级别、目标。
- 用户可选「仅此次 / 本次会话 / 永久记住（写入授权表）」。授权表持久化到 `lightbox.db` 新增表 `tool_grants`。

### 7.4 审计

- 所有工具调用写入新增表 `tool_calls`（时间、来源 agent、工具、参数摘要、结果状态、授权方式），与现有 `call_logs` 并列，可在 UI 里查看。

### 7.5 失败与回滚

- 危险操作尽量提供「可撤销」：文件写操作先备份；键鼠宏支持 Esc/全局热键紧急停止；超时强制中断。

---

## 8. 数据与存储变更

新增 SQLite 表（`app/db.py` 追加迁移）：

1. `tool_grants`：授权记录（tool_name、scope、target、decision=allow/deny/ask、expiry）。
2. `tool_calls`：工具调用审计日志。
3. （P2）`tasks` / `task_steps`：长任务编排状态。

`models.py` 增加 `ToolGrant`、`ToolCall`。工具元数据（`ToolSpec`）是代码常量，不入库。

---

## 9. 打包与依赖影响（关键决策点）

新增能力依赖需逐个验证 PyInstaller onedir 兼容性：

| 依赖 | 用途 | 打包风险 | 建议 |
|---|---|---|---|
| `pywin32` | COM 自动化 | 中（有隐藏 import，PyInstaller 有官方 hook） | P1 引入，spec 加 `hiddenimports=['win32com', ...]` |
| `uiautomation` | UIA 窗口控制 | 低（纯 Python，依赖 COM 原生 dll 运行时存在） | P0/P1 引入 |
| `Pillow` | 截图/画图 | 低（PyInstaller 支持好） | P0 引入 |
| `pyautogui` | 键鼠 | 中（含鼠标定位，需 `pyscreeze` 等） | P2，谨慎 |
| Playwright / Selenium | 浏览器 | **高**（驱动二进制、下载） | **P1 才评估，优先走 CDP 零依赖方案** |

原则：**P0 阶段尽量零重依赖（CDP 走已有 websockets、截图走 Pillow 或 Qt、UIA 走纯 Python 封装）**，把重依赖（pywin32/Playwright）推迟到 P1 并单独做打包冒烟测试（复用 `build_windows.sh` 的验证要点）。

---

## 10. UI 变更

在现有 `MainWindow` 的 Tab 里新增一页「桌面智能体（Agent）」（或独立子 Tab 分组）：

- **工具清单**：列出所有注册工具、能力级别、授权状态（允许/询问/拒绝），可切换开关。
- **授权记录**：查看/撤销已记住的 `tool_grants`。
- **执行日志**：`tool_calls` 列表（时间、agent、工具、结果）。
- **连接信息**：显示 MCP 接入方式与配置片段（便于用户复制进 Claude Code / Codex）。

复用 `i18n.py` 做中/英文案。

---

## 11. 分阶段实施计划

### Phase 0（最小可用，纯只读，无副作用）
- [ ] `app/capabilities/` 建立：`registry.py`、`clipboard.py`、`filesystem.py`（白名单只读）、`browser.py`（CDP 读取）、`screen.py`。
- [ ] 本地 HTTP 端点 `/desktop/*`（通道 3）用于联调。
- [ ] `ToolSpec` 注册表 + `tool_grants`/`tool_calls` 表 + 授权策略（默认 read 放行）。
- [ ] UI：工具清单页（只读版本）。

### Phase 1（MCP 化 + 有界副作用）
- [ ] `app/mcp/server.py`：MCP over streamable-HTTP，`tools/list`、`tools/call`。
- [ ] 浏览器写操作（点击/填表，限制当前标签/授权域名）+ 确认对话框。
- [ ] 文件写操作（白名单内 + 备份）+ 剪贴板写。
- [ ] 打包验证：新增依赖进 `lightaibox.spec` 冒烟。

### Phase 2（进阶，视需求）
- [ ] 网关内工具回填（通道 2），让任意经本网关的 agent 都能触发桌面工具。
- [ ] 内置 orchestrator（方式 B）做完整 agent 循环。
- [ ] 键鼠宏（`pyautogui`，最高确认级别）、长任务队列、断点续跑。

---

## 12. 风险与开放问题

1. **安全是首要风险**：一旦让模型驱动真实桌面，恶意 prompt / 上游模型被污染都可能造成真实损害。白名单 + 确认 + 只读优先是本设计的底线，P0 必须先把授权模型做实。
2. **COM 线程模型**：UIA / Office COM 需在正确的 STA 线程上序列化执行，与 Qt 主线程、uvicorn 守护线程的关系需在 P1 前用 spike 验证。
3. **CDP 依赖浏览器开了 remote-debugging 端口**：需要向用户说明如何启动（或由 LightAIBox 代为拉起带调试参数的浏览器子进程——但那是副作用操作，需走授权）。
4. **打包体积与依赖收集**：Playwright/pywin32 会显著增大产物并引入收集复杂度，能 CDP/UIA 纯 Python 搞定就不用它们。
5. **`127.0.0.1` 边界**：MCP/桌面工具必须维持「仅本机当前用户」。若未来要做跨机，需整体重审安全模型（本版明确不做）。

---

## 13. 建议的下一步（讨论用）

- **确认业务形态**：是先做「工具宿主（喂给 Claude Code/Codex）」还是「自研完整智能体」？本设计强烈建议先做前者（方式 A + Phase 0/1）。
- **确认首批能力清单**：浏览器读取 + 文件只读 + 剪贴板 + 截图，是否覆盖最想解决的场景？
- **做一个 CDP 只读小 spike**：验证「连上用户已开的 Chrome/Edge、读当前页面文本、截图回传」，这是 P0 里风险最高、也最能证明价值的一步。