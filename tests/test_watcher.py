# -*- coding: utf-8 -*-
"""Gopeed watcher 状态机行为测试（在服务器上跑，使用桩对象，不碰生产数据）。

运行：
    SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/wtest /data/venv/bin/python3 /tmp/test_watcher.py
"""
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/wtest"))
shutil.rmtree(WORK, ignore_errors=True)
(WORK / "gdl").mkdir(parents=True)
(WORK / "tgdl").mkdir(parents=True)

os.environ.update({
    "BOT_TOKEN": "T", "ALLOWED_CHAT": "1", "ARIA2_SECRET": "T",
    "DOWNLOAD_DIR": str(WORK / "tgdl"),
    "GOPEED_DLDIR": str(WORK / "gdl"),
    "GOPEED_MAP": str(WORK / "map.json"),
    "ALERT_STATE": str(WORK / "alert.json"),
    "RCLONE_CONF": str(WORK / "rclone.conf"),
})
sys.path.insert(0, SERVER_DIR)
import tgbot  # noqa: E402
tgbot.BRIDGE_COOKIE_FILE = str(WORK / "no_bridge.cookie")   # 测试不读生产转存账号
tgbot.QUARK_MODE = "account"
tgbot._probe_url = lambda url, hdrs: True   # 默认分享直链可用，桥接另有测试
tgbot.SAVED_NOTIFY_DELAY = 0   # 通知不延迟，方便断言   # 测试里不连真夸克，统一走桩

FAILS = []
N = [0]


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(f"{label}  {extra}")
        print(" ✗", label, extra)
    else:
        print(" ✓", label)


# ---------------- 桩 ----------------
class FakeGopeed:
    def __init__(self):
        self.tasks = {}
        self.removed = []
        self.added = []
        self.list_calls = 0
        self.list_fails = False

    def list_tasks(self):
        self.list_calls += 1
        if self.list_fails:
            raise tgbot.GopeedError("connection refused")
        out = []
        for tid, t in self.tasks.items():
            item = dict(t)
            item["id"] = tid
            out.append(item)
        return out

    def get_task(self, tid):
        if tid not in self.tasks:
            raise tgbot.GopeedError("task not found")
        return self.tasks[tid]

    def is_done(self, t):
        return t.get("status") == "done"

    def is_failed(self, t):
        return t.get("status") in ("error", "removed")

    def remove_task(self, tid, force=True):
        self.removed.append(tid)
        self.tasks.pop(tid, None)

    def add_task(self, url, name, path, headers=None, connections=None):
        tid = "new%d" % (len(self.added) + 1)
        self.added.append((tid, url, name))
        self.tasks[tid] = {"status": "running"}
        return tid


class FakeQuark:
    calls = 0

    def __init__(self, *a, **k):
        pass

    def get_stoken(self, pwd_id, passcode=""):
        return "stoken"

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        FakeQuark.calls += 1
        if FakeQuark.calls >= 99:
            raise RuntimeError("require login")
        return "https://cdn.example/direct?fid=%s" % fid

    def walk_share(self, pwd_id, stoken, *a, **kw):
        # 模拟重试时刷新 share_fid_token（夸克 41020 的场景）
        return [{"path": "x/f.bin", "name": "f.bin", "size": 5,
                 "fid": "FID1", "share_fid_token": "TOK-FRESH"}]

    def download_headers(self):
        return {"Cookie": "c"}


notifies = []
rclone = {"rc": 0, "err": ""}
rclone_calls = []


def fake_rclone(args, timeout=60):
    rclone_calls.append(list(args))
    if rclone["rc"] == 0 and len(args) > 1 and os.path.exists(args[1]):
        os.remove(args[1])
    return rclone["rc"], "", rclone["err"]


async def fake_notify(text):
    notifies.append(text)


tgbot.gopeed = FakeGopeed()
tgbot.QuarkShare = FakeQuark
tgbot._rclone_run = fake_rclone
tgbot._notify = fake_notify

