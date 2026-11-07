#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日体检：服务、磁盘、两个夸克账号、队列积压、最近上传。

用法：
    python3 healthcheck.py            # 只打印
    python3 healthcheck.py --notify   # 有问题才发 Telegram（每天一次汇总）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for raw in open("/etc/tgbot.env", encoding="utf-8").read().splitlines():
    if "=" in raw and not raw.lstrip().startswith("#"):
        k, v = raw.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import tgbot  # noqa: E402

GB = 1024 ** 3
MB = 1024 ** 2
PROBLEMS = []


def ok(cond, name, detail=""):
    mark = "✅" if cond else "❌"
    line = f"{mark} {name}" + (f"：{detail}" if detail else "")
    if not cond:
        PROBLEMS.append(line)
    return line


def send(text: str) -> None:
    url = f"https://api.telegram.org/bot{tgbot.BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": tgbot.ALLOWED_CHAT,
                       "text": text[:3900]}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args()

    lines = [f"🩺 系统体检 {time.strftime('%Y-%m-%d %H:%M', time.localtime())}"]

    # 1. 服务
    for svc in ("tgbot", "gopeed"):
        r = subprocess.run(["systemctl", "is-active", svc],
                           capture_output=True, text=True)
        lines.append(ok(r.stdout.strip() == "active", f"服务 {svc}", r.stdout.strip()))
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "aria2-pro"],
                       capture_output=True, text=True)
    lines.append(ok(r.stdout.strip() == "true", "容器 aria2-pro", r.stdout.strip()[:10]))

    # 2. Gopeed / aria2 接口
    try:
        tasks = tgbot.gopeed.list_tasks()
        running = sum(1 for t in tasks if t.get("status") == "running")
        lines.append(ok(True, "Gopeed 接口", f"{len(tasks)} 个任务 / 运行 {running}"))
    except Exception as exc:
        lines.append(ok(False, "Gopeed 接口", str(exc)[:60]))
    try:
        ver = tgbot._aria2("aria2.getVersion", []).get("version")
        lines.append(ok(True, "aria2 接口", ver or "?"))
    except Exception as exc:
        lines.append(ok(False, "aria2 接口", str(exc)[:60]))

    # 3. 磁盘
    du = shutil.disk_usage("/data")
    lines.append(ok(du.free > tgbot.MIN_FREE_BYTES, "服务器磁盘",
                    f"剩 {du.free / GB:.1f} GB"))

    # 4. 夸克账号
    for kind, title in (("main", "夸克主号"), ("bridge", "夸克转存号")):
        if kind == "bridge" and not tgbot.bridge_account_ready():
            continue
        try:
            q = tgbot._client_for(kind)
            total, used = q.quota()
            free = total - used
            info = q.member_info()
            cap = tgbot._member_cache.get(kind, {}).get("type") or info.get("member_type", "")
            lines.append(ok(free > tgbot.QUARK_MIN_FREE_MB * MB,
                            f"{title}（{cap}）",
                            f"剩 {free / GB:.1f} GB" if total else "查不到容量"))
        except Exception as exc:
            lines.append(ok(False, title, f"Cookie 可能失效：{str(exc)[:60]}"))

    # 5. 队列 / 在途
    try:
        queued = sum(len(v.get("items") or []) for v in tgbot.load_queue().values())
        inflight = len(tgbot.load_gopeed_map())
        lines.append(ok(True, "待处理", f"排队 {queued} / 在途 {inflight}"))
    except Exception as exc:
        lines.append(ok(False, "队列读取", str(exc)[:60]))

    # 6. 最近日志错误数
    r = subprocess.run(["journalctl", "-u", "tgbot", "--since", "-2h", "--no-pager"],
                       capture_output=True, text=True)
    errs = sum(1 for ln in r.stdout.splitlines() if "ERROR" in ln)
    lines.append(ok(errs < 10, "近 2 小时错误数", str(errs)))

    text = "\n".join(lines)
    print(text)
    if args.notify:
        if PROBLEMS:
            send("⚠️ 体检发现问题：\n\n" + text)
        print(f"\n问题数: {len(PROBLEMS)}")
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    sys.exit(main())
