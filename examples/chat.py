"""命令行 REPL：和 nova 对话，看着她的记忆生长。

	python examples/chat.py

可用命令：
	/save              立即存档
	/stat              查看缝隙场状态
	/viz [path]        把陶土球画一张 PNG（默认 ./data/field.png）
	/dream             立即触发一次走神，把她想到的打印出来
	/dream-on          打开后台走神线程
	/dream-off         关闭后台走神线程
	/sleep             跑一次睡眠期巩固（修剪 + 合并）
	/quit              存档并退出
"""

import os
import sys
import time
from threading import Lock

# 让脚本不必装包就能跑
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nova import Daydreamer, Nova, NovaConfig

print_lock = Lock()


def _print(text: str, end: str = "\n") -> None:
	with print_lock:
		print(text, end=end, flush=True)


def main() -> None:
	cfg = NovaConfig(
		field_path="./data/field",
		seed_memories_file="./examples/seed_memories.txt",
	)
	nova = Nova(cfg)

	# 走神回调：浮起念头时打印出来
	def on_dream(thought: str) -> None:
		_print(f"\n（nova 在出神：{thought}）\n你 > ", end="")

	dreamer = Daydreamer(
		nova,
		interval_seconds=cfg.daydream_interval_seconds,
		jitter=cfg.daydream_jitter,
		on_dream=on_dream,
	)

	_print("=" * 60)
	_print(f"nova 已唤醒。当前缝隙数：{len(nova.field)}")
	_print("/save /stat /viz /dream /dream-on /dream-off /sleep /quit")
	_print("=" * 60)

	while True:
		try:
			line = input("\n你 > ").strip()
		except (EOFError, KeyboardInterrupt):
			_print("")
			break
		if not line:
			continue

		# ---------- 命令 ----------
		if line == "/quit":
			break
		if line == "/save":
			nova.save()
			_print("（已存档）")
			continue
		if line == "/stat":
			_print_stat(nova)
			continue
		if line.startswith("/viz"):
			parts = line.split(maxsplit=1)
			out = parts[1] if len(parts) > 1 else "./data/field.png"
			path = nova.visualize(out, method="pca")
			if path:
				_print(f"（已画在 {path}）")
			else:
				_print("（缝隙太少，画不了）")
			continue
		if line == "/dream":
			t0 = time.time()
			thought = nova.dream_step()
			if thought:
				_print(f"\n（{time.time()-t0:.1f}s）nova 想到：{thought}")
			else:
				_print("（场太空，没想到什么）")
			continue
		if line == "/dream-on":
			if not dreamer.is_alive():
				dreamer.start()
				_print("（后台走神线程已开启）")
			else:
				dreamer.resume()
				_print("（恢复走神）")
			continue
		if line == "/dream-off":
			dreamer.pause()
			_print("（暂停走神）")
			continue
		if line == "/sleep":
			stats = nova.consolidate()
			_print(
				f"（睡了一觉：{stats['before']} → {stats['after']}，"
				f"修剪 {stats['pruned']}，合并 {stats['merged']}）"
			)
			continue

		# ---------- 普通对话 ----------
		response = nova.perceive(line)
		_print(f"\nnova > {response}")

	# 退出
	if dreamer.is_alive():
		dreamer.stop()
		dreamer.join(timeout=2.0)
	nova.save()
	_print("（再见。已存档。）")


def _print_stat(nova: Nova) -> None:
	field = nova.field
	if len(field) == 0:
		_print("缝隙场为空。")
		return
	all_f = field.all()
	hot = sorted(all_f, key=lambda f: -f.flow_count)[:5]
	fresh = sorted(all_f, key=lambda f: f.last_flow_time, reverse=True)[:5]
	_print(f"缝隙总数：{len(field)}")
	_print("最常被想起：")
	for f in hot:
		_print(f"  [×{f.flow_count}, drift={f.drift():.2f}] {f.content[:60]}")
	_print("最近被刻：")
	for f in fresh:
		_print(f"  [{f.quiet_seconds():.0f}s前] {f.content[:60]}")


if __name__ == "__main__":
	main()
