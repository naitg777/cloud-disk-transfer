# -*- coding: utf-8 -*-
"""双账号分工：小文件用「专用转存号」，大文件用主号。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/da /data/venv/bin/python3 test_dual.py
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/da"))
shutil.rmtree(WORK, ignore_errors=True)
for sub in ("gdl", "tgdl", "logs"):
    (WORK / sub).mkdir(parents=True)
os.environ.update({
    "BOT_TOKEN": "T", "ALLOWED_CHAT": "42", "ARIA2_SECRET": "T",
    "DOWNLOAD_DIR": str(WORK / "tgdl"), "GOPEED_DLDIR": str(WORK / "gdl"),
    "GOPEED_MAP": str(WORK / "logs/map.json"),
    "GOPEED_QUEUE": str(WORK / "logs/queue.json"),
    "ALERT_STATE": str(WORK / "logs/a.json"), "RCLONE_CONF": str(WORK / "r.json"),
    "QUARK_BRIDGE_COOKIE_FILE": str(WORK / "bridge.cookie"),
})
sys.path.insert(0, SERVER_DIR)
import tgbot  # noqa: E402

FAILS, N = [], [0]
MB = 1024 * 1024


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
        self.calls.append(url)
        return "tid%d" % len(self.calls)

    def list_tasks(self):
        return []

    def remove_task(self, tid, force=True):
        pass


class _Acct:
    """账号桩：saved 记录转存了哪些文件。"""

    member = "VIP"
    total = 100 * 1024 ** 3
    used = 10 * 1024 ** 3
    saved = []
    name = "acct"

    def __init__(self, *a, **k):
        pass

    files = []

    def walk_share(self, pwd_id, stoken, *a, **kw):
        return list(type(self).files)

    def get_stoken(self, pwd_id, passcode=""):
        return "st"

    def download_headers(self):
        return {"Cookie": "c"}

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        return "https://share/%s" % fid

    def quota(self):
        return type(self).total, type(self).used

    def member_info(self):
        return {"member_type": type(self).member,
                "total_capacity": type(self).total,
                "use_capacity": type(self).used}

    def ensure_dir(self, name, pdir_fid="0"):
        return "BRIDGE_" + type(self).name

    def save(self, pwd_id, stoken, fids, tokens, to_pdir_fid="0"):
        type(self).saved.append(list(fids))
        return "task_" + type(self).name

    def wait_task_fids(self, task_id, timeout=300, interval=2.0):
        return ["%s_OWN%d" % (type(self).name, i)
                for i in range(len(type(self).saved[-1]))]

    def own_direct_url(self, own_fid):
        return "https://%s/%s" % (type(self).name, own_fid)

    def delete(self, fids):
        pass


class FakeMain(_Acct):
    name = "main"
    member = "VIP"
    saved = []


class FakeBridge(_Acct):
    name = "bridge"
    member = "NORMAL"
    saved = []


class FakeGuestAcct(_Acct):
    name = "guest"
    saved = []


class Disk:
    total = 100 * 1024 ** 3
    free = 50 * 1024 ** 3
    used = 50 * 1024 ** 3


def setup(with_bridge: bool):
    cookie = WORK / "bridge.cookie"
    if with_bridge:
        cookie.write_text("dummy-cookie", encoding="utf-8")
    elif cookie.exists():
        cookie.unlink()
    tgbot._bridge_cookie_cache.update(mtime=0.0, cookie="")
    tgbot.BRIDGE_COOKIE_FILE = str(cookie)
    tgbot._client_for = lambda kind="main": FakeBridge() if (
        kind == "bridge" and tgbot.bridge_account_ready()) else FakeMain()
    tgbot.QuarkShare = FakeMain
    tgbot.QuarkGuest = FakeGuestAcct
    tgbot.QUARK_MODE = "bridge"
    tgbot.gopeed = FakeGopeed()
    tgbot.shutil.disk_usage = lambda p: Disk
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})
    FakeMain.saved.clear()
    FakeBridge.saved.clear()
    tgbot._member_cache["main"].update(ts=0.0, type="")
    tgbot._member_cache["bridge"].update(ts=0.0, type="")
    tgbot._quark_quota_cache["main"].update(ts=time.time(),
                                            total=FakeMain.total, used=FakeMain.used)
    tgbot._quark_quota_cache["bridge"].update(ts=time.time(),
                                              total=FakeBridge.total, used=FakeBridge.used)



def seed(items, pwd="pwddual"):
    """把文件写进队列，并让「重新扫描分享」返回同样的内容。"""
    for cls in (FakeMain, FakeBridge, FakeGuestAcct):
        cls.files = [{"fid": x["fid"], "name": x["name"], "size": x["size"],
                      "path": x["rel"], "share_fid_token": x["fid_token"]} for x in items]
    tgbot.queue_put(pwd, "分享_x", "2026-11-04", "", items)

async def case_split():
    setup(with_bridge=True)
    check(tgbot.bridge_account_ready(), "转存号已启用")
    seed([
        {"fid": "S1", "name": "small.bin", "size": 10 * MB, "rel": "small.bin",
         "fid_token": "T1"},
        {"fid": "B1", "name": "big.bin", "size": 100 * MB, "rel": "big.bin",
         "fid_token": "T2"},
    ])
    st = await tgbot.admit_wave(force=True)
    check(st["admitted"] == 2, "两个文件都建成任务", str(st))
    check(FakeBridge.saved == [["S1"]], "10MB 走了转存号", str(FakeBridge.saved))
    check(FakeMain.saved == [["B1"]], "100MB 走了主号", str(FakeMain.saved))
    urls = tgbot.gopeed.calls
    check(any("bridge/" in u for u in urls) and any("main/" in u for u in urls),
          "分别用两个账号的直链", str(urls))


async def case_no_bridge_account():
    """没配转存号时，全部落到主号。"""
    setup(with_bridge=False)
    check(not tgbot.bridge_account_ready(), "未配置转存号")
    seed([
        {"fid": "S1", "name": "small.bin", "size": 10 * MB, "rel": "small.bin",
         "fid_token": "T1"},
    ])
    st = await tgbot.admit_wave(force=True)
    check(st["admitted"] == 1, "照样能下", str(st))
    check(FakeMain.saved == [["S1"]], "落在主号", str(FakeMain.saved))


async def case_nonvip_main():
    """主号也是非会员时，大文件不该被转存（下不了，白占空间）。"""
    setup(with_bridge=True)
    FakeMain.member = "NORMAL"
    tgbot._member_cache["main"].update(ts=0.0, type="")
    seed([
        {"fid": "B1", "name": "big.bin", "size": 100 * MB, "rel": "big.bin",
         "fid_token": "T2"},
    ])
    st = await tgbot.admit_wave(force=True)
    FakeMain.member = "VIP"
    check(st["admitted"] == 0, "非会员下不了大文件 → 不转存", str(st))
    check(FakeMain.saved == [] and FakeBridge.saved == [],
          "谁都没白转存", f"{FakeMain.saved} {FakeBridge.saved}")
    left = sum(len(v.get("items") or []) for v in tgbot.load_queue().values())
    check(left == 1, "文件留在队列里", str(left))


async def main():
    async def noop(t):
        pass
    tgbot._notify = noop
    await case_split()
    await case_no_bridge_account()
    await case_nonvip_main()


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
