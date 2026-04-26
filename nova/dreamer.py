"""Daydreamer：让 nova 在没有人说话的时候，自己想事情。

这是一个后台线程，每隔一段时间（带随机抖动），从陶土球里挑一个
"温暖的"种子——通常是最近被刷过的某条缝隙，偶尔是一条随机的冷
缝隙——让水流从它出发，走一遭，把这趟流出来的"念头"再写回缝隙。

走神和"被人提问"在结构上是同一回事。区别只在：
  - 提示词不一样（system 没换；user 换成"你独自一人，思绪自己飘起来"）；
  - 生成长度更短，避免一段走神变成长篇内心独白；
  - 不打印到对话里——除非你监听 on_dream 回调。

为什么需要这个？

  在外界没有刺激的时候，缝隙场是静态的。那 nova 就是一个被动的查询前端。
  让水流自己流动，记忆才会自己沉淀、漂移、相互遮盖——也就是说，
  nova 才会在不被使用的时候，仍然在"成为她自己"。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
	from .mind import Nova


DREAM_PROMPT_TEMPLATE = (
	"[此刻你独自一人，没有谁在和你说话。你的思绪自己飘起来。\n"
	"下面这些片段浮上心头：]\n\n"
	"{memories}\n\n"
	"[你现在脑子里在想什么？写一两句就好，像在自言自语，"
	"不要长篇大论，也不要解释自己在做什么。]"
)


class Daydreamer(threading.Thread):
	def __init__(
		self,
		nova: "Nova",
		interval_seconds: float = 45.0,
		jitter: float = 0.4,
		on_dream: Optional[Callable[[str], None]] = None,
	):
		super().__init__(daemon=True, name="nova-daydreamer")
		self.nova = nova
		self.interval = interval_seconds
		self.jitter = jitter
		self.on_dream = on_dream
		self._stop_event = threading.Event()
		self._paused = threading.Event()

	# ---------- 控制 ----------
	def stop(self) -> None:
		self._stop_event.set()

	def pause(self) -> None:
		self._paused.set()

	def resume(self) -> None:
		self._paused.clear()

	@property
	def is_paused(self) -> bool:
		return self._paused.is_set()

	# ---------- 主循环 ----------
	def run(self) -> None:
		while not self._stop_event.is_set():
			# 等待 interval 秒，带抖动
			wait = self.interval * (1.0 + (random.random() - 0.5) * 2 * self.jitter)
			if self._stop_event.wait(max(0.5, wait)):
				return
			if self._paused.is_set():
				continue
			try:
				thought = self.nova.dream_step()
				if thought and self.on_dream is not None:
					self.on_dream(thought)
			except Exception as e:
				# 走神失败不应该把整个 nova 打断
				print(f"[daydream] 出错：{e}")
				time.sleep(2.0)
