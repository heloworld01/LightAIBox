"""B 优化真机验证脚本（开发者自测，非产品代码）。

验证「用**持久专用 profile** 带 --remote-debugging-port 拉起 Chrome → connect_over_cdp
接管 → 打开百度搜索」这一整条 B2 路径是否真实生效。

前置条件（本脚本不会替你关闭 Chrome，请在外部确认桌面 Chrome 已完全退出，
否则同 profile 目录可能被锁）：建议先
    powershell "Get-Process chrome -EA SilentlyContinue | Stop-Process -Force"

运行（在项目根目录）：
    python tool-output/verify_b2_realtime.py
"""
import asyncio
import os
import re
import sys

# 开发态：LightAgents 是外部源码库，需显式加进 sys.path（与 app/gateway_llm.py 的
# _ensure_lightagents 一致）。
for _cand in (os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "LightAgents")),
              r"D:\project\LightAgents"):
    if _cand not in sys.path:
        sys.path.insert(0, _cand)

from light_agents.tools.builtin.browser_tool import BrowserTool

# 命中即视为「触发安全/人机验证或风控页」的关键词
RISK_KEYWORDS = re.compile(r"验证|人机|滑块|风控|robot|captcha|verify|安全检测|阅读", re.I)


def _flag(name, ok, detail=""):
    mark = "✅" if ok else "❌"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


async def main():
    import os
    udd = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "LightAIBox", "chrome-profile")
    print(f"[待接管专用 profile] {udd}")
    print(f"[待接管调试端口] http://localhost:9333")

    t = BrowserTool(headless=False, use_chrome=True, persist_on_desktop=True,
                    default_timeout_ms=25000)
    try:
        # 走完整回退链：attach-running → attach-launched(真实profile+调试端口) → 隔离profile
        await asyncio.wait_for(t._ensure_open(), timeout=60)
        persistent = t._pw_instance_persistent
        # 接管专用 Chrome 成功的判据：_pw_instance_persistent 为 False（非隔离持久 context）
        attached = persistent is False and t._browser is None
        _flag("B2 接管专用 desktop Chrome", attached,
              f"persistent={persistent}（False=接管成功；True=落到隔离 context）")

        # 打开百度首页
        r1 = await asyncio.wait_for(t._arun({"action": "goto", "url": "https://www.baidu.com"}),
                                    timeout=45)
        txt1 = r1.text or ""
        _flag("打开百度首页", getattr(r1, "status", None) and "200" in txt1,
              (txt1 or "无返回").splitlines()[0][:120])
        _flag("首页未命中验证/风控", not RISK_KEYWORDS.search(txt1))

        # 触发一次搜索请求，观察是否弹验证
        r2 = await asyncio.wait_for(
            t._arun({"action": "goto",
                     "url": "https://www.baidu.com/s?wd=Playwright%E6%B5%8F%E8%A7%88%E5%99%A8"}),
            timeout=45)
        txt2 = r2.text or ""
        _flag("执行百度搜索请求", getattr(r2, "status", None) and "200" in txt2,
              (txt2 or "无返回").splitlines()[0][:120])
        hit = RISK_KEYWORDS.search(txt2)
        _flag("搜索结果未命中验证/风控", not hit,
              f"命中词={hit.group(0) if hit else None}")

        # 抓一下当前页正文，看是否复用登录态/正常出结果
        r3 = await asyncio.wait_for(t._arun({"action": "text", "max_chars": 600}), timeout=30)
        body = (r3.text or "")[:600].replace("\n", " ")
        print(f"[正文片段] {body}")

        print("\n—— 收尾：release_browser(kill_browser=False)，应保留 Chrome 窗口 ——")
        t.release_browser(kill_browser=False)
        _flag("release_browser 后工具句柄已清空",
              t._context is None and t._page is None and t._pw_instance is None)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        try:
            t.release_browser(kill_browser=True)
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
