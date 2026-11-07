# -*- coding: utf-8 -*-
"""转存桥接模式：分享直链被夸克拒（412）后，改为「转存到自己网盘再下载」。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/bt2 /data/venv/bin/python3 test_bridge.py
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/bt2"))
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
MB = 1024 ** 2


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


class FakeQuark:
    """分享直链永远 412，但转存 + 自己网盘直链正常。"""

    files = [
        {"path": "a.bin", "name": "a.bin", "size": 80 * MB, "fid": "F1",
         "share_fid_token": "T1"},
        {"path": "b.bin", "name": "b.bin", "size": 90 * MB, "fid": "F2",
         "share_fid_token": "T2"},
    ]
    saved = []          # 记录转存过的 fid
    deleted = []        # 记录删掉的 own_fid
    own_seq = 0

    def __init__(self, *a, **k):
        pass

    @staticmethod
    def parse_link(t):
        return "pwd123a", ""

    def get_stoken(self, pwd_id, passcode=""):
        return "st"

    def walk_share(self, pwd_id, stoken, *a, **kw):
        limit = a[4] if len(a) >= 5 else None
        return FakeQuark.files[:limit] if limit else FakeQuark.files

    def member_info(self):
        return {"member_type": "VIP", "total_capacity": 100 * 1024 ** 3,
                "use_capacity": 10 * 1024 ** 3}

    def quota(self):
        return 100 * 1024 ** 3, 10 * 1024 ** 3      # 假装夸克空间充足

    def download_headers(self):
        return {"Cookie": "account=1"}

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        return "https://dl-pc-zb/%s" % fid      # 这个会被 probe 判为不可用

    # ---- 转存相关 ----
    def ensure_dir(self, name, pdir_fid="0"):
        return "BRIDGE_FID"

    def save(self, pwd_id, stoken, fids, tokens, to_pdir_fid="0"):
        FakeQuark.saved.append(list(fids))
        return "task1"

    def wait_task_fids(self, task_id, timeout=300, interval=2.0):
        out = []
        for _ in FakeQuark.saved[-1]:
            FakeQuark.own_seq += 1
            out.append("OWN%d" % FakeQuark.own_seq)
        return out

    def own_direct_url(self, own_fid):
        return "https://dl-pc-zb/own/%s" % own_fid

    def delete(self, fids):
        FakeQuark.deleted.extend(fids)


class Disk:
    total = 100 * 1024 ** 3
    free = 50 * 1024 ** 3
    used = 50 * 1024 ** 3


async def case_bridge_flow():
    tgbot.gopeed = FakeGopeed()
    tgbot.QuarkShare = FakeQuark
    tgbot.QUARK_MODE = "auto"
    tgbot._share_cache.clear()
    tgbot._share_direct_known = None
    tgbot.shutil.disk_usage = lambda p: Disk
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})
    # 分享直链一律不可用
    tgbot._probe_url = lambda url, hdrs: False

    tgbot.queue_put("pwd123a", "分享_x", "2026-11-04", "", [
        {"fid": "F1", "name": "a.bin", "size": 80 * MB, "rel": "a.bin",
         "fid_token": "T1"},
        {"fid": "F2", "name": "b.bin", "size": 90 * MB, "rel": "b.bin",
         "fid_token": "T2"},
    ])
    stats = await tgbot.admit_wave(force=True)
    m = tgbot.load_gopeed_map()

    check(stats["admitted"] == 2, "直链被拒后仍然建成任务", str(stats))
    check(FakeQuark.saved and sorted(FakeQuark.saved[-1]) == ["F1", "F2"],
          "整波一次性转存", str(FakeQuark.saved))
    check(all(v.get("own_fid", "").startswith("OWN") for v in m.values()),
          "映射里记了中转文件 fid", str([v.get("own_fid") for v in m.values()]))
    urls = [c[0] for c in tgbot.gopeed.calls]
    check(all("/own/" in u for u in urls), "下载用的是自己网盘直链", str(urls))
    check(tgbot._share_direct_known is False, "记住了『分享直链不可用』")

    # 上传成功 → 删除中转副本
    info = list(m.values())[0]
    own_fid = info["own_fid"]
    FakeQuark.deleted.clear()
    await tgbot._drop_bridge_copy(info)
    check(FakeQuark.deleted == [own_fid], "下载完删掉中转副本", str(FakeQuark.deleted))
    check(info["own_fid"] == "", "删完把 own_fid 清掉（避免重复删）")


async def case_direct_still_used():
    """分享直链可用时不应该转存（省网盘空间）。"""
    tgbot.gopeed = FakeGopeed()
    FakeQuark.saved.clear()
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})
    tgbot._share_cache.clear()
    tgbot._set_share_direct(True)
    tgbot.queue_put("pwd123a", "分享_y", "2026-11-04", "", [
        {"fid": "F1", "name": "a.bin", "size": 10 * MB, "rel": "a.bin",
         "fid_token": "T1"},
    ])
    stats = await tgbot.admit_wave(force=True)
    check(stats["admitted"] == 1, "直链可用时正常建任务", str(stats))
    check(not FakeQuark.saved, "直链可用时不转存", str(FakeQuark.saved))
    check(tgbot.gopeed.calls[0][0].startswith("https://dl-pc-zb/F1"),
          "用的是分享直链", tgbot.gopeed.calls[0][0])
    check(not list(tgbot.load_gopeed_map().values())[0].get("own_fid"),
          "没有中转 fid")


async def case_bridge_save_retry():
    """转存时 token 过期（41020）应自动重扫分享再试一次。"""
    calls = {"n": 0}
    saved = []

    class TokenExpiring(FakeQuark):
        def save(self, pwd_id, stoken, fids, tokens, to_pdir_fid="0"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise tgbot.QuarkError("转存文件token校验异常（code=41020）")
            saved.append(list(fids))
            return "task2"

        def wait_task_fids(self, task_id, timeout=300, interval=2.0):
            return ["OWNX"]

    q = TokenExpiring()
    tgbot._share_cache.clear()
    tgbot.QuarkShare = TokenExpiring
    result = await tgbot._bridge_save_fresh(
        "pwd123a", "", [{"fid": "F1"}], q, "st",
        {"F1": {"fid": "F1", "share_fid_token": "T1"}})
    check(result == {"F1": "OWNX"}, "token 过期后重试成功", str(result))
    check(calls["n"] == 2, "只重试一次", str(calls["n"]))


async def main():
    await case_bridge_flow()
    await case_direct_still_used()
    await case_bridge_save_retry()


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
