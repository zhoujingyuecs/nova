"""启动 nova ContinuousRuntime。

用法：
    python run_continuous.py
    python run_continuous.py --commission "重写 README，让外部读者理解 nova"

不传 commission 时，nova 会先进入 self_orientation：
她不是被启动参数指定任务，而是在运行中根据记忆、能力、最近工作和工作区里
留下来的笔记，自己生成主线。

交互命令：
    /status          查看当前主线、SelfState、最近工作
    /work [n]        最近 n 条工作日志
    /agenda          查看 agenda
    /commission 标题 给 nova 一个外部委托
    /add 标题        新增主线（兼容命令）
    /sleep           立刻触发睡眠整理
    /quit            保存并退出

普通输入会作为"外部打断"交给 nova；她回应后 runtime 会继续自己的主线。
"""
from __future__ import annotations

import argparse
import sys

from nova import Nova
from nova.runtime import ContinuousRuntime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commission", default="",
                        help="启动时给 nova 的外部委托；不传则由 nova 自我取向")
    parser.add_argument("--agenda", default="",
                        help="兼容旧参数：等同于 --commission")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="有主线时每个 tick 的间隔秒数")
    parser.add_argument("--idle-interval", type=float, default=30.0,
                        help="无主线时走神间隔秒数")
    args = parser.parse_args()

    nova = Nova()
    runtime = ContinuousRuntime(
        nova,
        interval_seconds=args.interval,
        idle_interval_seconds=args.idle_interval,
        initial_commission=args.commission or args.agenda or None,
    )
    runtime.start()

    print("=" * 72)
    print("nova continuous runtime 已启动。")
    print("普通输入 = 打断 nova；/status /work /agenda /commission /sleep /quit")
    print("=" * 72)

    try:
        while True:
            try:
                line = input("你 > ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line in {"/quit", "/exit"}:
                break
            if line == "/status":
                print(runtime.status_text())
                continue
            if line.startswith("/work"):
                parts = line.split(maxsplit=1)
                n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 12
                print(runtime.worklog.summary_text(limit=n))
                continue
            if line == "/agenda":
                print(runtime.agenda.summary_text(limit=20))
                continue
            if line.startswith("/commission "):
                title = line[len("/commission "):].strip()
                item = runtime.add_agenda(title, source="commission", priority=0.8)
                print(f"已接收外部委托：{item.title} (id={item.id})")
                continue
            if line.startswith("/add "):
                title = line[5:].strip()
                item = runtime.add_agenda(title, source="user", priority=0.8)
                print(f"已加入主线：{item.title} (id={item.id})")
                continue
            if line == "/sleep":
                runtime._sleep_step("manual command")  # noqa: SLF001
                print("已触发睡眠整理。")
                continue

            response = runtime.submit_interrupt(line, wait=True, timeout=None)
            print(f"nova > {response}\n")
    finally:
        print("正在保存并停止 runtime……")
        runtime.stop()
        runtime.join(timeout=10)
        print("已停止。")


if __name__ == "__main__":
    main()
