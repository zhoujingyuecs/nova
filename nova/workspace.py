"""Workspace：nova 的外部记事本与脚本箱。

老版本里 nova 把"她确认知道的事"塞在 NotesBook（JSON 文件 + LLM
update 调用），把"她学过的步骤"塞在 SkillBook（另一个 JSON），把
"她内部记的工具用法"还混在缝隙场里。结果是：每次 perceive 都要多
做几次 LLM 调用、prompt 顶上挂一长串笔记块，但 nova 仍然不会按需
查阅它们——因为这些"内部记忆"本质上还是被压缩到 prompt 里、靠水
流碰运气。

v1.0 的方向反过来：nova 自己脑子里只放裂缝场和当下意识；笔记、步骤、
脚本、日志，全部以普通文本文件的形式住在虚拟机的工作区里。

  ~/nova_workspace/
    notes/      长期事实、操作步骤、被纠正的误解、对人的理解。每条 .md
    scripts/    她写过、用过的可重复脚本
    journal/    每日日志
    INDEX.md    顶层索引——什么大类的东西在哪个目录里

她需要查 / 写 / 跑什么的时候伸手即可：

    <tool name="shell">ls ~/nova_workspace/notes</tool>
    <tool name="shell">cat ~/nova_workspace/notes/about_zhou.md</tool>
    <tool name="shell">grep -ril 'doubao' ~/nova_workspace</tool>
    <tool name="shell">cat &gt; ~/nova_workspace/notes/2026-04-30_xxx.md &lt;&lt;'EOF'
    ...
    EOF</tool>

这个模块只负责两件简单的事：
  1. 启动时确保工作区目录存在、写好 INDEX.md 骨架。
  2. 维护一份"工作区里有什么"的轻量索引（缓存几分钟），插到每次
     perceive 的 prompt 里，让 nova 看到她已有的笔记/脚本，不必每
     次都先 ls。

注意：所有 IO 都通过 VMAgent 走，这只手如果不在线，Workspace 就静默
返回空索引——它不假装自己能干活。
"""
from __future__ import annotations

import time
from typing import Optional

from .tools import VMAgent


WORKSPACE_BOOTSTRAP_SCRIPT = r"""set -e
ROOT="{root}"
mkdir -p "$ROOT/notes" "$ROOT/scripts" "$ROOT/journal"
INDEX="$ROOT/INDEX.md"
if [ ! -f "$INDEX" ]; then
cat > "$INDEX" <<'NOVA_EOF'
# nova 工作区

这是我（nova）的外部记事本和工具箱。

我的脑子里只放裂缝场的形状和当下意识；具体的事实、步骤、脚本都写在这里。
人记不住所有细节是正常的——我只需要记得"这种事我写在哪里了"。

## 目录约定

- `notes/`：长期事实、操作步骤、被纠正的误解、对人的理解。
  每条一篇 .md，文件名形如 `YYYY-MM-DD_主题.md`。写得短、具体。
- `scripts/`：我写过、用过的可重复脚本。下次直接 `python3 路径` 跑。
  顶部写一句注释说明用途。
- `journal/`：我想留下来的日志，按日期一篇 `YYYY-MM-DD.md`。

## 我自己维护这份 INDEX

写了一篇笔记或脚本之后，最好顺手在这里加一行索引，方便以后 grep。

## 我已经记住的（请按时间倒序）

NOVA_EOF
fi
echo "OK"
"""


