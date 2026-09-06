"""桌面工具注册表：为智能体模式提供安全、只读的本地工具。

设计取舍（与 docs 草案一致，首版刻意保守）：
- 工具是**纯 Python 可调用对象 + JSON Schema**，不依赖 LightAgents 的 Tool 基类——
  ReAct 循环在 agent_bridge 里用协议感知方式驱动，避免引入额外适配层。
- 全部为**只读 / 无副作用**工具：时间、计算器、当前 provider 状态。不执行 shell、
  不读写任意文件、不联网。真实桌面操作（截屏、开应用、读写文件）留待后续接入，
  且必须经用户确认闸门后才落地。
- `run` 返回字符串；抛异常由上层捕获成错误观察结果喂回模型，不中断循环。
"""
from typing import Callable, Dict, List


# 每个工具：name / description / parameters(JSON Schema) / run(dict)->str
ToolDef = Dict[str, object]


def _now(args: dict) -> str:
    import datetime
    fmt = "%Y-%m-%d %H:%M:%S"
    tz = datetime.datetime.now().astimezone()
    return f"{tz.strftime(fmt)} ({tz.tzname()})"


_NOW_TOOL: ToolDef = {
    "name": "get_current_time",
    "description": "获取当前本地日期与时间。需要知道「现在几点 / 今天几号」时调用。",
    "parameters": {"type": "object", "properties": {}, "required": []},
    "run": _now,
}


def _calc(args: dict) -> str:
    """安全计算器：仅支持算术表达式（复用 LightAgents 内置计算器的 ast 思路）。"""
    expr = (args.get("expression") or "").strip()
    if not expr:
        raise ValueError("表达式为空")
    import ast
    import operator

    ops = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
    }
    funcs = {"abs": abs, "round": round, "min": min, "max": max, "sum": sum}

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"不支持的常量: {node.value!r}")
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in funcs and not node.keywords:
            return funcs[node.func.id](*[ev(a) for a in node.args])
        raise ValueError("表达式含不支持的运算")

    # ast.parse 拒绝除表达式外的任何语句，天然阻断 __import__ / exec 等
    tree = ast.parse(expr, mode="eval")
    result = ev(tree)
    return f"{expr} = {result}"


_CALC_TOOL: ToolDef = {
    "name": "calculator",
    "description": ("计算数学表达式，支持 + - * / // % ** 括号与 abs/round/min/max/sum。"
                    "例如 '2+3*4'、'(1+2)**10'。"),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "要计算的算术表达式"},
        },
        "required": ["expression"],
    },
    "run": _calc,
}


class DesktopTools:
    """按会话装配的工具集合。gateway/provider_id 供上下文类工具读取运行态。"""

    def __init__(self, gateway=None, provider_id=None):
        self.gateway = gateway
        self.provider_id = provider_id
        self._tools: Dict[str, ToolDef] = {}
        for t in (_NOW_TOOL, _CALC_TOOL):
            self.register(t)
        # 仅当注入了 gateway 时提供「查询当前网关状态」工具
        if gateway is not None:
            self.register(self._make_status_tool())

    def register(self, tool: ToolDef) -> None:
        self._tools[tool["name"]] = tool

    def list_tools(self) -> List[ToolDef]:
        return list(self._tools.values())

    def schemas_openai(self) -> List[dict]:
        return [
            {"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["parameters"]}}
            for t in self._tools.values()
        ]

    def schemas_anthropic(self) -> List[dict]:
        return [
            {"name": t["name"], "description": t["description"],
             "input_schema": t["parameters"]}
            for t in self._tools.values()
        ]

    def execute(self, name: str, args: dict) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"❌ 未找到工具 '{name}'"
        run: Callable[[dict], str] = tool["run"]  # type: ignore[assignment]
        try:
            return run(args or {})
        except Exception as exc:  # noqa: BLE001 —— 工具异常转成观察结果喂回模型
            return f"❌ 工具执行失败：{exc}"

    # ------------------------------------------------------------------ #
    def _make_status_tool(self) -> ToolDef:
        def _status(_args: dict) -> str:
            gw = self.gateway
            lines = []
            try:
                store = getattr(gw, "providers", None)
                providers = list(store.list()) if store is not None else []
            except Exception:
                providers = []
            avail = [p for p in providers if getattr(p, "is_available", lambda: False)()]
            lines.append(f"provider 总数={len(providers)}，可调度={len(avail)}")
            for p in avail[:8]:
                mm = "多模态" if getattr(p, "multimodal", False) else "文本"
                qt = getattr(p, "quota_type", "")
                used = getattr(p, "used_calls", 0)
                lim = getattr(p, "quota_limit", 0)
                quota = f"已用{used}" + (f"/{lim}" if lim else "/∞")
                lines.append(
                    f"- {getattr(p,'model','?')}（{getattr(p,'name','?')}）"
                    f" [{getattr(p,'api_type','?')}/{mm}] {quota}"
                    f" · {getattr(p,'last_tokens_per_sec',0.0):.0f} tok/s ({qt})")
            if self.provider_id is not None:
                cur = next((p for p in providers if getattr(p, "id", None) == self.provider_id), None)
                if cur is not None:
                    lines.insert(0, f"当前锁定 provider: {getattr(cur,'model','?')} (id={self.provider_id})")
            return "\n".join(lines) if lines else "无可用的 provider 信息"

        return {
            "name": "get_gateway_status",
            "description": ("查询本地 AI 网关的运行状态：已配置的 provider、各自模型、"
                            "今日用量与速度。用于回答「现在能用什么模型 / 还剩多少额度」这类问题。"),
            "parameters": {"type": "object", "properties": {}, "required": []},
            "run": _status,
        }