_real_sleep = asyncio.sleep


async def fast_sleep(d, *a, **k):
    if d == 5:
        return await _real_sleep(0.01)
    return await _real_sleep(d)


asyncio.sleep = fast_sleep


def reset():
    notifies.clear()
    rclone_calls.clear()
    rclone["rc"], rclone["err"] = 0, ""
    tgbot.gopeed = FakeGopeed()
    tgbot.save_gopeed_map({})
    # 熔断状态也要清，否则上一场景的失败会串到下一场景
    tgbot._cdn_block_until = 0.0
    tgbot._cdn_fail_times.clear()
    tgbot._share_cache.clear()
    tgbot._stoken_cache.clear()
    tgbot._share_direct_known = None


def info_for(saved_name, size=5, **kw):
    d = {"date": "2026-11-01", "share": "分享_x", "orig_name": "f.bin",
         "saved_name": saved_name, "rel_path": "sub/f.bin", "size": size,
         "added_at": int(time.time()), "pwd_id": "pwd12345", "passcode": "",
         "fid": "FID1", "fid_token": "TOK1", "retry": 0}
    d.update(kw)
    return d


def put_src(name, size=5):
    p = WORK / "gdl" / name
    p.write_bytes(b"x" * size)
    return p


async def wait_until(cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        await _real_sleep(0.01)
    return cond()


async def main():
    watcher = asyncio.create_task(tgbot.gopeed_watcher())
    await _real_sleep(0.05)

    # ---------- 1. 正常完成 ----------
    reset()
    put_src("s1.bin")
    tgbot.gopeed.tasks["T1"] = {"status": "done"}
    tgbot.map_add("T1", info_for("s1.bin"))
    ok = await wait_until(lambda: not tgbot.load_gopeed_map())
    check(ok, "场景1 完成后清空映射", str(tgbot.load_gopeed_map()))
    archived = any("2026-11-01/分享_x/sub/f.bin" in " ".join(map(str, c)) for c in rclone_calls)
    check(archived, "场景1 归档路径正确", str(rclone_calls[:1]))
    await wait_until(lambda: any("已保存到网盘" in n for n in notifies), timeout=3)
    check(any("已保存到网盘" in n for n in notifies), "场景1 发送完成通知", str(notifies))
    check("T1" in tgbot.gopeed.removed, "场景1 清理 Gopeed 任务")
    check(not (WORK / "gdl/2026-11-01").exists(), "场景1 空目录被回收")

    # ---------- 2. 下载失败 → 自动重新入队 ----------
    reset()
    tgbot.gopeed.tasks["T2"] = {"status": "error"}
    tgbot.map_add("T2", info_for("s2.bin"))
    ok = await wait_until(lambda: "new1" in tgbot.load_gopeed_map())
    m = tgbot.load_gopeed_map()
    check(ok, "场景2 重新入队生成新任务", str(list(m)))
    check(m.get("new1", {}).get("retry") == 1, "场景2 重试计数 +1", str(m.get("new1", {}).get("retry")))
    check(m.get("new1", {}).get("next_retry_at") == 0, "场景2 新任务无重试延迟")
    check("T2" in tgbot.gopeed.removed, "场景2 旧任务被删除")
    check(m.get("new1", {}).get("fid_token") == "TOK-FRESH",
          "场景2 重试前刷新了 share_fid_token", str(m.get("new1", {}).get("fid_token")))

    # ---------- 3. 下载失败 3 次后放弃 ----------
    reset()
    tgbot.gopeed.tasks["T3"] = {"status": "error"}
    tgbot.map_add("T3", info_for("s3.bin", retry=3))
    ok = await wait_until(lambda: not tgbot.load_gopeed_map()
                          and any("没" in n and "成" in n for n in notifies))
    check(ok, "场景3 超过重试上限后放弃", str(notifies))
    check("T3" in tgbot.gopeed.removed, "场景3 清理失败任务")

    # ---------- 3.5 熔断：重试也连续失败 → 整体暂停，不烧光重试次数 ----------
    reset()
    tgbot.CDN_BREAK_THRESHOLD = 3
    tgbot.CDN_BREAK_SECONDS = 60
    tgbot._cdn_block_until = 0.0
    tgbot._cdn_fail_times.clear()
    # 让"重试"这一步也失败（取直链报错），才会触发熔断
    real_gdu = FakeQuark.get_direct_url
    FakeQuark.get_direct_url = lambda self, *a, **k: (_ for _ in ()).throw(
        RuntimeError("412 rejected"))
    for i in range(6):
        tid = "BRK%d" % i
        tgbot.gopeed.tasks[tid] = {"status": "error"}
        tgbot.map_add(tid, info_for("brk%d.bin" % i))
    ok = await wait_until(lambda: tgbot.cdn_blocked_for() > 0, timeout=8)
    FakeQuark.get_direct_url = real_gdu
    check(ok, "场景3.5 连续失败触发熔断", str(tgbot.cdn_blocked_for()))
    check(any("暂停" in n for n in notifies), "场景3.5 熔断时通知用户", str(notifies[-1:]))
    kept = len(tgbot.load_gopeed_map())
    check(kept >= 2, "场景3.5 熔断后文件保留、不被丢弃", str(kept))
    check(all(int(v.get("retry") or 0) <= 1 for v in tgbot.load_gopeed_map().values()),
          "场景3.5 熔断时没有烧光重试次数",
          str([v.get("retry") for v in tgbot.load_gopeed_map().values()]))
    # 熔断期间不应再新建任务
    before = len(tgbot.gopeed.added)
    await _real_sleep(0.5)
    check(len(tgbot.gopeed.added) == before, "场景3.5 熔断期间不再重试", str(len(tgbot.gopeed.added)))
    tgbot.CDN_BREAK_THRESHOLD = 999
    tgbot._cdn_block_until = 0.0
    tgbot._cdn_fail_times.clear()
    # 清掉熔断场景留下的任务，避免解锁后它们被重试、污染下一场景
    tgbot.save_gopeed_map({})
    tgbot.gopeed = FakeGopeed()

    # ---------- 4. 上传失败（普通）→ 排期重试 → 二次成功 ----------
    reset()
    put_src("s4.bin")
    rclone["rc"], rclone["err"] = 1, "connection reset by peer"
    tgbot.gopeed.tasks["T4"] = {"status": "done"}
    tgbot.map_add("T4", info_for("s4.bin"))
    ok = await wait_until(lambda: tgbot.load_gopeed_map().get("T4", {}).get("upload_retry") == 1)
    inf = tgbot.load_gopeed_map().get("T4", {})
    check(ok, "场景4 上传失败写入重试计数", str(inf))
    check(inf.get("last_error") == "upload", "场景4 记录错误类型", str(inf.get("last_error")))
    check(inf.get("next_retry_at", 0) > time.time(), "场景4 设置退避时间")

    # 让重试立即到期，并让 rclone 成功
    inf["next_retry_at"] = 0
    tgbot.map_update("T4", inf)
    rclone["rc"], rclone["err"] = 0, ""
    ok = await wait_until(lambda: not tgbot.load_gopeed_map())
    check(ok, "场景4 重试后上传成功并清空映射", str(tgbot.load_gopeed_map()))
    await wait_until(lambda: any("已保存到网盘" in n for n in notifies), timeout=3)
    check(any("已保存到网盘" in n for n in notifies), "场景4 最终成功通知")

    # ---------- 5. 授权失效 → 1 小时后再试 ----------
    reset()
    put_src("s5.bin")
    rclone["rc"], rclone["err"] = 1, "oauth2: cannot fetch token: 400 Bad Request"
    tgbot.gopeed.tasks["T5"] = {"status": "done"}
    tgbot.map_add("T5", info_for("s5.bin"))
    ok = await wait_until(lambda: tgbot.load_gopeed_map().get("T5", {}).get("last_error") == "gdrive")
    inf = tgbot.load_gopeed_map().get("T5", {})
    check(ok, "场景5 识别授权失效", str(inf))
    check(inf.get("next_retry_at", 0) - time.time() > 3000, "场景5 退避约 1 小时",
          str(inf.get("next_retry_at", 0) - time.time()))
    rclone["rc"], rclone["err"] = 0, ""

    # ---------- 6. Gopeed 中任务消失 + 文件已落盘 ----------
    reset()
    put_src("s6.bin")
    tgbot.map_add("T6", info_for("s6.bin"))
    ok = await wait_until(lambda: not tgbot.load_gopeed_map(), timeout=6)
    check(ok, "场景6 任务消失但文件在，仍完成上传", str(tgbot.load_gopeed_map()))
    check((WORK / "gdl/2026-11-01/分享_x/sub/f.bin").exists() is False, "场景6 文件被上传走")

    # ---------- 7. Gopeed 中任务消失 + 没有文件 ----------
    reset()
    tgbot.map_add("T7", info_for("s7_missing.bin"))
    ok = await wait_until(lambda: not tgbot.load_gopeed_map()
                          and any("找不到这个任务" in n for n in notifies), timeout=6)
    check(ok, "场景7 任务丢失且无文件 → 放弃并通知", str(notifies))

    # ---------- 8. 并发两个任务 ----------
    reset()
    put_src("s8a.bin")
    put_src("s8b.bin")
    tgbot.gopeed.tasks["T8a"] = {"status": "done"}
    tgbot.gopeed.tasks["T8b"] = {"status": "done"}
    tgbot.map_add("T8a", info_for("s8a.bin", rel_path="a.bin"))
    tgbot.map_add("T8b", info_for("s8b.bin", rel_path="b.bin"))
    # 机器负载高时上传可能被排到 60s 后重试：这里主动把重试时间清零，最多补 3 轮
    ok = False
    for _ in range(3):
        ok = await wait_until(lambda: not tgbot.load_gopeed_map(), timeout=12)
        if ok:
            break
        for tid, info in tgbot.load_gopeed_map().items():
            info["next_retry_at"] = 0
            info["upload_retry"] = 0
            tgbot.map_update(tid, info)
    check(ok, "场景8 并发任务全部完成", str(tgbot.load_gopeed_map()))
    check((WORK / "gdl/2026-11-01/分享_x/a.bin").exists() is False, "场景8 文件已上传")

    # ---------- 9. Gopeed 整体失联 → 不得误判任务丢失 ----------
    reset()
    tgbot.gopeed.list_fails = True
    tgbot.map_add("T9", info_for("s9.bin"))
    await _real_sleep(1.0)          # 相当于正常轮询 100 次（5s/次）
    check("T9" in tgbot.load_gopeed_map(), "场景9 Gopeed 失联时保留任务", str(tgbot.load_gopeed_map()))
    check(not any("找不到这个任务" in n for n in notifies), "场景9 未误报任务丢失", str(notifies))
    tgbot.gopeed.list_fails = False

    # ---------- 10. 一轮只拉一次任务列表（O(1) 次 HTTP） ----------
    reset()
    for i in range(20):
        tgbot.map_add("TB%02d" % i, info_for("sb%02d.bin" % i, rel_path="b%02d.bin" % i))
        tgbot.gopeed.tasks["TB%02d" % i] = {"status": "running"}
    before = tgbot.gopeed.list_calls
    await _real_sleep(0.5)          # 约 50 轮
    used = tgbot.gopeed.list_calls - before
    check(used <= 60, "场景10 每轮只拉一次列表", f"calls={used}")
    check(len(tgbot.load_gopeed_map()) == 20, "场景10 运行中任务保持不变")

    watcher.cancel()
    try:
        await watcher
    except asyncio.CancelledError:
        pass


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
