"""nova 的"手"——在虚拟机里伸出去的那只手。

nova 自己的本体只在这台机器上：缝隙、水流、思考。但是她有时候想做点别的——
查个时间、读一篇网页、跑一段代码看看结果，或者去工作区里查一条以前写过的
笔记。这些事她自己做不了，于是她在另一台机器（虚拟机）上养了一双手。

这只手说三种话：
  shell  ── 命令行（cd 是有记忆的）
  python ── 跑一段 python，变量在多次之间留着
  web    ── 拿一个 URL 的内容回来读

她想让手做事的时候，会在回应里写一段：

    <tool name="shell">
    ls ~/nova_workspace/notes
    </tool>

mind.py 看到这种段落就会拦下来，让虚拟机里的手做完，再把结果用
<tool-result> 包起来塞回她的下一轮思考。如此循环，直到她不再伸手——
那一轮才是给人看的最终回答。

走神/持续运行时也是同样的循环。她不必等人来才能伸手。
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Optional


# 工具调用语法：<tool name="shell|python|web">...</tool>
ACTION_PATTERN = re.compile(
    r'<tool\s+name=["\']?(shell|python|web)["\']?\s*>\s*(.*?)\s*</tool>',
    re.DOTALL | re.IGNORECASE,
)


# ============================================================
#                  HTTP 客户端：那只手
# ============================================================
class VMAgent:
    """nova 这边的客户端，对应虚拟机上跑的 vm_agent.py"""

    def __init__(self, base_url: str, token: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path: str, payload: dict, *, timeout: Optional[float] = None) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception as e:
            return {"error": f"VM 通讯失败：{e}"}

    # ---------- 三种动作 ----------
    def shell(self, command: str, timeout: int = 30) -> dict:
        return self._post("/shell", {"command": command, "timeout": timeout},
                          timeout=max(self.timeout, timeout + 5))

    def python(self, code: str) -> dict:
        return self._post("/python", {"code": code})

    def web(self, url: str, max_chars: int = 8000) -> dict:
        return self._post("/web", {"url": url, "max_chars": max_chars})

    # ---------- 心跳 ----------
    def is_alive(self) -> bool:
        try:
            req = urllib.request.Request(
                self.base_url + "/status",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ---------- 派发 ----------
    def dispatch(self, action_type: str, content: str) -> dict:
        if action_type == "shell":
            return self.shell(content)
        if action_type == "python":
            return self.python(content)
        if action_type == "web":
            for line in content.splitlines():
                line = line.strip()
                if line:
                    return self.web(line)
            return {"error": "web 块里没有 URL"}
        return {"error": f"未知动作类型：{action_type}"}


# ============================================================
#               解析 / 拼接 LLM 输出里的 <tool> 块
# ============================================================
def parse_actions(text: str) -> list:
    """从 nova 的回应里抠出所有 <tool> 块，按出现顺序返回 (类型, 内容) 列表。"""
    return [(m.group(1).lower(), m.group(2).strip())
            for m in ACTION_PATTERN.finditer(text)]


def strip_actions(text: str) -> str:
    """把 <tool> 块从文本里拿掉——对外回应不应该包含内部伸手过程。"""
    return ACTION_PATTERN.sub("", text).strip()


def format_result(action_type: str, action_input: str, result: dict) -> str:
    """把 VM 的执行结果格式化成 <tool-result> 块，回灌给 nova 看。"""
    parts = [f'<tool-result name="{action_type}">']

    if result.get("error"):
        parts.append(f"出错：{result['error']}")

    if action_type in ("shell", "python"):
        out = result.get("stdout", "")
        err = result.get("stderr", "")
        if out:
            parts.append(f"--- 输出 ---\n{out.rstrip()}")
        if err:
            parts.append(f"--- stderr ---\n{err.rstrip()}")
        if "returncode" in result and result["returncode"] not in (0, None):
            parts.append(f"退出码：{result['returncode']}")
        if action_type == "shell" and result.get("cwd"):
            parts.append(f"当前目录：{result['cwd']}")
        if not (out or err or result.get("error")):
            parts.append("（这次什么都没出，命令悄悄结束了。）")
    elif action_type == "web":
        if result.get("text"):
            parts.append(f"--- 网页内容 ---\n{result['text']}")
        elif not result.get("error"):
            parts.append("（页面是空的或者拿不到。）")

    parts.append("</tool-result>")
    return "\n".join(parts)
