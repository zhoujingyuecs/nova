"""缝隙场的存盘与读取。

存档格式刻意做得朴素，方便人眼检查：
  field/
    meta.json        —— 维度、版本号、缝隙数等
    fissures.json    —— 每条缝隙的可读字段
    shapes.npy       —— 当前形状矩阵 (N, d) float32
    origins.npy      —— 出生形状矩阵 (N, d) float32

shapes.npy 与 fissures.json 中的顺序严格对齐。

★ v1.0 修复（重要）：
  之前 fissures.json 直接 open("w") 写入。如果进程在 json.dump 中途
  被 SIGKILL / 断电 / OOM 打断，文件就会留下一个**截断的**残骸——
  下次启动 json.load 时炸掉，nova 整个起不来。

  这一版改成所有写入都走 tmp + os.replace 原子替换。同时启动时如果
  发现文件损坏，会自动尝试用上一份 .bak 备份恢复；恢复不了就把损坏
  文件移到 fissures.json.broken 留作证据，从空场重启。
  这样最坏情况下你只丢最近一次保存之间的几条新缝隙——nova 不会再
  因为一次断电就全废。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from typing import Optional

import numpy as np

from .config import NovaConfig
from .field import FissureField
from .fissure import Fissure


_VERSION = 3


def _atomic_write_text(path: str, content: str) -> None:
    """原子写入：先写到 .tmp，再 os.replace。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _atomic_write_bytes(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _rotate_backup(path: str, keep: int = 3) -> None:
    """把当前 path 滚动备份成 path.bak.0 ~ path.bak.{keep-1}。

    .bak.0 是最近一次成功保存前的状态。"""
    if not os.path.exists(path) or keep <= 0:
        return
    # 把旧 bak 往后挪：bak.{n-2} -> bak.{n-1} ...
    for i in range(keep - 1, 0, -1):
        src = f"{path}.bak.{i - 1}"
        dst = f"{path}.bak.{i}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        shutil.copy2(path, f"{path}.bak.0")
    except OSError:
        pass


def save_field(field: FissureField, path: Optional[str] = None,
               keep_backup: int = 3) -> None:
    path = path or field.cfg.field_path
    os.makedirs(path, exist_ok=True)

    fissures = field.all()
    stats = field.link_stats()
    meta = {
        "version": _VERSION,
        "dim": field.dim,
        "count": len(fissures),
        "embedding_model": field.cfg.embedding_model,
        "saved_at": time.time(),
        "link_stats": stats,
    }

    fissures_json_path = os.path.join(path, "fissures.json")

    # 在写入前滚动一份备份（防止这次写一半坏掉就把好的也覆盖了）
    _rotate_backup(fissures_json_path, keep=keep_backup)

    _atomic_write_text(
        os.path.join(path, "meta.json"),
        json.dumps(meta, ensure_ascii=False, indent=2),
    )

    _atomic_write_text(
        fissures_json_path,
        json.dumps(
            [fis.to_dict() for fis in fissures],
            ensure_ascii=False,
            indent=2,
        ),
    )

    if fissures:
        shapes = np.stack([fis.shape for fis in fissures]).astype(np.float32)
        origins = np.stack([fis.origin_shape for fis in fissures]).astype(np.float32)
    else:
        shapes = np.zeros((0, field.dim), dtype=np.float32)
        origins = np.zeros((0, field.dim), dtype=np.float32)

    # numpy 没有内置原子写，我们手动绕一下
    import io
    buf = io.BytesIO()
    np.save(buf, shapes, allow_pickle=False)
    _atomic_write_bytes(os.path.join(path, "shapes.npy"), buf.getvalue())
    buf = io.BytesIO()
    np.save(buf, origins, allow_pickle=False)
    _atomic_write_bytes(os.path.join(path, "origins.npy"), buf.getvalue())


def _read_json_with_recovery(path: str) -> Optional[list]:
    """读取 fissures.json；损坏时尝试 .bak.0 / .bak.1 / .bak.2 恢复。

    返回解析出的 list，或 None（彻底失败）。"""
    if not os.path.exists(path):
        return None

    candidates = [path] + [f"{path}.bak.{i}" for i in range(3)]
    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                print(f"⚠️ {candidate} 不是 list，跳过。")
                continue
            if candidate != path:
                # 用备份恢复：把损坏的原文件移到 .broken，备份提升为正本
                broken = path + ".broken"
                try:
                    if os.path.exists(broken):
                        os.remove(broken)
                    os.rename(path, broken)
                    print(f"⚠️ {os.path.basename(path)} 损坏，已移到 {os.path.basename(broken)}；")
                    print(f"   用 {os.path.basename(candidate)} 恢复。")
                except OSError as e:
                    print(f"   （重命名 broken 文件失败：{e}）")
                # 把候选回写到 path（已经原子）
                with open(candidate, "r", encoding="utf-8") as f:
                    raw_text = f.read()
                _atomic_write_text(path, raw_text)
            return data
        except json.JSONDecodeError as e:
            print(f"⚠️ {os.path.basename(candidate)} JSON 解析失败：{e}")
            continue
        except OSError as e:
            print(f"⚠️ {os.path.basename(candidate)} 读取出错：{e}")
            continue

    # 全部失败：把损坏文件移到 .broken 留底
    broken = path + ".broken"
    try:
        if os.path.exists(broken):
            os.remove(broken)
        os.rename(path, broken)
        print(f"❌ {os.path.basename(path)} 和所有备份都读不出来，已移到 {os.path.basename(broken)}。")
        print(f"   nova 将从空缝隙场重启，但是种子记忆会再次注入。")
    except OSError:
        pass
    return None


def load_field(cfg: NovaConfig, embedding_dim: int,
               path: Optional[str] = None) -> FissureField:
    path = path or cfg.field_path
    field = FissureField(cfg, embedding_dim)

    meta_path = os.path.join(path, "meta.json")
    if not os.path.exists(meta_path):
        return field

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠️ meta.json 损坏：{e}；从空场重启。")
        return field

    if meta.get("dim") != embedding_dim:
        raise RuntimeError(
            f"维度不匹配：旧缝隙场 dim={meta.get('dim')}，"
            f"当前嵌入器 dim={embedding_dim}。请清空 {path} 或使用同一嵌入模型。"
        )

    fissures_json_path = os.path.join(path, "fissures.json")
    shapes_path = os.path.join(path, "shapes.npy")
    origins_path = os.path.join(path, "origins.npy")
    if not (os.path.exists(fissures_json_path)
            and os.path.exists(shapes_path)
            and os.path.exists(origins_path)):
        return field

    fissure_dicts = _read_json_with_recovery(fissures_json_path)
    if fissure_dicts is None:
        return field

    try:
        shapes = np.load(shapes_path)
        origins = np.load(origins_path)
    except Exception as e:
        print(f"⚠️ shapes/origins 加载失败：{e}；从空场重启。")
        return field

    # fissures.json 行数和 npy 行数应该一致；不一致时按较短的对齐
    n_json = len(fissure_dicts)
    n_shapes = len(shapes)
    n_origins = len(origins)
    n = min(n_json, n_shapes, n_origins)
    if n != n_json or n != n_shapes or n != n_origins:
        print(f"⚠️ 缝隙记录不对齐（json={n_json}, shapes={n_shapes}, origins={n_origins}），按 {n} 对齐。")
        fissure_dicts = fissure_dicts[:n]
        shapes = shapes[:n]
        origins = origins[:n]

    # 第一遍：加载所有缝隙
    bad = 0
    for d, s, o in zip(fissure_dicts, shapes, origins):
        try:
            fis = Fissure.from_dict(d, shape=s, origin_shape=o)
            field._add_fissure(fis)
        except Exception:
            bad += 1
    if bad:
        print(f"⚠️ 跳过 {bad} 条无法解析的缝隙。")

    # 第二遍：清理失效引用
    valid_ids = set(field._fissures.keys())
    cleaned_links = 0
    cleaned_chain = 0
    for fis in field._fissures.values():
        for tid in list(fis.outgoing_links.keys()):
            if tid not in valid_ids:
                del fis.outgoing_links[tid]
                cleaned_links += 1
        if fis.prev_id and fis.prev_id not in valid_ids:
            fis.prev_id = ""
            cleaned_chain += 1
        if fis.next_id and fis.next_id not in valid_ids:
            fis.next_id = ""
            cleaned_chain += 1
    if cleaned_links > 0:
        print(f"⚠️ 清理了 {cleaned_links} 条指向不存在缝隙的失效链接")
    if cleaned_chain > 0:
        print(f"⚠️ 清理了 {cleaned_chain} 条断裂的对话链指针")

    field.sync_all()

    stats = field.link_stats()
    loaded_version = meta.get("version", 1)
    chain_nodes = stats.get("chain_nodes", 0)
    print(
        f"📦 缝隙场加载（存档版本 v{loaded_version}）："
        f"{stats['node_count']} 条缝隙，"
        f"{stats['total_links']} 条暗道，"
        f"其中 {chain_nodes} 条带对话链，"
        f"平均链强度 {stats['mean_link_strength']:.2f}"
    )
    if loaded_version < _VERSION:
        print(
            f"   （从 v{loaded_version} 升级到 v{_VERSION}：旧缝隙没有场景元数据，"
            f"新对话从此刻开始会带上 speaker / episode_id / 对话链。）"
        )
    return field
