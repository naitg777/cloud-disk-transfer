# -*- coding: utf-8 -*-
"""大分享规模测试：几千个文件时的时间/内存/批次行为。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/scale /data/venv/bin/python3 test_scale.py
"""
import asyncio
import os
import resource
import shutil
import sys
import time
import tracemalloc
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/scale"))
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

# 这些用例不测自检，直接让它通过（自检本身由 test_preflight 覆盖）
async def _pf_common_stub():
    return True, ["✅ 自检（测试桩）"]


async def _pf_share_stub(q, st, files, pwd_id=""):
    return "ok", ["✅ 分享（测试桩）"], len(files or []), 0


tgbot.preflight_common = _pf_common_stub
tgbot.preflight_share = _pf_share_stub

tgbot.BRIDGE_COOKIE_FILE = str(WORK / "no_bridge.cookie")   # 测试不读生产转存账号
tgbot._probe_url = lambda url, hdrs: True   # 默认分享直链可用，桥接另有测试

FAILS, N = [], [0]
MB = 1024 ** 2


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(label)
        print(" ✗", label, extra)
    else:
        print(" ✓", label, extra if extra else "")


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
        self.added = []

    def add_task(self, url, name, path, headers=None, connections=None):
        self.added.append(name)
        return "tid%d" % len(self.added)

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
        return "bigshare001", ""

    def get_stoken(self, pwd_id, passcode=""):
        return "st"

    def walk_share(self, pwd_id, stoken, *a, **kw):
        limit = a[4] if len(a) >= 5 else None
        return FakeQuark.files[:limit] if limit else FakeQuark.files

    def download_headers(self):
        return {}

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        return "https://cdn/%s" % fid


class Disk:
    def __init__(self, free):
        self.total = 100 * 1024 ** 3
        self.free = free
        self.used = self.total - free


async def fake_notify(t):
    pass


def make_files(n, size=3 * MB):
    return [{"path": "合集/第%03d集.bin" % i, "name": "第%03d集.bin" % i,
             "size": size, "fid": "F%05d" % i, "share_fid_token": "T%05d" % i}
            for i in range(n)]


async def case_big_share(n=5000):
    tgbot.gopeed = FakeGopeed()
    tgbot.QuarkShare = FakeQuark
    tgbot.QUARK_MODE = "account"
    tgbot._share_cache.clear()
    tgbot._stoken_cache.clear()
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})
    tgbot.shutil.disk_usage = lambda p: Disk(20 * 1024 ** 3)
    FakeQuark.files = make_files(n)

    tracemalloc.start()
    t0 = time.time()
    up = Update()
    await tgbot.handle_quark_share(up, None, "https://pan.quark.cn/s/bigshare001")
    dt = time.time() - t0
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    m = tgbot.load_gopeed_map()
    q = tgbot.load_queue()
    queued = sum(len(v["items"]) for v in q.values())
    print(f"   {n} 文件：处理 {dt:.2f}s，放行 {len(m)}，排队 {queued}，内存峰值 {peak/MB:.1f} MB")
    check(dt < 30, f"{n} 文件解析+入队不超 30 秒", f"{dt:.2f}s")
    check(len(m) + queued == n, "文件数与放行+排队一致", f"{len(m)}+{queued} vs {n}")
    check(len(m) <= tgbot.MAX_WAVE_FILES, "首波不超过单波上限", str(len(m)))
    check("边传边下" in (up.sink[-1] or "") or "自动接着" in (up.sink[-1] or ""),
          "提示里说明了分批", (up.sink[-1] or "")[:60])
    check(peak < 200 * MB, "内存峰值 < 200MB", f"{peak/MB:.0f} MB")
    return queued


async def case_wave_speed():
    """连续放行几波，看单波耗时（应该远小于一次重扫分享）。"""
    tgbot._share_cache.clear()
    tgbot._stoken_cache.clear()
    times = []
    for _ in range(3):
        tgbot.save_gopeed_map({})
        t0 = time.time()
        st = await tgbot.admit_wave()
        times.append(time.time() - t0)
        if st["left"] == 0:
            break
    print("   每波耗时:", ", ".join(f"{t*1000:.0f}ms" for t in times))
    check(all(t < 10 for t in times), "每波放行耗时 < 10s", str([round(t, 2) for t in times]))


async def case_limit_enforced():
    """超过上限的文件数应该在扫到 +1 时就停，而不是扫完再拒绝。"""
    tgbot._share_cache.clear()
    tgbot._stoken_cache.clear()
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})
    tgbot.MAX_SHARE_FILES = 200
    FakeQuark.files = make_files(5000)
    up = Update()
    t0 = time.time()
    await tgbot.handle_quark_share(up, None, "https://pan.quark.cn/s/bigshare001")
    dt = time.time() - t0
    check("超过单次上限" in (up.sink[-1] or ""), "超限时明确拒绝", (up.sink[-1] or "")[:40])
    check(len(tgbot.load_gopeed_map()) == 0, "超限时一个都不建", str(len(tgbot.load_gopeed_map())))
    print(f"   超限判定耗时 {dt:.3f}s")
    tgbot.MAX_SHARE_FILES = 20000


async def case_queue_file_size(n):
    """队列文件大小与写盘耗时（每波都会重写）。"""
    tgbot._share_cache.clear()
    tgbot._stoken_cache.clear()
    tgbot.save_queue({})
    items = [{"fid": "F%05d" % i, "name": "第%05d集.bin" % i, "size": 3 * MB,
              "rel": "合集/第%05d集.bin" % i, "fid_token": "T%05d" % i}
             for i in range(n)]
    tgbot.queue_put("bigshare001", "合集_x", "2026-11-03", "", items)
    p = Path(tgbot.QUEUE_FILE)
    size = p.stat().st_size
    t0 = time.time()
    tgbot.save_queue(tgbot.load_queue())
    dt = time.time() - t0
    print(f"   {n} 条队列：文件 {size/1024:.0f} KB，写入 {dt*1000:.0f}ms")
    check(dt < 2.0, f"{n} 条队列写入 < 2s", f"{dt:.3f}s")


async def case_boundary():
    """正好等于上限应该收；多一个就拒绝。"""
    tgbot.MAX_SHARE_FILES = 200
    for n, should_pass, label in ((200, True, "恰好 200 个（等于上限）"),
                                  (201, False, "201 个（超一个）")):
        tgbot._share_cache.clear()
        tgbot._stoken_cache.clear()
        tgbot.save_gopeed_map({})
        tgbot.save_queue({})
        tgbot.gopeed = FakeGopeed()
        FakeQuark.files = make_files(n)
        up = Update()
        await tgbot.handle_quark_share(up, None, "https://pan.quark.cn/s/bigshare001")
        got = len(tgbot.load_gopeed_map()) + sum(
            len(v["items"]) for v in tgbot.load_queue().values())
        if should_pass:
            check(got == n, f"{label} 正常入队", f"{got}/{n}")
        else:
            check(got == 0 and "超过单次上限" in (up.sink[-1] or ""),
                  f"{label} 被拒绝", f"{got} | {(up.sink[-1] or '')[:30]}")
    tgbot.MAX_SHARE_FILES = 20000


async def main():
    await case_big_share(5000)
    await case_wave_speed()
    await case_limit_enforced()
    await case_boundary()
    await case_queue_file_size(5000)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"   进程峰值 RSS {rss:.0f} MB")
    check(rss < 400, "进程内存 < 400MB")


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
