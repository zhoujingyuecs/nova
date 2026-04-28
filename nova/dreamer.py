"""Daydreamer：让 nova 在没有人说话的时候，自己想事情。

这是一个后台线程，每隔一段时间（带随机抖动），让 nova 走一次
dream_step。dream_step 自己会负责选种子、走水流、调用 LLM、给
缝隙刻形状、必要时伸手。

和"被人提问"在结构上是同一回事，区别只在：
  - 提示词不一样（这部分在 mind.py 的 dream_step 里）；
  - 生成长度更短，避免一段走神变成长篇内心独白；
  - 不打印到对话里——除非你监听 on_dream 回调。

★ 新版的 dream_step 偶尔会在 prompt 里塞一句"你也可以把心里的话
送到外面那个窗口"——这是给"自我对话"留的入口。daydreamer 自己
不直接干这件事，它只是按时让 nova 醒一下。

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
