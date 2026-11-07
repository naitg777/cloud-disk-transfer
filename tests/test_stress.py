# -*- coding: utf-8 -*-
"""鲁棒性压测：大批量任务、混合状态、并发分享、异常恢复、循环稳定性。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/stest /data/venv/bin/python3 /tmp/test_stress.py
"""
import asyncio
import json
import os
import shutil
import sys
import time
import tracemalloc
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/stest"))
shutil.rmtree(WORK, ignore_errors=True)
for sub in ("gdl", "tgdl", "logs"):
    (WORK / sub).mkdir(parents=True)

os.environ.update({
    "BOT_TOKEN": "T", "ALLOWED_CHAT": "42", "ARIA2_SECRET": "T",
    "DOWNLOAD_DIR": str(WORK / "tgdl"),
    "GOPEED_DLDIR": str(WORK / "gdl"),
    "GOPEED_MAP": str(WORK / "logs" / "map.json"),
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
tgbot.QUARK_MODE = "account"
tgbot._probe_url = lambda url, hdrs: True   # 默认分享直链可用，桥接另有测试
tgbot.SAVED_NOTIFY_DELAY = 0.5   # 通知不延迟，方便断言   # 测试里不连真夸克，统一走桩

FAILS, N = [], [0]


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(f"{label} {extra}")
        print(" ✗", label, extra)
    else:
        print(" ✓", label)


class FakeGopeed:
    def __init__(self):
        self.tasks = {}
        self.list_calls = 0
        self.removed = []
        self.added = []

    def list_tasks(self):
        self.list_calls += 1
        out = []
        for tid, t in self.tasks.items():
            item = dict(t)
            item["id"] = tid
            out.append(item)
        return out

    def is_done(self, t):
        return t.get("status") == "done"

    def is_failed(self, t):
        return t.get("status") in ("error", "removed")

    def remove_task(self, tid, force=True):
        self.removed.append(tid)
        self.tasks.pop(tid, None)

    def add_task(self, url, name, path, headers=None, connections=None):
        tid = "re%s" % (len(self.added) + 1)
        self.added.append(tid)
        self.tasks[tid] = {"status": "running"}
        return tid


class FakeQuark:
    def __init__(self, *a, **k):
        pass

    def get_stoken(self, pwd_id, passcode=""):
        return "st"

    def walk_share(self, pwd_id, stoken, *a, **kw):
        return []

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        return "https://cdn/%s" % fid

    def download_headers(self):
        return {}


notifies, rclone_calls = [], []


async def fake_notify(t):
    notifies.append(t)


def fake_rclone(args, timeout=60):
    rclone_calls.append(list(args))
    if len(args) > 1 and os.path.exists(args[1]):
        os.remove(args[1])
    return 0, "", ""


tgbot.gopeed = FakeGopeed()
tgbot.QuarkShare = FakeQuark
tgbot._rclone_run = fake_rclone
tgbot._notify = fake_notify

_real_sleep = asyncio.sleep


async def fast_sleep(d, *a, **k):
    return await _real_sleep(0.01 if d == 5 else d)


asyncio.sleep = fast_sleep


def info_for(name, size=5, **kw):
    d = {"date": "2026-11-01", "share": "压测", "orig_name": name,
         "saved_name": name, "rel_path": name, "size": size,
         "added_at": int(time.time()), "pwd_id": "pwd12345", "passcode": "",
         "fid": "F" + name, "fid_token": "TOK", "retry": 0}
    d.update(kw)
    return d


async def wait_until(cond, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        await _real_sleep(0.01)
    return cond()


async def main():
    tracemalloc.start()
    watcher = asyncio.create_task(tgbot.gopeed_watcher())
    await _real_sleep(0.05)

    # ---------- 1. 200 个完成态任务一次性消化 ----------
    tgbot.save_gopeed_map({})
    tgbot.gopeed = FakeGopeed()
    t0 = time.time()
    for i in range(200):
        name = "bulk%03d.bin" % i
        (WORK / "gdl" / name).write_bytes(b"x" * 5)
        tgbot.gopeed.tasks["T%03d" % i] = {"status": "done"}
        tgbot.map_add("T%03d" % i, info_for(name))
    ok = await wait_until(lambda: not tgbot.load_gopeed_map(), timeout=40)
    elapsed = time.time() - t0
    check(ok, "200 个任务全部消化", f"剩余 {len(tgbot.load_gopeed_map())}，耗时 {elapsed:.1f}s")
    # 成功通知会合并发送：只看计数器，顺便确认没有被刷成 200 条
    await wait_until(lambda: tgbot._saved_count >= 200, timeout=10)
    check(tgbot._saved_count >= 200, "200 个文件都记了保存", str(tgbot._saved_count))
    await wait_until(lambda: len(notifies) >= 1, timeout=6)
    check(1 <= len(notifies) < 200, "大分享通知已合并发送", str(len(notifies)))
    check(elapsed < 35, "200 任务在 35s 内完成", f"{elapsed:.1f}s")
    # 每轮只拉一次列表，而不是每任务一次
    cycles_est = elapsed / 0.01
    check(tgbot.gopeed.list_calls < 200 + 50,
          "轮询调用次数与任务数无关", f"list_calls={tgbot.gopeed.list_calls}, 任务=200")
    check(not any((WORK / "gdl").rglob("bulk*.bin")), "本地文件全部清空")

    # ---------- 2. 混合状态：运行中 / 失败 / 文件缺失 ----------
    tgbot.save_gopeed_map({})
    tgbot.gopeed = FakeGopeed()
    notifies.clear()
    tgbot._saved_count = 0
    # 本章节专门测"失败重试"，把新的熔断机制关掉（熔断另有测试）
    tgbot.CDN_BREAK_THRESHOLD = 999
    tgbot._cdn_block_until = 0.0
    tgbot._cdn_fail_times.clear()
    for i in range(20):
        tgbot.map_add("RUN%02d" % i, info_for("run%02d.bin" % i))
        tgbot.gopeed.tasks["RUN%02d" % i] = {"status": "running"}
    for i in range(5):
        tgbot.map_add("ERR%02d" % i, info_for("err%02d.bin" % i))
        tgbot.gopeed.tasks["ERR%02d" % i] = {"status": "error"}
    for i in range(5):
        tgbot.map_add("GONE%02d" % i, info_for("gone%02d.bin" % i))
    await _real_sleep(0.35)
    m = tgbot.load_gopeed_map()
    check(len([k for k in m if k.startswith("RUN")]) == 20, "运行中任务保持不变",
          str(len([k for k in m if k.startswith("RUN")])))
    check(len([k for k in m if k.startswith("re")]) == 5, "失败任务已重新入队",
          str(sorted(k for k in m if k.startswith("re"))))
    check(len([k for k in m if k.startswith("GONE")]) == 5, "消失任务在阈值内保留",
          str(len([k for k in m if k.startswith("GONE")])))

    # ---------- 3. Gopeed 列表接口异常恢复 ----------
    class FlakyGopeed(FakeGopeed):
        def list_tasks(self):
            self.list_calls += 1
            if self.list_calls % 2 == 0:
                raise tgbot.GopeedError("boom")
            return super().list_tasks()

    tgbot.gopeed = FlakyGopeed()
    tgbot.gopeed.tasks["FLAKY1"] = {"status": "done"}
    (WORK / "gdl" / "flaky.bin").write_bytes(b"x" * 5)
    tgbot.map_add("FLAKY1", info_for("flaky.bin"))
    ok = await wait_until(lambda: "FLAKY1" not in tgbot.load_gopeed_map(), timeout=5)
    check(ok, "列表接口间歇失败仍能推进任务")

    # ---------- 4. map 文件损坏后自动恢复 ----------
    tgbot.GOPEED_MAP.write_text("{corrupted", encoding="utf-8")
    tgbot.map_add("NEW1", info_for("new1.bin"))
    check(tgbot.load_gopeed_map().keys() == {"NEW1"}, "映射损坏后仍可写入",
          str(tgbot.load_gopeed_map().keys()))

    # ---------- 5. 0 字节文件 ----------
    tgbot.save_gopeed_map({})
    tgbot.gopeed = FakeGopeed()
    (WORK / "gdl" / "empty.bin").write_bytes(b"")
    tgbot.gopeed.tasks["EMPTY1"] = {"status": "done"}
    tgbot.map_add("EMPTY1", info_for("empty.bin", size=0))
    ok = await wait_until(lambda: not tgbot.load_gopeed_map(), timeout=5)
    check(ok, "0 字节文件可正常归档上传")

    # ---------- 6. 长时间运行的内存/结构稳定性 ----------
    tgbot.save_gopeed_map({})
    tgbot.gopeed = FakeGopeed()
    for i in range(50):
        tgbot.map_add("LONG%02d" % i, info_for("long%02d.bin" % i))
        tgbot.gopeed.tasks["LONG%02d" % i] = {"status": "running"}
    await _real_sleep(1.0)
    snap1 = tracemalloc.take_snapshot()
    await _real_sleep(2.0)
    snap2 = tracemalloc.take_snapshot()
    growth = sum(s.size_diff for s in snap2.compare_to(snap1, "filename"))
    check(growth < 3 * 1024 * 1024, "3 秒空转内存增长 < 3MB", f"{growth/1024:.0f} KB")
    check(len(tgbot.load_gopeed_map()) == 50, "运行中映射未被误改")

    watcher.cancel()
    try:
        await watcher
    except asyncio.CancelledError:
        pass


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
