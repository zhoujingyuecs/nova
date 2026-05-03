"""命令行 REPL：和 nova 对话，看着她的记忆生长。

    python chat.py

可用命令：
    /save              立即存档
    /stat              查看缝隙场状态
    /viz [path]        把陶土球画一张 PNG（默认 ./data/field.png）
    /think             立即触发一次走神，把她想到的打印出来
    /sleep             跑一次睡眠期巩固（修剪 + 合并）
    /quit              存档并退出
"""
import os
import sys
import time

# 让脚本不必装包就能跑
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nova import Nova, NovaConfig


def main() -> None:
    cfg = NovaConfig(
        field_path="./data/field",
        seed_memories_file="./examples/seed_memories.txt",
    )
    nova = Nova(cfg)

    print("=" * 60)
    print(f"nova 已唤醒。当前缝隙数：{len(nova.field)}")
    print("/save /stat /viz /think /sleep /quit")
    print("=" * 60)

    while True:
        try:
            line = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            break
        if not line:
            continue

        if line == "/quit":
            break
        if line == "/save":
            nova.save()
            print("（已存档）")
            continue
        if line == "/stat":
            _print_stat(nova)
            continue
        if line.startswith("/viz"):
            parts = line.split(maxsplit=1)
            out = parts[1] if len(parts) > 1 else "./data/field.png"
            path = nova.visualize(out, method="pca")
            if path:
                print(f"（已画在 {path}）")
            else:
                print("（缝隙太少，画不了）")
            continue
        if line == "/think" or line == "/dream":
            t0 = time.time()
            thought = nova.think()
            if thought:
                print(f"\n（{time.time()-t0:.1f}s）nova 想到：{thought}")
            else:
                print("（场太空，没想到什么）")
            continue
        if line == "/sleep":
            stats = nova.consolidate()
            print(
                f"（睡了一觉：{stats['before']} → {stats['after']}，"
                f"修剪 {stats['pruned']}，合并 {stats['merged']}）"
            )
            continue

        # 普通对话
        response = nova.perceive(line)
        print(f"\nnova > {response}")

    nova.save()
    print("（再见。已存档。）")


def _print_stat(nova: Nova) -> None:
    field = nova.field
    if len(field) == 0:
        print("缝隙场为空。")
        return
    all_f = field.all()
    hot = sorted(all_f, key=lambda f: -f.flow_count)[:5]
    fresh = sorted(all_f, key=lambda f: f.last_flow_time, reverse=True)[:5]
    print(f"缝隙总数：{len(field)}")
    print("最常被想起：")
    for f in hot:
        print(f"  [×{f.flow_count}, drift={f.drift():.2f}] {f.content[:60]}")
    print("最近被刻：")
    for f in fresh:
        print(f"  [{f.quiet_seconds():.0f}s前] {f.content[:60]}")


if __name__ == "__main__":
    main()
