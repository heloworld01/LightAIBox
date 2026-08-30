# LightAIBox · 轻量化 AI 工具箱

基于 **PySide6** 的桌面端轻量级 AI 网关工具：把多个大模型 API 提供方（OpenAI 兼容 / Anthropic）统一到一个本地入口，提供调度、配额、调用记录等能力，在本地集中管理并复用多套大模型密钥。

![Python](https://img.shields.io/badge/Python-3.8+-blue) ![GUI](https://img.shields.io/badge/GUI-PySide6-green)

## 特性

- **统一 API 代理**：`chat` / `chat_stream` 一个接口屏蔽 OpenAI 与 Anthropic 协议差异，支持指定模型与按策略自适应、一次性与流式输出。
- **Claude Code 直连**：Anthropic 兼容端点完整透传 `tools` 与多轮 `tool_result`，流式 tool_use 遵循官方协议，可直接承载多步 agent 循环。
- **多 Provider 管理**：可视化增删改查，后台测连不卡界面、无弹窗。
- **智能调度**：按策略（长输入优先 / 短输入优先）挑选，单 Provider 失败自动降级重试。
- **配额控制**：按调用次数或 token 数设限，超配额自动停用、可一键重置。
- **调用记录与统计**：SQLite 持久化，支持按日期 / Provider 过滤并汇总调用次数、成功率与 token 用量。

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python -m app.main
```

首次运行自动创建数据库 `app/data/lightbox.db`。

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
└── ui/              # PySide6 界面
```

## 文档

- English: [README.md](README.md)

## 许可证

本项目未指定开源许可证，如需商用或分发请与作者确认。
