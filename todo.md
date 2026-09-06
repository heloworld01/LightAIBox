# TODO · 后续计划

> 创建：2026-09-03
> 两大目标：**① 实现对 Claude Code 的控制 / 编排 / 调度；② 实现启动谷歌浏览器完成动作（浏览器自动化）。**
> 关联文档：`docs/desktop-agent-design.md`（桌面智能体总体设计，本 TODO 是其落地执行版）

---

## 0. 初步方案设计（结论先行）

### 目标 ①：控制编排调度 Claude Code

LightAIBox 已具备 Anthropic 兼容端点 `/v1/messages`（含 `tool_use`/`input_json_delta` 流式透传，`selftest_cc.py` 已验证），Claude Code 可以指向本网关跑 agent 循环。因此「控制」分两层：

- **L1 · 被集成（反向控制，先做）**：LightAIBox 作为 Claude Code 的 **MCP server + 模型网关**。
  Claude Code 负责思考与循环，LightAIBox 提供：多 provider 调度/配额/故障切换（已有）、桌面工具（浏览器、剪贴板、截图…见目标 ②）、执行审计与确认弹窗。
  → 新增 `app/mcp/server.py`（MCP over streamable-HTTP，挂进现有 uvicorn 线程）。
- **L2 · 主动编排（正向控制，后做）**：LightAIBox 自己拉起并驱动 claude 进程。
  用 **`claude --output-format stream-json --input-format stream-json`（headless/SDK 模式）** 作为子进程接口，由 Python 侧读写 JSON 行协议：下发任务 prompt、解析 `assistant/tool_use/result` 事件、注入权限决策（`canUseTool` 风格的确认回传）、暂停/终止、并行多实例构成 worker 池。
  → 新增 `app/agent/cc_supervisor.py`（进程管理 + 协议编解码）+ 任务队列 + UI「编排」页。
  备选方案：官方 Claude Agent SDK（Python），但会引入 node 运行时依赖，打包复杂，优先裸 CLI stream-json。

### 目标 ②：启动 Chrome 完成动作

采用设计文档既定路线：**CDP（Chrome DevTools Protocol）为主，零重依赖**。

- LightAIBox 代为拉起 Chrome：`chrome.exe --remote-debugging-port=9222 --user-data-dir=%LOCALAPPDATA%\LightAIBox\chrome-profile`（独立 profile，避免污染日常浏览，也保证可复现）。
- 通过 `http://127.0.0.1:9222/json` 发现 target，`websockets` 连接 debugger，调用：
  `Page.navigate` / `Runtime.evaluate` / `DOM.*` / `Input.dispatchMouseEvent|dispatchKeyEvent` / `Page.captureScreenshot`。
- 封装成工具注册表 `ToolSpec`（`app/capabilities/browser.py`），同一套 handler 双向暴露：给 L1 当 MCP tools，给 L2 当编排器可直接调用的本地动作。
- 兜底：CDP 不可用时用 UIA（`uiautomation` 纯 Python）读窗口/点击，仅做降级路径。

两个目标的汇合点：**工具注册表（registry）是公共底座**——Claude Code 经 MCP 调它，自建编排器经函数调用直接用它，UI 审计/授权面板管它。

---

## 1. 阶段拆分

### Phase A · 地基（registry + 首批只读能力）
- [ ] `app/capabilities/registry.py`：`ToolSpec`（name/description/input_schema/handler/risk/requires_approval）。
- [ ] `app/capabilities/screen.py`：截图（Qt `QScreen.grabWindow`，零新依赖）。
- [ ] `app/capabilities/clipboard.py`：剪贴板读写（QClipboard）。
- [ ] `app/db.py` 迁移：新增 `tool_grants`（授权）、`tool_calls`（审计）两表。
- [ ] 临时联调端点 `POST /desktop/{tool}`（走 registry，供脚本自测）。

### Phase B · Chrome 自动化（目标 ② MVP）
- [ ] `app/capabilities/chrome_launcher.py`：定位 chrome.exe（注册表/常见路径）→ 带调试端口拉起/复用 → 健康探测 `/json/version`。
- [ ] `app/capabilities/browser.py`（CDP 客户端，基于 websockets，需加入 requirements.txt）：
  - [ ] `browser_open(url)` — 开标签并导航
  - [ ] `browser_snapshot()` — 当前页标题/URL/正文文本/DOM 摘要
  - [ ] `browser_screenshot()` — `Page.captureScreenshot`
  - [ ] `browser_click(selector)` / `browser_type(selector, text)` — `Input.dispatch*` 或 `Runtime.evaluate` 组合
  - [ ] `browser_eval(js)` — 受限读取（P0 只允许只读语义）
- [ ] 风险分级：open/click/type = `side_effect`，首次弹 Qt 确认框，可按域名记住授权。
- [ ] 冒烟脚本：`selftest_browser.py`（打开 bing → 搜索关键词 → 抓首条结果标题回显）。

