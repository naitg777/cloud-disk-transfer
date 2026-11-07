# -*- coding: utf-8 -*-
"""磁力/直链 分批排队（aria2 队列）测试。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/at /data/venv/bin/python3 test_aria2.py
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/at"))
shutil.rmtree(WORK, ignore_errors=True)
for sub in ("gdl", "tgdl", "logs"):
    (WORK / sub).mkdir(parents=True)

os.environ.update({
    "BOT_TOKEN": "T", "ALLOWED_CHAT": "42", "ARIA2_SECRET": "T",
    "DOWNLOAD_DIR": str(WORK / "tgdl"),
    "GOPEED_DLDIR": str(WORK / "gdl"),
    "GOPEED_MAP": str(WORK / "logs" / "map.json"),
    "GOPEED_QUEUE": str(WORK / "logs" / "queue.json"),
    "ARIA2_QUEUE": str(WORK / "logs" / "aria2_queue.json"),
    "QUARK_BRIDGE_COOKIE_FILE": str(WORK / "logs" / "no_bridge.cookie"),
    "ALERT_STATE": str(WORK / "logs" / "a.json"),
    "RCLONE_CONF": str(WORK / "r.json"),
})
sys.path.insert(0, SERVER_DIR)
import tgbot  # noqa: E402
tgbot.BRIDGE_COOKIE_FILE = str(WORK / "no_bridge.cookie")   # 测试不读生产转存账号

FAILS, N = [], [0]


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(label)
        print(" ✗", label, extra)
    else:
        print(" ✓", label)


class FakeAria2:
    def __init__(self):
        self.active = []
        self.added = []
        self.fail_next = 0

    def __call__(self, method, params, timeout=10):
        if method == "aria2.tellActive":
            return [{"gid": g} for g in self.active]
        if method == "aria2.addUri":
            if self.fail_next:
                self.fail_next -= 1
                raise RuntimeError("boom")
            gid = "g%d" % (len(self.added) + 1)
            self.added.append((gid, params[0][0]))
            self.active.append(gid)
            return gid
        return None


class Disk:
    def __init__(self, free):
        self.total = 100 * 1024 ** 3
        self.free = free
        self.used = self.total - free


async def main():
    a = FakeAria2()
    tgbot._aria2 = a
    tgbot.shutil.disk_usage = lambda p: Disk(10 * 1024 ** 3)
    tgbot.save_aria2_queue([])
    tgbot.ARIA2_MAX_ACTIVE = 2

    # 3 个链接：一次只放 2 个
    for i in range(3):
        tgbot.aria2_queue_add("magnet:?xt=urn:btih:TEST%d" % i)
    res = await tgbot.feed_aria2(force=True)
    check(res["started"] == 2, "只放行到并发上限", str(res))
    check(tgbot.aria2_queue_size() == 1, "剩下的还在排队", str(tgbot.aria2_queue_size()))

    # 模拟一个下载完成，腾出名额
    a.active.pop(0)
    res = await tgbot.feed_aria2()
    check(res["started"] == 1 and tgbot.aria2_queue_size() == 0,
          "有坑位后自动续上", str(res))

    # 磁盘余量不足 → 不放行
    tgbot.shutil.disk_usage = lambda p: Disk(1 * 1024 ** 3)   # 低于 2GB 安全线
    tgbot.aria2_queue_add("magnet:?xt=urn:btih:LOWDISK")
    a.active.clear()
    res = await tgbot.feed_aria2(force=True)
    check(res["started"] == 0, "磁盘不够时不放行", str(res))
    check(tgbot.aria2_queue_size() == 1, "链接保留在队列", str(tgbot.aria2_queue_size()))

    # 添加失败 3 次后丢弃
    tgbot.shutil.disk_usage = lambda p: Disk(10 * 1024 ** 3)
    a.active.clear()
    a.fail_next = 3
    for _ in range(4):
        await tgbot.feed_aria2(force=True)
    check(tgbot.aria2_queue_size() == 0, "连续失败 3 次后丢弃", str(tgbot.aria2_queue_size()))

    # 同一链接不重复入队
    tgbot.save_aria2_queue([])
    tgbot.aria2_queue_add("https://example.com/a.zip")
    tgbot.aria2_queue_add("https://example.com/a.zip")
    check(tgbot.aria2_queue_size() == 1, "重复链接不重复排队", str(tgbot.aria2_queue_size()))


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
