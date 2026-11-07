#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理下载目录里的失败残留文件。

覆盖两处历史漏洞：
    - /data/tg_downloads   Telegram 文件上传失败后留下的本地副本（Bot 不重试）
    - /data/gopeed/downloads 上传重试耗尽、或 Bot 重启落在建任务与写映射之间留下的孤儿文件

安全规则（任一命中即跳过，绝不误删正在使用的文件）：
    1. 文件名出现在 gopeed_tasks.json 的 saved_name 里 → 属于在途任务
    2. 文件修改时间在 N 天以内（默认 7 天）
    3. 下载中间件后缀（.aria2 .part .tmp .gopeed .partial .!qB .td）
    4. 下载根目录本身永不删除

用法：
    python3 cleanup_leftovers.py [--dry-run] [--days 7] [--notify]
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TG_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/data/tg_downloads"))
GOPEED_DIR = Path(os.environ.get("GOPEED_DLDIR", "/data/gopeed/downloads"))
ARIA2_DIR = Path(os.environ.get("ARIA2_HOST_DLDIR", "/data/aria2/downloads"))
GOPEED_MAP = Path(os.environ.get("GOPEED_MAP", "/opt/cloud-disk-transfer/logs/gopeed_tasks.json"))
ENVF = Path("/etc/tgbot.env")

SKIP_SUFFIXES = (".aria2", ".part", ".partial", ".tmp", ".gopeed", ".!qb", ".td", ".log")
DEFAULT_DAYS = 7


def active_names() -> set[str]:
    """映射表里正在处理的任务文件名，绝不删除。"""
    try:
        data = json.loads(GOPEED_MAP.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    names = set()
    for info in data.values():
        for key in ("saved_name", "orig_name"):
            val = (info or {}).get(key)
            if val:
                names.add(os.path.basename(val))
    return names


def iter_files(root: Path):
    if not root.is_dir():
        return
    for p in root.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                yield p
        except OSError:
            continue


def cleanup(root: Path, cutoff: float, protected: set[str], dry_run: bool):
    removed, freed, kept = [], 0, 0
    for p in iter_files(root):
        name = p.name
        if name in protected or name.lower().endswith(SKIP_SUFFIXES):
            kept += 1
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff:
            kept += 1
            continue
        age_days = (time.time() - st.st_mtime) / 86400
        if dry_run:
            print(f"  [dry-run] 将删除 {p}（{st.st_size/1048576:.2f} MB，{age_days:.1f} 天前）")
        else:
            try:
                p.unlink()
            except OSError as exc:
                print(f"  [警告] 删除失败 {p}: {exc}")
                continue
        removed.append((str(p), st.st_size))
        freed += st.st_size

    # 自底向上回收空目录（不碰根目录）
    if not dry_run and removed:
        for d in sorted((x for x in root.rglob("*") if x.is_dir()),
                        key=lambda x: len(x.parts), reverse=True):
            try:
                if d != root and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
    return removed, freed, kept


def notify(count: int, freed: int, details: list[str]) -> None:
    """用 Bot token 直发一条 Telegram 消息（失败不影响清理结果）。"""
    if not count:
        return
    try:
        env = {}
        for raw in ENVF.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                k, v = raw.split("=", 1)
                env[k.strip()] = v.strip()
        token, chat = env.get("BOT_TOKEN"), env.get("ALLOWED_CHAT")
        if not token or not chat:
            return
        text = (f"🧹 服务器自动清理完成\n{'─' * 22}\n\n"
                f"清理了 {count} 个早就该删的残留文件，腾出 {freed / 1048576:.1f} MB\n"
                f"（都是 7 天以上没人管的历史文件，不影响正在下载的任务）\n\n"
                + "\n".join(details[:10]))
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
    except Exception as exc:
        print(f"[警告] 通知发送失败: {exc}")


def cleanup_torrent_cache(root: Path, cutoff: float, dry_run: bool):
    """aria2 的磁力元数据缓存（<hash>.torrent）会一直留在下载目录，只清这一种文件。

    注意：这里**只删 .torrent**，绝不动 /data/aria2/downloads 里正在下载或已下载的数据，
    因为那是 aria2 自己的地盘，误删代价比 Gopeed 目录大得多。
    """
    removed, freed = [], 0
    for p in iter_files(root):
        if p.suffix.lower() != ".torrent":
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff:
            continue
        if dry_run:
            print(f"  [dry-run] 将删除磁力缓存 {p}")
        else:
            try:
                p.unlink()
            except OSError as exc:
                print(f"  [警告] 删除失败 {p}: {exc}")
                continue
        removed.append((str(p), st.st_size))
        freed += st.st_size
    return removed, freed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args()

    cutoff = time.time() - args.days * 86400
    protected = active_names()
    print(f"保护中的在途文件 {len(protected)} 个；阈值 {args.days} 天")

    all_lines, total, total_freed = [], 0, 0
    for root in (TG_DIR, GOPEED_DIR):
        if not root.is_dir():
            print(f"{root} 不存在，跳过")
            continue
        removed, freed, kept = cleanup(root, cutoff, protected, args.dry_run)
        total += len(removed)
        total_freed += freed
        print(f"{root}: 删除 {len(removed)} 个，释放 {freed/1048576:.2f} MB，保留 {kept} 个")
        if removed:
            label = "Telegram 文件" if root == TG_DIR else "下载目录"
            all_lines.append(f"· {label}：{len(removed)} 个，腾出 {freed/1048576:.1f} MB")
            for path, _ in removed[:3]:
                all_lines.append(f"　 {os.path.basename(path)}")

    if ARIA2_DIR.is_dir():
        removed, freed = cleanup_torrent_cache(ARIA2_DIR, cutoff, args.dry_run)
        total += len(removed)
        total_freed += freed
        print(f"{ARIA2_DIR}: 清理磁力缓存 {len(removed)} 个，释放 {freed/1048576:.2f} MB")
        if removed:
            all_lines.append(f"· 磁力缓存：{len(removed)} 个，腾出 {freed/1048576:.1f} MB")

    print(f"合计删除 {total} 个文件" + ("（dry-run，未实际删除）" if args.dry_run else ""))
    if args.notify and not args.dry_run:
        notify(total, total_freed, all_lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
