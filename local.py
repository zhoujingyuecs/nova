# coding=UTF-8
"""
local.py —— 本地 nova 运算服务器
-------------------------------------
功能：
  1. 连接云服务器（page.py 所在机器）的 socketio
  2. 接收云服务器派发的对话任务（id + input）
  3. 用本地的 Nova 实例调用 perceive()，让她回应
  4. 回传结果（id + output + status）

本地只跑 nova，不再跑别的。
"""

import socketio
import time
import threading
import queue
import traceback

from nova import Nova, NovaConfig, Daydreamer

# =============================================================
# 1. 启动 nova
# =============================================================
print("⏳ 正在唤醒 nova，请稍候……")
cfg = NovaConfig(
    field_path="./data/field",
    seed_memories_file="./examples/seed_memories.txt",
    daydream_enabled=True,                  # 让她在没人说话的时候也自己想事情
)
nova = Nova(cfg)
print(f"✅ nova 已醒。当前缝隙数：{len(nova.field)}")

# 后台走神线程：网站没人说话时，她也在自己想事情
dreamer = Daydreamer(
    nova,
    interval_seconds=cfg.daydream_interval_seconds,
    jitter=cfg.daydream_jitter,
    on_dream=lambda t: print(f"💭 nova 出神：{t[:80]}"),
)
if cfg.daydream_enabled:
    dreamer.start()
    print("💭 走神线程已开启")

# =============================================================
# 2. 任务执行
# =============================================================
TASK_TIMEOUT = 240  # 单条对话最多 4 分钟


def execute_task(task_data):
    """带超时控制地让 nova 回应一次"""
    chat_id = task_data.get("id", "")
    user_input = task_data.get("input", "")

    result = {"id": chat_id, "status": "error", "error": "未执行"}

    def worker():
        nonlocal result
        try:
            print(f"🔨 nova 正在思考：{user_input[:60]}...")
            t0 = time.time()
            response = nova.perceive(user_input)
            cost = time.time() - t0
            result = {
                "id": chat_id,
                "output": response,
                "status": "success",
            }
            print(f"✅ ({cost:.1f}s) nova 回答：{response[:80]}")
        except Exception as e:
            traceback.print_exc()
            result = {
                "id": chat_id,
                "status": "error",
                "error": str(e),
            }

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=TASK_TIMEOUT)

    if t.is_alive():
        print(f"⏰ 任务超时（>{TASK_TIMEOUT}s）：{chat_id}")
        result = {
            "id": chat_id,
            "status": "timeout",
            "error": f"她想得太久了（>{TASK_TIMEOUT}s），暂时没回过来",
        }
    return result


# =============================================================
# 3. socketio 客户端 & 任务队列
# =============================================================
sio = socketio.Client()
CLOUD_SERVER_URL = "http://139.224.11.35:8080"

task_queue = queue.Queue()
is_running = False


@sio.event
def connect():
    print(f"✅ 已连接云服务器：{CLOUD_SERVER_URL}")


@sio.event
def connect_error(data):
    print(f"❌ 连接云服务器失败：{data}")


@sio.event
def disconnect():
    print("❌ 与云服务器断开连接")


@sio.on("connect_success")
def handle_connect_success(data):
    print(
        f"📢 云服务器欢迎：{data.get('msg')} | 客户端 ID：{data.get('client_id')}"
    )


@sio.on("new_chat_task")
def handle_new_chat_task(task_data):
    chat_id = task_data.get("id", "?")
    print(f"📥 收到对话任务：{chat_id}")
    task_queue.put(task_data)
    if not is_running:
        threading.Thread(target=consume_tasks, daemon=True).start()


def consume_tasks():
    global is_running
    is_running = True
    try:
        while not task_queue.empty():
            td = task_queue.get()
            try:
                # nova 回应时，先暂停走神，避免占用 LLM
                was_alive = dreamer.is_alive() and not dreamer.is_paused
                if was_alive:
                    dreamer.pause()
                try:
                    res = execute_task(td)
                finally:
                    if was_alive:
                        dreamer.resume()
                sio.emit("chat_result", res)
                print(f"📤 已回传结果：{res.get('id')} ({res.get('status')})")
            except Exception as e:
                print(f"❌ 任务消费异常：{e}")
            finally:
                task_queue.task_done()
    finally:
        is_running = False


# =============================================================
# 4. 启动
# =============================================================
def connect_to_cloud():
    while True:
        try:
            sio.connect(CLOUD_SERVER_URL)
            sio.wait()
        except Exception as e:
            print(f"❌ 连接/重连失败：{e}，5 秒后重试")
            time.sleep(5)


if __name__ == "__main__":
    time.sleep(1)
    print(f"🚀 本地服务启动，目标云端：{CLOUD_SERVER_URL}")
    try:
        connect_to_cloud()
    except KeyboardInterrupt:
        print("\n🛑 收到中断信号，准备退出……")
        if dreamer.is_alive():
            dreamer.stop()
            dreamer.join(timeout=2.0)
        nova.save()
        print("（已存档退出。）")
