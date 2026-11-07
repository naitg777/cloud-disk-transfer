# -*- coding: utf-8 -*-
"""夸克访问模式：账号优先 / 公开分享 / 自动切换 / 请求头顺序。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/gt /data/venv/bin/python3 test_guest.py
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/gt"))
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
CALLS = []


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(label)
        print(" ✗", label, extra)
    else:
        print(" ✓", label)


class FakeGuest:
    fail = False

    def __init__(self):
        CALLS.append("guest:init")

    @staticmethod
    def parse_link(t):
        return "d02af8share", ""

    def get_stoken(self, pwd_id, passcode=""):
        CALLS.append("guest:stoken")
        if FakeGuest.fail:
            raise RuntimeError("guest 不可用")
        return "gst"

    def walk_share(self, pwd_id, stoken, *a, **kw):
        CALLS.append("guest:walk")
        return [{"path": "a/x.bin", "name": "x.bin", "size": 1024,
                 "fid": "G1", "share_fid_token": "GT1"}]

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        CALLS.append("guest:url")
        return "https://dl-guest/x"

    def download_headers(self):
        CALLS.append("guest:headers")
        return {"Cookie": "__sdid=abc"}


class FakeAccount:
    fail = False

    def __init__(self):
        CALLS.append("account:init")

    @staticmethod
    def parse_link(t):
        return "d02af8share", ""

    def get_stoken(self, pwd_id, passcode=""):
        CALLS.append("account:stoken")
        if FakeAccount.fail:
            raise RuntimeError("账号不可用")
        return "ast"

    def walk_share(self, pwd_id, stoken, *a, **kw):
        CALLS.append("account:walk")
        return [{"path": "a/y.bin", "name": "y.bin", "size": 2048,
                 "fid": "A1", "share_fid_token": "AT1"}]

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        CALLS.append("account:url")
        return "https://dl-account/y"

    def download_headers(self):
        CALLS.append("account:headers")
        return {"Cookie": "account=1"}


class FakeGopeed:
    def __init__(self):
        self.calls = []

    def add_task(self, url, name, path, headers=None, connections=None):
        self.calls.append((url, headers))
        return "tid1"

    def list_tasks(self):
        return []

    def remove_task(self, tid, force=True):
        pass


async def case_modes():
    CALLS.clear()
    tgbot._share_cache.clear()
    tgbot.QuarkGuest = FakeGuest
    tgbot.QuarkShare = FakeAccount
    tgbot._share_direct_known = None

    tgbot.QUARK_MODE = "account"
    q, _, _ = await tgbot._open_share("d02af8share")
    check(isinstance(q, FakeAccount), "account 模式只用账号", type(q).__name__)

    CALLS.clear()
    tgbot._share_cache.clear()
    tgbot.QUARK_MODE = "guest"
    q, _, _ = await tgbot._open_share("d02af8share")
    check(isinstance(q, FakeGuest), "guest 模式只用公开会话", type(q).__name__)
    check("account:init" not in CALLS, "guest 模式完全不碰账号", str(CALLS))

    CALLS.clear()
    tgbot._share_cache.clear()
    tgbot.QUARK_MODE = "auto"
    tgbot._share_direct_known = None
    q, _, _ = await tgbot._open_share("d02af8share")
    check(isinstance(q, FakeAccount), "auto 模式账号可用时用账号", type(q).__name__)

    CALLS.clear()
    tgbot._share_cache.clear()
    tgbot._set_share_direct(False)      # 分享直链被拒不影响"用哪个客户端打开分享"
    q, _, _ = await tgbot._open_share("d02af8share")
    check(isinstance(q, FakeAccount), "分享直链被拒时仍用账号开分享（好走转存）",
          type(q).__name__)

    CALLS.clear()
    tgbot._share_cache.clear()
    tgbot._share_direct_known = None
    FakeAccount.fail = True
    try:
        q, _, _ = await tgbot._open_share("d02af8share")
        check(isinstance(q, FakeGuest), "账号失败回退公开会话", type(q).__name__)
    finally:
        FakeAccount.fail = False

    tgbot.QUARK_MODE = "guest"
    FakeGuest.fail = True
    tgbot._share_cache.clear()          # 关键：前面成功过，缓存会让这里直接返回
    try:
        await tgbot._open_share("d02af8share")
        check(False, "公开会话失败且无兜底时应报错")
    except Exception:
        check(True, "公开会话失败且无兜底时报错")
    finally:
        FakeGuest.fail = False


async def case_header_order():
    """公开会话下必须先取 URL 再取请求头（__sdid 是取 URL 时写入的）。"""
    CALLS.clear()
    tgbot._share_cache.clear()
    tgbot.QUARK_MODE = "guest"
    tgbot.QuarkGuest = FakeGuest
    tgbot.gopeed = FakeGopeed()
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})
    tgbot.shutil.disk_usage = lambda p: type("D", (), {
        "total": 100 * 1024 ** 3, "free": 50 * 1024 ** 3, "used": 50 * 1024 ** 3})()
    tgbot.queue_put("d02af8share", "分享_x", "2026-11-02", "", [
        {"fid": "G1", "name": "x.bin", "size": 1024, "rel": "a/x.bin",
         "fid_token": "GT1"}])
    await tgbot.admit_wave(force=True)
    order = [c for c in CALLS if c.startswith("guest:")]
    check("guest:url" in order and "guest:headers" in order, "取过 URL 和请求头", str(order))
    if "guest:url" in order and "guest:headers" in order:
        check(order.index("guest:url") < order.index("guest:headers"),
              "先取直链再取请求头（__sdid 才在）", str(order))
    check(len(tgbot.gopeed.calls) == 1, "任务已创建", str(len(tgbot.gopeed.calls)))
    if tgbot.gopeed.calls:
        check(tgbot.gopeed.calls[0][1].get("Cookie") == "__sdid=abc",
              "下载头用的是公开会话 Cookie", str(tgbot.gopeed.calls[0][1]))


async def main():
    await case_modes()
    await case_header_order()


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
