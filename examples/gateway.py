"""nova 网关：socket 协议入口。

    python gateway.py

每次连接，从 socket 收一段文字，喂给 nova.perceive，把回应送回去关连接。
"""
import os
import socket
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nova import Nova, NovaConfig


def recv_full(conn) -> str:
    data = b""
    conn.settimeout(1)
    while True:
        try:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            time.sleep(0.01)
        except Exception:
            break
    return data.decode("utf-8", errors="ignore")


def run(host: str = "0.0.0.0", port: int = 10001) -> None:
    cfg = NovaConfig(
        field_path="./data/field",
        seed_memories_file="./examples/seed_memories.txt",
    )
    nova = Nova(cfg)
    print(f"nova 已唤醒（缝隙数 {len(nova.field)}）。监听 {host}:{port}")

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)

    num = 0
    while True:
        num += 1
        try:
            conn, addr = server.accept()
            print(num, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "client", addr)
            stimulus = recv_full(conn)
            if not stimulus.strip():
                conn.close()
                continue

            response = nova.perceive(stimulus)
            print("---- 输入 ----")
            print(stimulus)
            print("---- nova ----")
            print(response)

            conn.sendall(response.encode("utf-8"))
            conn.close()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("错误：", e)
            time.sleep(1)
            continue

    nova.save()
    print("（已存档退出。）")


if __name__ == "__main__":
    run()
