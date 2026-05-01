"""笔记本（NotesBook）：nova "确认知道的事"。

==================================================================
为什么单独搞一个笔记本？—— 缝隙场不够用
==================================================================

之前的版本里，nova 所有"非当下"的信息都堆在缝隙场（FissureField）里。
你教她一套操作步骤，那段对话作为带 [有人对我说] 标签的缝隙存进去，
下次要用的时候——能不能想起来，全靠水流碰巧刷过那条缝隙。这是
**回忆机制**，不是**学习机制**。

人脑里这两件事其实分开：
  • 回忆：模糊的、漂移的、按相似度浮起来的片段——"啊好像那次他
    跟我提过这事来着"
  • 知识：明确的、可调用的、心里能"一二三"列出来的事——"豆包工
    具的用法是 A→B→C；用户的名字是周；豆包响应慢是正常的不是超时"

nova 之前只有缝隙场对应"回忆"。学到的东西哪怕被反复教，也只是在
缝隙场里多积几条带相似文本的缝隙——没有提炼、没有结构、没有索引。
所以哪怕你教了 5 次同样的步骤，nova 第 6 次仍然不会执行——因为她
从来没有"我知道这件事"那个状态，只有"我隐约记得有人提过这事"。

笔记本就是补这个：

  • **每条笔记是一句明确的"我知道..."**（学到的步骤 / 重要事实 /
    被纠正的误解 / 长期偏好）
  • **永远在 prompt 里**，不靠水流、不靠运气
  • 跨 episode、跨重启都保留——这是稳定的"她已经会的事"
  • 通过显式的 ADD / UPDATE / REMOVE 动作维护——LLM 不能擅自改写
    所有内容，只能按动作动一条
  • **保守地添加**——只在真有沉淀价值时才记，避免笔记本被情绪
    和场景细节污染

每次 perceive 完一句话之后，nova 会用一次额外的 LLM 调用，看刚才那
段对话里有没有要记进笔记本的——这个"消化沉淀"的动作类似一个人结束
一段对话后，心里悄悄把"我刚学到了 X" 记一笔。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Note:
	"""笔记本里的一条笔记。

	id 用 'n_xxxxxx' 短前缀，方便 LLM 在 UPDATE / REMOVE 时引用。
	"""
	id: str = field(default_factory=lambda: "n_" + uuid.uuid4().hex[:6])
	content: str = ""
	created_at: float = field(default_factory=time.time)
	updated_at: float = field(default_factory=time.time)
	# referenced_count 是预留的——以后想做"长期没用过的笔记淡出"时会用上
	referenced_count: int = 0

	def to_dict(self) -> dict:
		return {
			"id": self.id,
			"content": self.content,
			"created_at": self.created_at,
			"updated_at": self.updated_at,
			"referenced_count": self.referenced_count,
		}

	@classmethod
	def from_dict(cls, d: dict) -> "Note":
		return cls(
			id=d.get("id") or ("n_" + uuid.uuid4().hex[:6]),
			content=(d.get("content") or "").strip(),
			created_at=float(d.get("created_at", time.time())),
			updated_at=float(d.get("updated_at", time.time())),
			referenced_count=int(d.get("referenced_count", 0) or 0),
		)


class NotesBook:
	"""nova 的笔记本——稳定、明确、永远在 prompt 里。"""

	def __init__(self, path: Optional[str] = None,
				 max_total: int = 200,
				 max_chars_per_note: int = 200):
		self._notes: dict[str, Note] = {}
		self._order: list[str] = []   # 按创建时间顺序（老→新）
		self.path = path
		self.max_total = max_total
		self.max_chars_per_note = max_chars_per_note

	def __len__(self) -> int:
		return len(self._notes)

	def __iter__(self):
		return iter(self.all())

	# ==========================================================
	#                    动作：增 / 改 / 删
	# ==========================================================
	def add(self, content: str) -> Optional[Note]:
		"""加一条笔记。返回新建的 Note，重复或空内容时返回 None。"""
		content = self._normalize_content(content)
		if not content:
			return None
		# 防完全重复（去除空白、忽略大小写后）
		key = self._dedup_key(content)
		for existing in self._notes.values():
			if self._dedup_key(existing.content) == key:
				return None
		# 容量上限：超出时把最早的、近期没被引用的笔记移出
		if len(self._notes) >= self.max_total:
			self._evict_oldest()
		n = Note(content=content)
		# 保证 id 唯一
		while n.id in self._notes:
			n.id = "n_" + uuid.uuid4().hex[:6]
		self._notes[n.id] = n
		self._order.append(n.id)
		return n

	def update(self, note_id: str, new_content: str) -> bool:
		"""改一条笔记的内容。id 不存在或新内容为空时返回 False。"""
		n = self._notes.get(note_id)
		if n is None:
			return False
		new_content = self._normalize_content(new_content)
		if not new_content:
			return False
		# 如果改完和别的笔记完全一样，等价于"合并"——删掉这条就行了
		key = self._dedup_key(new_content)
		for existing in self._notes.values():
			if existing.id == note_id:
				continue
			if self._dedup_key(existing.content) == key:
				# 已有同样内容的笔记，把当前这条删掉即可
				return self.remove(note_id)
		n.content = new_content
		n.updated_at = time.time()
		return True

	def remove(self, note_id: str) -> bool:
		if note_id not in self._notes:
			return False
		del self._notes[note_id]
		try:
			self._order.remove(note_id)
		except ValueError:
			pass
		return True

	def touch(self, note_id: str) -> None:
		"""标记一条笔记被引用过一次（暂未在主流程里调用，预留）。"""
		n = self._notes.get(note_id)
		if n is not None:
			n.referenced_count += 1

	def _evict_oldest(self) -> None:
		"""容量满时丢一条最老的——按 (referenced_count 升序, created_at 升序) 排。"""
		if not self._order:
			return
		def key(nid):
			n = self._notes[nid]
			return (n.referenced_count, n.created_at)
		victim = min(self._order, key=key)
		self.remove(victim)

	# ==========================================================
	#                     查询 / 渲染
	# ==========================================================
	def all(self) -> list[Note]:
		return [self._notes[i] for i in self._order if i in self._notes]

	def get(self, note_id: str) -> Optional[Note]:
		return self._notes.get(note_id)

	def render_for_prompt(self, max_chars: int = 1500) -> str:
		"""渲染笔记本到 prompt 里的一段文字。空时返回 ""。

		按创建时间从老到新——老的更基础（"我会用什么工具"），新的更近因
		（"刚被纠正的误解"）。如果总长度超过 max_chars，截断并提示。
		"""
		items = self.all()
		if not items:
			return ""
		lines = []
		used = 0
		for n in items:
			line = f"  • [{n.id}] {n.content}"
			if used + len(line) + 1 > max_chars and lines:
				# 留个 hint 告诉 nova "还有更多笔记"
				remaining = len(items) - len(lines)
				lines.append(
					f"  …（还有 {remaining} 条更早的笔记，限于篇幅未列出。"
					f"睡眠期会做整理。）"
				)
				break
			lines.append(line)
			used += len(line) + 1
		return "\n".join(lines)

	def render_for_update_prompt(self, max_chars: int = 2000) -> str:
		"""渲染笔记本——给"更新笔记本"那次 LLM 调用看的版本。

		和 render_for_prompt 类似，但内容更紧凑、id 更显眼，方便 LLM 在
		UPDATE / REMOVE 时引用。
		"""
		items = self.all()
		if not items:
			return "（笔记本现在是空的。）"
		lines = []
		used = 0
		for n in items:
			line = f"[{n.id}] {n.content}"
			if used + len(line) + 1 > max_chars and lines:
				lines.append(f"…（还有 {len(items) - len(lines)} 条更早的笔记未列）")
				break
			lines.append(line)
			used += len(line) + 1
		return "\n".join(lines)

	# ==========================================================
	#                     持久化
	# ==========================================================
	def save(self, path: Optional[str] = None) -> None:
		path = path or self.path
		if not path:
			return
		try:
			d = os.path.dirname(path)
			if d:
				os.makedirs(d, exist_ok=True)
			payload = {
				"notes": [
					self._notes[i].to_dict()
					for i in self._order if i in self._notes
				],
				"saved_at": time.time(),
				"version": 1,
			}
			tmp = path + ".tmp"
			with open(tmp, "w", encoding="utf-8") as f:
				json.dump(payload, f, ensure_ascii=False, indent=2)
			os.replace(tmp, path)
		except Exception as e:
			print(f"⚠️ 笔记本落盘失败：{e}")

	def load(self, path: Optional[str] = None) -> None:
		path = path or self.path
		if not path or not os.path.exists(path):
			return
		try:
			with open(path, "r", encoding="utf-8") as f:
				d = json.load(f)
		except Exception as e:
			print(f"⚠️ 笔记本读取失败（忽略，从空白开始）：{e}")
			return
		notes_list = d.get("notes", []) or []
		self._notes = {}
		self._order = []
		for raw in notes_list:
			try:
				n = Note.from_dict(raw)
			except Exception:
				continue
			if not n.content.strip():
				continue
			if n.id in self._notes:
				# id 撞了，换一个
				n.id = "n_" + uuid.uuid4().hex[:6]
				while n.id in self._notes:
					n.id = "n_" + uuid.uuid4().hex[:6]
			self._notes[n.id] = n
			self._order.append(n.id)
		if self._notes:
			print(f"📓 笔记本恢复：{len(self._notes)} 条")

	# ==========================================================
	#                     内部辅助
	# ==========================================================
	def _normalize_content(self, s: str) -> str:
		s = (s or "").strip()
		# 去掉一些常见的"格式垃圾"——LLM 偶尔会带 markdown 列表前缀
		import re
		s = re.sub(r"^\s*(?:[-*·•]|\d+[\.\、\)）])\s*", "", s)
		# 去掉 LLM 偶尔把整句包在引号里的情况
		quote_pairs = [('"', '"'), ("'", "'"), ("「", "」"),
					   ("\u201c", "\u201d"), ("\u2018", "\u2019"), ("《", "》")]
		for lq, rq in quote_pairs:
			if s.startswith(lq) and s.endswith(rq) and len(s) > 2:
				s = s[len(lq):-len(rq)].strip()
				break
		# 长度上限
		if len(s) > self.max_chars_per_note:
			s = s[:self.max_chars_per_note].rstrip() + "…"
		return s

	@staticmethod
	def _dedup_key(s: str) -> str:
		"""去重比较时用的归一化 key——忽略大小写、空白和尾部省略号。"""
		s = (s or "").strip().lower()
		s = "".join(s.split())
		s = s.rstrip("…").rstrip(".")
		return s
