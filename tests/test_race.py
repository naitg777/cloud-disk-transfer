# -*- coding: utf-8 -*-
"""并发重复提交同一分享的竞态回归测试（修复后应为 1 个任务、1 条映射）。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/rtest /data/venv/bin/python3 /tmp/test_race.py
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/rtest"))
shutil.rmtree(WORK, ignore_errors=True)
(WORK / "gdl").mkdir(parents=True)
(WORK / "tgdl").mkdir(parents=True)
(WORK / "logs").mkdir(parents=True)

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
tgbot._probe_url = lambda url, hdrs: True   # 默认分享直链可用，桥接另有测试

FAILS, N = [], [0]


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(f"{label} {extra}")
        print(" ✗", label, extra)
    else:
        print(" ✓", label)


class Msg:
    def __init__(self):
        self.sink = []

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
        self.effective_chat = Chat()
        self.message = Msg()


class FakeGopeed:
    def __init__(self):
        self.n = 0
        self.names = []

    def add_task(self, url, name, path, headers=None, connections=None):
        self.n += 1
        self.names.append(name)
        return "tid%03d" % self.n

    def list_tasks(self):
        return []

    def remove_task(self, tid, force=True):
        pass


RealQuark = tgbot.QuarkShare


class FakeQuark:
    def __init__(self, *a, **k):
        pass

    @staticmethod
    def parse_link(t):
        return RealQuark.parse_link(t)

    def get_stoken(self, pwd_id, passcode=""):
        return "st"

    def walk_share(self, pwd_id, stoken, *a, **kw):
        return [{"path": "软件/a.apk", "name": "a.apk", "size": 100,
                 "fid": "F1", "share_fid_token": "T1"}]

    def quota(self):
        return 100 * 1024 ** 3, 10 * 1024 ** 3      # 假装夸克空间充足

    def download_headers(self):
        return {"Cookie": "c"}

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        return "https://cdn/%s" % fid


async def fake_notify(t):
    pass


REAL_TO_THREAD = asyncio.to_thread


async def slow_to_thread(fn, *a, **kw):
    await asyncio.sleep(0.15)          # 放大竞态窗口
    return await REAL_TO_THREAD(fn, *a, **kw)


async def main():
    tgbot.gopeed = FakeGopeed()
    tgbot.QUARK_MODE = "account"   # 测试里强制走账号桩，避免真去连夸克
    tgbot.QuarkShare = FakeQuark
    tgbot._notify = fake_notify
    tgbot.asyncio.to_thread = slow_to_thread
    tgbot.save_gopeed_map({})
    link = "https://pan.quark.cn/s/abc123def45"

    # 20 条并发提交，模拟连点 / 转发刷屏
    updates = [Update() for _ in range(20)]
    await asyncio.gather(*[tgbot.handle_quark_share(u, None, link) for u in updates])

    m = tgbot.load_gopeed_map()
    print("map entries:", len(m))
    print("tasks created:", tgbot.gopeed.n)
    print("names:", set(tgbot.gopeed.names))
    check(tgbot.gopeed.n == 1, "20 次并发提交只建 1 个任务", str(tgbot.gopeed.n))
    check(len(m) == 1, "映射里只有 1 条", str(len(m)))
    check(len(set(tgbot.gopeed.names)) == 1, "保存名唯一", str(set(tgbot.gopeed.names)))
    skipped_msgs = sum(1 for u in updates if u.message.sink
                       and "已经在下载队列里了" in u.message.sink[-1])
    check(skipped_msgs == 19, "其余 19 条提示已在队列", str(skipped_msgs))

    # 不同分享仍可并行处理
    RealQuarkParse = RealQuark.parse_link
    class FakeQuark2(FakeQuark):
        def walk_share(self, pwd_id, stoken, *a, **kw):
            return [{"path": "软件/b.apk", "name": "b.apk", "size": 100,
                     "fid": "F%d" % len(pwd_id), "share_fid_token": "T2"}]

    tgbot.QUARK_MODE = "account"   # 测试里强制走账号桩，避免真去连夸克
    tgbot.QuarkShare = FakeQuark2
    tgbot.gopeed = FakeGopeed()
    tgbot.save_gopeed_map({})
    u1, u2 = Update(), Update()
    await asyncio.gather(
        tgbot.handle_quark_share(u1, None, "https://pan.quark.cn/s/aaaaaaaa1111"),
        tgbot.handle_quark_share(u2, None, "https://pan.quark.cn/s/bbbbbbbb2222"),
    )
    check(tgbot.gopeed.n == 2, "不同分享并行各自建任务", str(tgbot.gopeed.n))
    check(len(tgbot.load_gopeed_map()) == 2, "不同分享两条映射", str(len(tgbot.load_gopeed_map())))


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