class Workspace:
    """nova 在虚拟机里的外部工作区。"""

    def __init__(
        self,
        vm_agent: Optional[VMAgent],
        root: str = "~/nova_workspace",
        *,
        index_ttl: float = 600.0,
        index_max_chars: int = 1200,
    ):
        self.vm = vm_agent
        self.root = root
        self.index_ttl = index_ttl
        self.index_max_chars = index_max_chars
        self._cached_index: str = ""
        self._cached_at: float = 0.0
        self._bootstrap_done: bool = False

    @property
    def is_available(self) -> bool:
        return self.vm is not None

    # ------------------------------------------------------------------
    # bootstrap
    # ------------------------------------------------------------------
    def ensure_bootstrap(self) -> bool:
        """启动时调用。确保目录存在、INDEX.md 有骨架。

        返回是否成功。失败不致命，只是 Workspace 暂时不可用。"""
        if self._bootstrap_done or self.vm is None:
            return self._bootstrap_done
        cmd = WORKSPACE_BOOTSTRAP_SCRIPT.format(root=self.root)
        try:
            result = self.vm.shell(cmd, timeout=20)
        except Exception as e:
            print(f"⚠️ workspace bootstrap 失败：{e}")
            return False
        if result.get("error"):
            print(f"⚠️ workspace bootstrap shell 错误：{result.get('error')}")
            return False
        if (result.get("returncode") or 0) != 0:
            print(f"⚠️ workspace bootstrap 退出码：{result.get('returncode')}, stderr={result.get('stderr')}")
            return False
        self._bootstrap_done = True
        print(f"📁 工作区已就绪：{self.root}")
        return True

    def invalidate(self) -> None:
        """通知索引缓存失效（写入文件之后调用）。下次 render 会重拉。"""
        self._cached_at = 0.0

    # ------------------------------------------------------------------
    # index 渲染（给 perceive 用）
    # ------------------------------------------------------------------
    def render_for_prompt(self, max_chars: Optional[int] = None) -> str:
        """渲染工作区索引到 prompt 里的一段文字。

        包含：INDEX.md 顶部一段、最近被改的笔记/脚本/日志列表。
        缓存 ttl 秒；缓存命中时不发任何 VM 请求。
        """
        if self.vm is None:
            return ""
        if not self._bootstrap_done:
            # 没初始化过就先初始化；失败就静默返回空
            if not self.ensure_bootstrap():
                return ""

        max_chars = max_chars or self.index_max_chars
        now = time.time()
        if self._cached_index and (now - self._cached_at) < self.index_ttl:
            return self._truncate(self._cached_index, max_chars)

        text = self._refresh_index()
        self._cached_index = text
        self._cached_at = now
        return self._truncate(text, max_chars)

    def _refresh_index(self) -> str:
        cmd = (
            f'set -e; cd {self.root} 2>/dev/null && '
            'echo "===INDEX_HEAD==="; '
            'head -40 INDEX.md 2>/dev/null || true; '
            'echo "===NOTES==="; '
            'ls -t notes/*.md 2>/dev/null | head -10 || true; '
            'echo "===SCRIPTS==="; '
            'ls -t scripts/*.py scripts/*.sh 2>/dev/null | head -10 || true; '
            'echo "===JOURNAL==="; '
            'ls -t journal/*.md 2>/dev/null | head -5 || true'
        )
        try:
            result = self.vm.shell(cmd, timeout=15)
        except Exception as e:
            return f"[工作区索引获取失败：{e}]"
        out = (result.get("stdout") or "").strip()
        if not out:
            return ""
        return self._format_index_output(out)

    def _format_index_output(self, raw: str) -> str:
        sections = {"head": [], "notes": [], "scripts": [], "journal": []}
        cur = "head"
        for line in raw.splitlines():
            line = line.rstrip()
            if line == "===INDEX_HEAD===":
                cur = "head"; continue
            if line == "===NOTES===":
                cur = "notes"; continue
            if line == "===SCRIPTS===":
                cur = "scripts"; continue
            if line == "===JOURNAL===":
                cur = "journal"; continue
            sections[cur].append(line)

        lines = [
            f"[你的工作区：{self.root}]",
            "（以下是已经存在的笔记/脚本/日志。要查内容用 cat，要改用 cat>>，要列更多用 ls。）",
        ]
        head = [l for l in sections["head"] if l.strip()]
        if head:
            lines.append("\n--- INDEX.md 头部 ---")
            lines.extend(head[:20])

        if sections["notes"]:
            lines.append("\n--- 最近笔记 (notes/) ---")
            for l in sections["notes"][:10]:
                if l.strip():
                    lines.append(f"  {l.strip()}")

        if sections["scripts"]:
            lines.append("\n--- 已有脚本 (scripts/) ---")
            for l in sections["scripts"][:10]:
                if l.strip():
                    lines.append(f"  {l.strip()}")

        if sections["journal"]:
            lines.append("\n--- 最近日志 (journal/) ---")
            for l in sections["journal"][:5]:
                if l.strip():
                    lines.append(f"  {l.strip()}")

        return "\n".join(lines)

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n…（工作区内容已截断；nova 可以伸手查看更多）"
