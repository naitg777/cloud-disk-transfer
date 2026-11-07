# -*- coding: utf-8 -*-
"""空间不足时的降级策略：夸克只剩很少空间（会员过期）时怎么办。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/sp /data/venv/bin/python3 test_space.py
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/sp"))
shutil.rmtree(WORK, ignore_errors=True)
for sub in ("gdl", "tgdl", "logs"):
    (WORK / sub).mkdir(parents=True)
os.environ.update({
    "BOT_TOKEN": "T", "ALLOWED_CHAT": "42", "ARIA2_SECRET": "T",
    "DOWNLOAD_DIR": str(WORK / "tgdl"), "GOPEED_DLDIR": str(WORK / "gdl"),
    "GOPEED_MAP": str(WORK / "logs/map.json"),
    "GOPEED_QUEUE": str(WORK / "logs/queue.json"),
    "ALERT_STATE": str(WORK / "logs/a.json"), "RCLONE_CONF": str(WORK / "r.json"),
})
sys.path.insert(0, SERVER_DIR)
import tgbot  # noqa: E402
tgbot.BRIDGE_COOKIE_FILE = str(WORK / "no_bridge.cookie")   # 测试不读生产转存账号

FAILS, N = [], [0]
MB = 1024 * 1024
NOTIFIES = []


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(label)
        print(" ✗", label, extra)
    else:
        print(" ✓", label)


class FakeGopeed:
    def __init__(self):
        self.calls = []

    def add_task(self, url, name, path, headers=None, connections=None):
        self.calls.append((url, headers, name))
        return "tid%d" % len(self.calls)

    def list_tasks(self):
        return []

    def remove_task(self, tid, force=True):
        pass


class FakeAccount:
    files = []
    total = 10 * 1024 ** 3
    used = 9 * 1024 ** 3
    saved = []

    def __init__(self, *a, **k):
        pass

    @staticmethod
    def parse_link(t):
        return "pwdspace", ""

    def get_stoken(self, pwd_id, passcode=""):
        return "st"

    def walk_share(self, pwd_id, stoken, *a, **kw):
        return list(FakeAccount.files)

    def download_headers(self):
        return {"Cookie": "account=1"}

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        return "https://dl-pc-zb/%s" % fid

    def quota(self):
        return FakeAccount.total, FakeAccount.used

    def member_info(self):
        return {"member_type": "VIP",
                "total_capacity": FakeAccount.total,
                "use_capacity": FakeAccount.used}

    def ensure_dir(self, name, pdir_fid="0"):
        return "BRIDGE"

    def save(self, pwd_id, stoken, fids, tokens, to_pdir_fid="0"):
        FakeAccount.saved.append(list(fids))
        return "task"

    def wait_task_fids(self, task_id, timeout=300, interval=2.0):
        return ["OWN%d" % i for i in range(len(FakeAccount.saved[-1]))]

    def own_direct_url(self, own_fid):
        return "https://dl-pc-zb/own/%s" % own_fid

    def delete(self, fids):
        pass


class FakeGuest:
    def __init__(self, *a, **k):
        pass

    def get_stoken(self, pwd_id, passcode=""):
        return "gst"

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        return "https://dl-guest/%s" % fid

    def download_headers(self):
        return {"Cookie": "__sdid=abc"}


class Disk:
    total = 100 * 1024 ** 3
    free = 50 * 1024 ** 3
    used = 50 * 1024 ** 3


async def fake_notify(t):
    NOTIFIES.append(t)


def setup(free_gb):
    tgbot.QuarkShare = FakeAccount
    tgbot.QuarkGuest = FakeGuest
    tgbot.QUARK_MODE = "auto"
    tgbot.gopeed = FakeGopeed()
    tgbot.shutil.disk_usage = lambda p: Disk
    tgbot._probe_url = lambda url, hdrs: False     # 分享直链一律不可用
    tgbot._share_cache.clear()
    tgbot._stoken_cache.clear()
    tgbot._guest_cache.clear()
    tgbot._share_direct_known = None
    tgbot._space_warned.clear()
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})
    FakeAccount.saved.clear()
    NOTIFIES.clear()
    tgbot._quark_quota_cache["main"].update(
        ts=time.time(), total=10 * 1024 ** 3,
        used=int((10 - free_gb) * 1024 ** 3))
    tgbot._quark_quota_cache["bridge"].update(ts=0.0, total=0, used=0)


def queue_files(sizes, pwd="pwdspace"):
    items = [{"fid": "F%d" % i, "name": "f%d.bin" % i, "size": s, "rel": "f%d.bin" % i,
              "fid_token": "T%d" % i} for i, s in enumerate(sizes)]
    # 转存时会重新扫一次分享拿最新 token，这里让「扫描结果」= 队列内容
    FakeAccount.files = [{"fid": x["fid"], "name": x["name"], "size": x["size"],
                          "path": x["rel"], "share_fid_token": x["fid_token"]}
                         for x in items]
    tgbot.queue_put(pwd, "分享_x", "2026-11-04", "", items)


async def case_enough_space():
    setup(5)                       # 剩 5GB
    queue_files([80 * MB, 90 * MB])
    st = await tgbot.admit_wave(force=True)
    check(st["admitted"] == 2, "空间够时正常转存下载", str(st))
    check(FakeAccount.saved and len(FakeAccount.saved[-1]) == 2, "整波转存", str(FakeAccount.saved))
    urls = [c[0] for c in tgbot.gopeed.calls]
    check(all("/own/" in u for u in urls), "用的是自己网盘直链", str(urls))


async def case_space_exhausted():
    setup(0.1)                     # 只剩 100MB，低于 300MB 门槛
    queue_files([10 * MB, 80 * MB])
    st = await tgbot.admit_wave(force=True)
    m = tgbot.load_gopeed_map()
    check(not FakeAccount.saved, "空间不足不转存", str(FakeAccount.saved))
    check(st["admitted"] == 1, "小文件仍能下（公开通道）", str(st))
    urls = [c[0] for c in tgbot.gopeed.calls]
    check(urls and all("dl-guest" in u for u in urls), "小文件走公开直链", str(urls))
    check(list(m.values())[0]["own_fid"] == "", "公开会话任务没有中转副本")
    left = sum(len(v.get("items") or []) for v in tgbot.load_queue().values())
    check(left == 1, "大文件留在队列里等空间", str(left))
    check(any("空间不够" in n for n in NOTIFIES), "通知说明空间不够", str(NOTIFIES[-1:]))


async def case_partial_fit():
    setup(1.0)                     # 剩 1GB，预算 0.6GB
    queue_files([200 * MB] * 4)
    st = await tgbot.admit_wave(force=True)
    check(st["admitted"] == 3, "只转存装得下的部分", str(st))
    left = sum(len(v.get("items") or []) for v in (tgbot.load_queue().values()))
    check(left == 1, "装不下的留队列", str(left))


async def case_disk_view():
    setup(0.2)
    block = tgbot._quark_block()
    check("夸克主号" in block, "磁盘页显示夸克容量", block[:40])
    check("空间不够" in block, "空间紧张时给出提示", block[-40:])
    setup(5)
    check("🟢 可用" in tgbot._quark_block(), "空间充足时提示正常")


async def case_cooldown():
    """冷却中的文件这波要跳过；冷却到点后要能自动重试（不能永久放弃）。"""
    setup(5)
    queue_files([10 * MB])
    queues = tgbot.load_queue()
    pwd = next(iter(queues))
    # 1) 未来冷却 → 这一波不放行
    for it in queues[pwd]["items"]:
        it["skip_until"] = int(time.time() + 3600)
    tgbot.save_queue(queues)
    st = await tgbot.admit_wave(force=True)
    check(st["admitted"] == 0, "冷却中的文件不建任务", str(st))
    check(len(tgbot.load_gopeed_map()) == 0, "冷却中不进映射", str(len(tgbot.load_gopeed_map())))

    # 2) 冷却已过期 → 自动恢复
    queues = tgbot.load_queue()
    for it in queues[pwd]["items"]:
        it["skip_until"] = int(time.time() - 1)
    tgbot.save_queue(queues)
    st = await tgbot.admit_wave(force=True)
    check(st["admitted"] == 1, "冷却到点后自动重试", str(st))


async def main():
    tgbot._notify = fake_notify
    await case_enough_space()
    await case_space_exhausted()
    await case_partial_fit()
    await case_disk_view()
    await case_cooldown()


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
