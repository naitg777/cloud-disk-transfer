# -*- coding: utf-8 -*-
"""大分享分批下载（波次队列）测试。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/bt /data/venv/bin/python3 test_batch.py
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/bt"))
shutil.rmtree(WORK, ignore_errors=True)
for sub in ("gdl", "tgdl", "logs"):
    (WORK / sub).mkdir(parents=True)

os.environ.update({
    "BOT_TOKEN": "T", "ALLOWED_CHAT": "42", "ARIA2_SECRET": "T",
    "DOWNLOAD_DIR": str(WORK / "tgdl"),
    "GOPEED_DLDIR": str(WORK / "gdl"),
    "GOPEED_MAP": str(WORK / "logs" / "map.json"),
    "GOPEED_QUEUE": str(WORK / "logs" / "queue.json"),
    "ALERT_STATE": str(WORK / "logs" / "alert.json"),
    "RCLONE_CONF": str(WORK / "rclone.conf"),
})
sys.path.insert(0, SERVER_DIR)
import tgbot  # noqa: E402

# 这些用例不测自检，直接让它通过（自检本身由 test_preflight 覆盖）
async def _pf_common_stub():
    return True, ["✅ 自检（测试桩）"]


async def _pf_share_stub(q, st, files, pwd_id=""):
    return "ok", ["✅ 分享（测试桩）"], len(files or []), 0


tgbot.preflight_common = _pf_common_stub
tgbot.preflight_share = _pf_share_stub

tgbot.BRIDGE_COOKIE_FILE = str(WORK / "no_bridge.cookie")   # 测试不读生产转存账号

FAILS, N = [], [0]
GB = 1024 ** 3


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(label)
        print(" ✗", label, extra)
    else:
        print(" ✓", label)


class Msg:
    def __init__(self, sink):
        self.sink = sink

    async def edit_text(self, text, **kw):
        self.sink.append(text)
        return self

    async def reply_text(self, text, **kw):
        self.sink.append(text)
        return self


class Chat:
    id = 42


class Update:
    def __init__(self):
        self.sink = []
        self.effective_chat = Chat()
        self.message = Msg(self.sink)


class FakeGopeed:
    def __init__(self):
        self.n = 0
        self.names = []

    def add_task(self, url, name, path, headers=None, connections=None):
        self.n += 1
        self.names.append(name)
        return "tid%02d" % self.n

    def list_tasks(self):
        return []

    def remove_task(self, tid, force=True):
        pass


class FakeQuark:
    files = []

    def __init__(self, *a, **k):
        pass

    @staticmethod
    def parse_link(t):
        return "d02af8share", ""

    def get_stoken(self, pwd_id, passcode=""):
        return "st"

    def walk_share(self, pwd_id, stoken, *a, **kw):
        return FakeQuark.files

    def quota(self):
        return 100 * 1024 ** 3, 10 * 1024 ** 3      # 假装夸克空间充足

    def download_headers(self):
        return {}

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        return "https://cdn/%s" % fid


class Disk:
    def __init__(self, free):
        self.total = 100 * GB
        self.used = self.total - free
        self.free = free


notifies = []


async def fake_notify(t):
    notifies.append(t)


tgbot.gopeed = FakeGopeed()
tgbot.QUARK_MODE = "account"
tgbot._probe_url = lambda url, hdrs: True   # 默认分享直链可用，桥接另有测试   # 测试里强制走账号桩，避免真去连夸克
tgbot.QuarkShare = FakeQuark
tgbot._notify = fake_notify


def make_files(sizes):
    out = []
    for i, s in enumerate(sizes):
        out.append({"path": "包/f%d.bin" % i, "name": "f%d.bin" % i,
                    "size": s, "fid": "F%d" % i, "share_fid_token": "T%d" % i})
    return out


# ---------------- 1. plan_wave 纯函数 ----------------
def case_plan():
    items = [{"size": s} for s in (1 * GB, 1 * GB, 1 * GB)]
    wave, rest = tgbot.plan_wave(items, int(1.8 * GB), 3 * GB)
    check(len(wave) == 1 and len(rest) == 2, "plan_wave 按容量切波", f"{len(wave)}/{len(rest)}")

    # 单个文件超过 capacity 但没超过 hard_limit → 允许单独成波
    wave, rest = tgbot.plan_wave([{"size": 2 * GB}], int(1.8 * GB), 3 * GB)
    check(len(wave) == 1, "plan_wave 大文件单独成波")

    # 超过 hard_limit → 不进波
    wave, rest = tgbot.plan_wave([{"size": 10 * GB}], int(1.8 * GB), 3 * GB)
    check(len(wave) == 0 and len(rest) == 1, "plan_wave 超硬上限不放进波")


async def case_batch_flow():
    # 6 个 1GB 文件，可用余量 5GB（预算 1.8GB / 硬上限 3GB）
    tgbot._share_cache.clear()
    tgbot._stoken_cache.clear()
    tgbot.shutil.disk_usage = lambda p: Disk(5 * GB)
    tgbot.gopeed = FakeGopeed()
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})
    FakeQuark.files = make_files([1 * GB] * 6)

    up = Update()
    await tgbot.handle_quark_share(up, None, "https://pan.quark.cn/s/d02af8share")
    m = tgbot.load_gopeed_map()
    q = tgbot.load_queue()

    check(len(m) == 1, "第一批只放 1 个进下载器", str(len(m)))
    check(len(q.get("d02af8share", {}).get("items", [])) == 5,
          "其余 5 个进队列", str(len(q.get("d02af8share", {}).get("items", []))))
    check("同一个文件夹" in (up.sink[-1] or ""), "提示里说明会放进同一文件夹", up.sink[-1][-160:])
    share = list(m.values())[0]["share"]
    date = list(m.values())[0]["date"]

    # 模拟第一个文件下载完并上传（map 清空），再续一波
    tgbot.save_gopeed_map({})
    stats = await tgbot.admit_wave()
    check(stats["admitted"] == 1, "续波再放 1 个", str(stats))
    m2 = tgbot.load_gopeed_map()
    check(len(m2) == 1 and list(m2.values())[0]["share"] == share
          and list(m2.values())[0]["date"] == date,
          "续波的文件落在同一个归档目录",
          str({k: (v["share"], v["date"]) for k, v in m2.items()}))

    # 把剩余全部走完
    for _ in range(10):
        tgbot.save_gopeed_map({})
        st = await tgbot.admit_wave()
        if not st["admitted"] and not st["left"]:
            break
        if st["left"] == 0:
            break
    check(tgbot.load_queue() == {}, "全部放行后队列清空", str(tgbot.load_queue()))

    # 磁盘不够时不放行
    tgbot.shutil.disk_usage = lambda p: Disk(2 * GB)   # 余量刚好等于安全线
    tgbot.save_queue({"x": {"share": "s", "date": "2026-11-02", "passcode": "",
                            "items": [{"fid": "Z1", "name": "z.bin", "size": 100,
                                       "rel": "z.bin", "fid_token": "t"}]}})
    st = await tgbot.admit_wave(force=True)
    check(st["admitted"] == 0, "没有余量时不放行", str(st))


async def case_oversize():
    tgbot._share_cache.clear()
    tgbot._stoken_cache.clear()
    tgbot.shutil.disk_usage = lambda p: Disk(5 * GB)
    tgbot.gopeed = FakeGopeed()
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})
    # 1 个 10GB（超过硬上限 3GB）+ 2 个 1GB
    FakeQuark.files = make_files([10 * GB, 1 * GB, 1 * GB])
    up = Update()
    await tgbot.handle_quark_share(up, None, "https://pan.quark.cn/s/d02af8share")
    m = tgbot.load_gopeed_map()
    q = tgbot.load_queue()
    check(len(m) == 1, "超大文件被跳过，正常文件照常下", str(len(m)))
    check("太大" in (up.sink[-1] or ""), "提示里说明有文件太大被跳过", up.sink[-1][-200:])
    total_q = sum(len(v.get("items", [])) for v in q.values())
    check(total_q == 1, "剩余 1 个正常文件在队列", str(total_q))
    tgbot.shutil.disk_usage = shutil.disk_usage


async def main():
    case_plan()
    await case_batch_flow()
    await case_oversize()


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
