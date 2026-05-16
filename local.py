# coding=UTF-8
"""local.py —— 本地 nova 节点入口（持续运行 + page 中继 + swarm 联结）

启动时唤醒 Nova，立即跑 ContinuousRuntime（nova 持续生活）；
同时用 socketio.Client 连云端 page.py。这条 socket 同时承担两件事：

  1) 旧的访客对话中继（v1.3.1 起的 chat_result / new_chat_task）
  2) 新的 swarm 协议（v1.4 起的 swarm_* 事件）——把这台机器作为一个
     **node** 接入 swarm，与其它物理机的 nova 联合。

跨物理机运行：
  机器 A、B、C 各自跑 local.py，都连同一个 page.py。
  page.py 内部的 SwarmHub 中继它们之间的消息——它们就形成了一个
  swarm。每个 node 仍然是独立的 nova，但**共享目标、共享部分记忆、
  通过仲裁形成行动、通过回忆形成连续性**。

运行：
    python local.py
    python local.py --commission "重写 README，让外部读者理解 nova"
    python local.py --cloud http://your-host:8080
    python local.py --no-cloud           # 只跑 runtime，不连 page
    python local.py --no-swarm           # 连 page 但不入 swarm（v1.3.1 行为）
    python local.py --node-name 白烬·北京

不传 --commission 时，nova 会先 self_orientation，自己生成主线。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import traceback

import socketio

from nova import Nova, NovaConfig
from nova.runtime import ContinuousRuntime
from nova.swarm import (
    NodeProfile, PROTOCOL_VERSION as SWARM_PROTOCOL_VERSION,
    derive_default_node_id, derive_default_node_name,
)
from nova.swarm_link import SwarmLink
from nova.swarm_integration import SwarmAdapter


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="nova 本地节点（持续运行 + 连 page + swarm）")
    p.add_argument(
        "--cloud",
        default=os.environ.get("NOVA_CLOUD_URL", "http://127.0.0.1:8080"),
        help="云端 page.py 的 URL；也可用 NOVA_CLOUD_URL 环境变量",
    )
    p.add_argument(
        "--commission",
        default="",
        help="启动时给 nova 的外部委托；不传则她自己 self_orientation",
    )
    p.add_argument(
        "--field-path",
        default="./data/field",
        help="缝隙场和 runtime 状态的落盘目录",
    )
    p.add_argument(
        "--seed",
        default="./examples/seed_memories.txt",
        help="种子记忆文件；不存在时跳过",
    )
    p.add_argument(
        "--task-timeout",
        type=float,
        default=1200.0,
        help="单次外部打断的最长等待秒数",
    )
    p.add_argument(
        "--no-cloud",
        action="store_true",
        help="只跑 ContinuousRuntime，不连 page（本地内省模式）",
    )
    p.add_argument(
        "--no-swarm",
        action="store_true",
        help="连 page 但不加入 swarm（v1.3.1 行为，单节点）",
    )
    p.add_argument(
        "--node-name",
        default=os.environ.get("NOVA_NODE_NAME", ""),
        help="本节点在 swarm 里的可读名（默认从 hostname 推导）",
    )
    p.add_argument(
        "--swarm-id",
        default=os.environ.get("NOVA_SWARM_ID", "default"),
        help="加入哪个 swarm（同一台 page 上可以承载多个）",
    )
    return p.parse_args()


def boot_runtime(args: argparse.Namespace) -> ContinuousRuntime:
    print("⏳ 正在唤醒 nova，请稍候……")
    seed = args.seed if args.seed and os.path.exists(args.seed) else None
    cfg = NovaConfig(
        field_path=args.field_path,
        seed_memories_file=seed,
    )
    # 命令行覆盖：swarm 名 / id
    if args.node_name:
        cfg.swarm_node_name = args.node_name
    if args.swarm_id:
        cfg.swarm_id = args.swarm_id
    if args.no_swarm or args.no_cloud:
        cfg.swarm_enabled = False
    nova = Nova(cfg)
    print(f"✅ nova 已醒。当前缝隙数：{len(nova.field)}")

    runtime = ContinuousRuntime(
        nova,
        initial_commission=args.commission or None,
    )
    runtime.start()
    print("🌀 ContinuousRuntime 已启动。nova 正在自己生活。")
    return runtime


def build_node_profile(cfg: NovaConfig) -> NodeProfile:
    node_id = (cfg.swarm_node_id or "").strip() \
        or derive_default_node_id(cfg.field_path)
    node_name = (cfg.swarm_node_name or "").strip() \
        or derive_default_node_name()
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    return NodeProfile(
        node_id=node_id,
        node_name=node_name,
        swarm_id=cfg.swarm_id or "default",
        hostname=hostname,
        version=SWARM_PROTOCOL_VERSION,
        embedding_model=cfg.embedding_model,
        embedding_dim=0,           # 真实值由 Embedder 决定，会在 attach 时填上
        backend=cfg.llm_backend,
    )


def build_socket_client(runtime: ContinuousRuntime,
                        task_timeout: float) -> socketio.Client:
    sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=2,
        reconnection_delay_max=30,
        randomization_factor=0.5,
    )

    nova = runtime.nova
    cfg = nova.cfg

    # v1.4：装配 swarm 链路（如果 cfg.swarm_enabled）
    swarm_link = None
    if getattr(cfg, "swarm_enabled", True):
        profile = build_node_profile(cfg)
        profile.embedding_dim = int(getattr(nova.embedder, "dim", 0))
        swarm_link = SwarmLink(sio, profile)
        swarm_link.bind()
        adapter = SwarmAdapter(nova, swarm_link)
        nova.swarm = adapter
        print(
            f"🌌 swarm 装配完成：节点名 {profile.node_name}"
            f" id={profile.node_id} swarm_id={profile.swarm_id}"
        )
    else:
        print("（已禁用 swarm，按 v1.3.1 单节点形态运行）")

    @sio.event
    def connect() -> None:
        print(f"🔗 已连接云端 page")
        if swarm_link is None:
            return

        def _delayed_hello():
            # 等握手稳定再 emit;不然 sio.connected 可能还是 False
            time.sleep(0.5)
            try:
                ok = swarm_link.hello()
                print(f"🌌 swarm hello 已发送(ok={ok})")
            except Exception as e:
                print(f"⚠️ swarm hello 失败:{e}")
            try:
                adapter = nova.swarm
                if adapter is not None:
                    adapter.send_heartbeat(force=True)
            except Exception as e:
                print(f"⚠️ swarm heartbeat 失败:{e}")

        threading.Thread(target=_delayed_hello, daemon=True,
                         name="swarm-initial-hello").start()

    @sio.event
    def connect_error(data) -> None:
        print(f"❌ 连接云端失败：{data}")

    @sio.event
    def disconnect() -> None:
        print("⚪ 与云端断开（socketio 会自动重连）")

    @sio.on("connect_success")
    def on_connect_success(data) -> None:
        print(
            f"📢 云端欢迎：{data.get('msg')} | 客户端 ID：{data.get('client_id')}"
        )

    @sio.on("new_chat_task")
    def on_new_chat_task(task_data) -> None:
        threading.Thread(
            target=_process_chat_task,
            args=(sio, runtime, task_data, task_timeout),
            daemon=True,
            name="nova-interrupt-worker",
        ).start()

    @sio.on("status_request")
    def on_status_request(data) -> None:
        try:
            payload = {
                "request_id": (data or {}).get("request_id", ""),
                "ok": True,
                "status": runtime.status(),
                "status_text": runtime.status_text(),
            }
        except Exception as e:
            payload = {
                "request_id": (data or {}).get("request_id", ""),
                "ok": False,
                "error": str(e),
            }
        try:
            sio.emit("status_response", payload)
        except Exception as e:
            print(f"⚠️ status_response 发送失败：{e}")

    return sio


def _process_chat_task(
    sio: socketio.Client,
    runtime: ContinuousRuntime,
    task_data: dict,
    task_timeout: float,
) -> None:
    chat_id = task_data.get("id", "")
    user_input = (task_data.get("input") or "").strip()
    print(f"📥 收到 page 派发的对话任务：{chat_id}")

    if not user_input:
        _emit_result(sio, {"id": chat_id, "status": "error", "error": "空输入"})
        return

    t0 = time.time()
    try:
        response = runtime.submit_interrupt(
            user_input,
            source=f"page:{chat_id}",
            wait=True,
            timeout=task_timeout,
        )
    except Exception as e:
        traceback.print_exc()
        _emit_result(sio, {"id": chat_id, "status": "error", "error": str(e)})
        return

    cost = time.time() - t0
    if response is None or response.startswith("（nova 正在忙"):
        msg = response or f"等了 {task_timeout:.0f} 秒，nova 还没回过来。"
        print(f"⏰ 任务超时：{chat_id}（{cost:.1f}s）")
        _emit_result(sio, {"id": chat_id, "status": "timeout", "error": msg})
        return

    print(f"✅ ({cost:.1f}s) nova 回答 {chat_id}：{response[:80]}")
    _emit_result(sio, {"id": chat_id, "output": response, "status": "success"})


def _emit_result(sio: socketio.Client, result: dict) -> None:
    try:
        sio.emit("chat_result", result)
        print(f"📤 已回传：{result.get('id')} ({result.get('status')})")
    except Exception as e:
        print(f"⚠️ chat_result 发送失败：{e}")


def main() -> None:
    args = parse_args()
    runtime = boot_runtime(args)

    if args.no_cloud:
        print("🧪 --no-cloud：只跑 runtime，不连 page。Ctrl+C 退出。")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown(runtime, sio=None)
        return

    sio = build_socket_client(runtime, task_timeout=args.task_timeout)

    print(f"🚀 准备连接云端 page：{args.cloud}")
    while True:
        try:
            sio.connect(args.cloud)
            sio.wait()
        except KeyboardInterrupt:
            print("\n🛑 收到中断信号，准备退出……")
            break
        except Exception as e:
            print(f"❌ 连接 / 维持连接失败：{e}，5 秒后重试")
            time.sleep(5)
        else:
            time.sleep(2)

    _shutdown(runtime, sio=sio)


def _shutdown(runtime: ContinuousRuntime, sio):
    if sio is not None:
        try:
            if sio.connected:
                sio.disconnect()
        except Exception:
            pass
    print("📦 正在保存 nova 状态……")
    runtime.stop()
    runtime.join(timeout=15)
    try:
        runtime.nova.save()
    except Exception as e:
        print(f"⚠️ 最终存档失败：{e}")
    print("（已存档退出。）")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