### Phase C · MCP server（目标 ① L1）
- [ ] `app/mcp/server.py`：MCP streamable-HTTP，`initialize` / `tools/list` / `tools/call`，挂载到现有 uvicorn app（`/mcp`）。
- [ ] 把 Phase A/B 全部工具注册进 MCP。
- [ ] Claude Code 接入文档 + 一键复制配置片段：`claude mcp add lightaibox --transport http http://127.0.0.1:8765/mcp`。
- [ ] UI「Agent」页：工具清单开关、授权记录、执行日志、MCP 连接信息。
- [ ] 端到端验收：Claude Code 经网关模型 + lightaibox MCP 完成「帮我打开 xx 网页并总结」类任务。

### Phase D · Claude Code 主动编排（目标 ① L2）
- [ ] spike：手工验证 `claude -p --output-format stream-json --verbose` 的事件流格式（system/assistant/result/工具权限请求），确认版本兼容矩阵。
- [ ] `app/agent/cc_supervisor.py`：
  - [ ] 子进程生命周期（启动/心跳/stdin 下发/stdout 逐行解析/终止杀进程树）
  - [ ] `--permission-mode` / `--allowedTools` 参数策略映射（不同任务档位）
  - [ ] `--mcp-config` 自动生成本机 lightaibox MCP 配置（让被编排的 CC 天然获得浏览器工具）
- [ ] `app/agent/orchestrator.py`：任务队列（goal → 拆步 → 派给 CC worker → 收集 result → 汇总），先用串行单 worker，再扩并行池。
- [ ] UI「编排」页：新建任务、实时事件流视图、停止按钮（全局紧急停止 = 杀所有 CC 子进程 + 断开 CDP 键鼠通道）。
- [ ] 验收场景：GUI 里点一个任务「调研 X 并写报告」→ CC 干活 + 需要网页时经 MCP 调 LightAIBox 开 Chrome → 产出落盘。

### Phase E · 加固与收尾
- [ ] 安全复核：MCP/`/desktop` 仅绑定 127.0.0.1；危险工具默认关闭；确认弹窗全链路无绕过。
- [ ] 打包：websockets 等新依赖进 `lightaibox.spec` hiddenimports，onedir 冒烟测试（复用 `build_windows.sh` 流程）。
- [ ] i18n 中英文案补齐；README/UI_SPEC 更新；发布 v1.1.0。

---

## 2. 关键决策记录（ADR 摘要）

| # | 决策 | 选项 | 结论与理由 |
|---|---|---|---|
| 1 | CC 控制方向 | 只做被集成 / 只做主动编排 / 两者 | **两者都做，先 L1 后 L2**：L1 复用已有协议栈最快见效；L2 才满足"编排调度"完整语义 |
| 2 | CC 通信协议 | 包装 `claude` CLI 拼字符串 / stream-json 无头模式 / Agent SDK | **stream-json**：结构化可恢复，无 node SDK 打包负担 |
| 3 | 浏览器驱动 | Playwright / Selenium / 裸 CDP / UIA | **裸 CDP**：仅加 websockets 一个轻依赖；UIA 只做兜底 |
| 4 | Chrome 启动方 | 用户自开调试端口 / LightAIBox 代拉起 | **代拉起 + 独立 user-data-dir**：可复现、不污染日常 profile |
| 5 | 工具暴露形态 | 私有 HTTP / MCP / 网关内 tool 回填 | **MCP 为主**（生态标准，CC 原生支持）；网关内回填（通道②）推迟到 P2，侵入性大 |

## 3. 依赖与约束

- 新增 pip 依赖尽量 ≤2 个：`websockets`（CDP）、可选 `psutil`（杀进程树）。Phase D 若需要再评估。
- 运行前提：本机安装 Chrome；Phase D 需要 `claude` CLI 在 PATH（检测不到时 UI 给出安装引导）。
- 维持 `127.0.0.1` 绑定边界；Windows-only 不变。
- COM/UIA 相关调用收敛到单一后台 STA 线程（Phase B 兜底路径前完成 spike）。

## 4. 里程碑

- **M1（B 末）**：脱离任何 agent，纯代码即可让 Chrome 完成一次搜索动作。
- **M2（C 末）**：Claude Code 通过 MCP 使用浏览器工具完成任务 → 可对外演示的最小闭环。
- **M3（D 末）**：LightAIBox GUI 内一键派发任务给受编排的 Claude Code，全程可视化 + 可紧急停止。

## 5. 开放问题（下次讨论确认）

1. L2 编排是否要支持同时多个 CC worker（并行子任务）？还是 v1 先单任务串行？
2. 浏览器动作要不要录制回放（操作序列保存为可复用 macro）？
3. 确认弹窗对高频 click/type 是否太扰民 → 是否需要"任务级预授权"粒度？
4. CC 编排产出的文件落在哪个目录约定（workspace 根）？
