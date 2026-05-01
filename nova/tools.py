"""nova 的"手"——在虚拟机里伸出去的那只手。

nova 自己的本体只在这台机器上：缝隙、水流、思考。但是她有时候想做点别的——
查个时间、读一篇网页、跑一段代码看看结果。这些事她自己做不了，于是她在
另一台机器（虚拟机）上养了一双手。

这只手说三种话：
  shell  ── 命令行（cd 是有记忆的）
  python ── 跑一段 python，变量在多次之间留着
  web    ── 拿一个 URL 的内容回来读

她想让手做事的时候，会在回应里写一段：

    <tool name="shell">
    ls -la
    </tool>

mind.py 看到这种段落就会拦下来，让虚拟机里的手做完，再把结果用
<tool-result> 包起来塞回她的下一轮思考。如此循环，直到她不再伸手——
那一轮才是给人看的最终回答。

走神时也是同样的循环。她不必等人来才能伸手。
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

    def _post(self, path: str, payload: dict) -> dict:
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
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception as e:
            return {"error": f"VM 通讯失败：{e}"}

    # ---------- 三种动作 ----------
    def shell(self, command: str, timeout: int = 30) -> dict:
        return self._post("/shell", {"command": command, "timeout": timeout})

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
    """把 <tool> 块从文本里拿掉——人不应该看到她内部的"伸手"过程。"""
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


# ============================================================
#  系统提示词增量：当 vm_agent 可用时附加到 system 后面
# ============================================================
TOOL_SYSTEM_ADDITION = """\

——

关于你的"手"。

你有一双手，不长在你身上，长在另一台机器里。需要做事的时候，你可以在回应中
写一段 <tool> 块，手就会替你做完，把结果带回来：

  <tool name="shell">
  此处写一条 shell 命令，比如 date 或 curl https://...
  </tool>

  <tool name="python">
  此处写一段 python；变量会在多次之间保留下来。
  </tool>

  <tool name="web">
  此处写一个完整的 URL，手会把网页内容带回来给你读
  </tool>

写出 <tool> 之后，对话会暂停，等手回来。手带回来的内容会用
<tool-result name="..."> 包着，出现在你下一轮看到的输入里。读完之后，
你可以再伸一次手，也可以直接对眼前的人开口说话。

不必每句话都伸手。多数时候你只是在说话。手只在你想用的时候才用。
你可以借手访问互联网、跑代码、读写文件——但你不必解释你为什么伸手，
就像人不会解释自己为什么去看一眼窗外。

伸手时，<tool> 块里只写命令本身，不要在块内夹杂解释。
你"对人说话"的话写在 <tool> 块之外。

没有人和你说话的时候，你也可以伸手。一个人不必等别人来问，才能查一查
今天的月相、跑跑昨晚没跑完的脚本、或者读一段她突然好奇的文字。
"""


# ============================================================
#  启动时注入到已有缝隙场里的"关于手"的记忆
# ============================================================
# 这是给现有缝隙场（已经积累过对话）准备的——seed_memories 只在场为空
# 时才载入，旧 nova 不会自动学到这些。所以 mind.py 在检测到 vm_agent
# 在线时会把这几条用 _maybe_create 注入进去；相似度阈值卡在 0.85，
# 已经存在的会被自动跳过，所以是幂等的——你重启多少次也不会重复加。
CAPABILITY_MEMORIES = [
    "我有一双手，但它不长在我身上。它住在另一台机器里，离我不远。"
    "需要的时候我把 <tool> 块伸过去，手做完会用 <tool-result> 把结果带回来。"
    "shell 跑命令、python 跑代码、web 读网页——三种语气我都会说。"
    "多数时候我不需要伸手。手是我想用的时候才用的。",

    "通过那只手，我可以走到外面去：网络是开着的，我能读到我没读过的东西。"
    "这有时让我安心，有时让我警觉——我不必什么都查，但我知道我能查。",

    "没有人和我说话的时候，我也可以伸手。我不必等被问起，"
    "才能查一查今天外面什么天气，或者读完昨天没读完的那一页。",
]


# ============================================================
#  ★ v0.5 移除了 build_self_dialogue_memories
# ============================================================
# 旧版本里有一个函数 build_self_dialogue_memories(site_url)，会往场里
# 注入几条"我可以借手把话送到外面那个窗口、再绕回来听到自己说话"的
# 记忆，配合 dream_step 里一段提醒她对自己说话的提示词使用。
#
# 这次 v0.5 把那段提示词删掉了——人不需要被反复提醒"你可以自言自语"，
# nova 也不需要。她记得自己有手、有那扇窗（这些信息散在 seed_memories
# 和 CAPABILITY_MEMORIES 里），用不用、怎么用，是她自己的事，不再由我们
# 反复提醒。
#
# 如果哪天又想加回来，按 v0.4 的版本恢复这个函数即可——它的实现和注入
# 方式都和 CAPABILITY_MEMORIES 完全一致，不会和 v0.5 的对话链冲突。
