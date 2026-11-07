# -*- coding: utf-8 -*-
"""handle_quark_share / handle_text 行为测试（桩对象，不联网）。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/htest /data/venv/bin/python3 /tmp/test_handler.py
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/htest"))
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

FAILS, N = [], [0]


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(f"{label} {extra}")
        print(" ✗", label, extra)
    else:
        print(" ✓", label)


class Msg:
    def __init__(self, sink):
        self.text = None
        self.sink = sink

    async def edit_text(self, text, **kw):
        self.sink.append(text)
        self.text = text
        return self

    async def reply_text(self, text, **kw):
        self.sink.append(text)
        self.text = text
        return self


class Chat:
    id = 42


class Update:
    def __init__(self):
        self.effective_chat = Chat()
        self.sink = []
        self.message = Msg(self.sink)


class FakeGopeed:
    def __init__(self):
        self.added = []
        self.n = 0

    def add_task(self, url, name, path, headers=None, connections=None):
        self.n += 1
        tid = "tid%03d" % self.n
        self.added.append((tid, name, url))
        return tid

    def remove_task(self, tid, force=True):
        pass


class FakeQuark:
    fail_names = set()

    def __init__(self, *a, **k):
        pass

    @staticmethod
    def parse_link(text):
        return RealQuark.parse_link(text)

    def get_stoken(self, pwd_id, passcode=""):
        return "st"

    def walk_share(self, pwd_id, stoken, *a, **kw):
        return self.files

    def quota(self):
        return 100 * 1024 ** 3, 10 * 1024 ** 3      # 假装夸克空间充足

    def download_headers(self):
        return {"Cookie": "c"}

    def get_direct_url(self, fid, tok, pwd_id, stoken):
        if fid in FakeQuark.fail_names:
            raise RuntimeError("CDN 拒绝")
        return "https://cdn/%s" % fid


notifies = []


async def fake_notify(t):
    notifies.append(t)


RealQuark = tgbot.QuarkShare
tgbot.gopeed = FakeGopeed()
tgbot.QUARK_MODE = "account"
tgbot._probe_url = lambda url, hdrs: True   # 默认分享直链可用，桥接另有测试   # 测试里强制走账号桩，避免真去连夸克
tgbot.QuarkShare = FakeQuark
tgbot._notify = fake_notify


def make_files():
    return [
        {"path": "软件/a.apk", "name": "a.apk", "size": 100, "fid": "F1", "share_fid_token": "T1"},
        {"path": "软件/sub/b.apk", "name": "b.apk", "size": 200, "fid": "F2", "share_fid_token": "T2"},
        {"path": "软件/c.apk", "name": "c.apk", "size": 300, "fid": "F3", "share_fid_token": "T3"},
    ]


async def main():
    link = "https://pan.quark.cn/s/abc123def45 提取码: q1w2"

    # ---- 1. 正常 3 文件 ----
    tgbot.save_gopeed_map({})
    FakeQuark.files = make_files()
    FakeQuark.fail_names = set()
    tgbot.gopeed = FakeGopeed()
    up = Update()
    await tgbot.handle_quark_share(up, None, link)
    m = tgbot.load_gopeed_map()
    check(len(m) == 3, "1 三个任务写入映射", str(len(m)))
    check(len(tgbot.gopeed.added) == 3, "1 三次 add_task", str(len(tgbot.gopeed.added)))
    bj_date = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
    check(f"保存到：Aria2Files › {bj_date} › 软件_abc123" in (up.sink[-1] or ""),
          "1 保存位置带 pwd_id 后缀", up.sink[-1][-200:])
    check("本次开始下载 3 个" in up.sink[-1], "1 汇总放行数", up.sink[-1][:60])
    info = list(m.values())[0]
    check(info.get("share", "").startswith("软件_"), "1 share 名称", info.get("share"))
    check(info.get("rel_path") == "a.apk", "1 rel_path 去掉顶层", info.get("rel_path"))
    check(info.get("saved_name", "").startswith("abc123de_"), "1 saved_name 前缀", info.get("saved_name"))

    # ---- 2. 重复提交同一分享 ----
    tgbot._share_cache.clear()
    up2 = Update()
    await tgbot.handle_quark_share(up2, None, link)
    check("已经在下载队列里了" in (up2.sink[-1] or ""), "2 重复分享被跳过", up2.sink[-1][:80])
    check(len(tgbot.gopeed.added) == 3, "2 未重复创建任务")

    # ---- 3. 磁盘不足 ----
    tgbot._share_cache.clear()
    tgbot.save_gopeed_map({})
    tgbot.gopeed = FakeGopeed()
    real_du = shutil.disk_usage

    class DU:
        total = 100 * 1024 ** 3
        used = 99 * 1024 ** 3
        free = 1 * 1024 ** 3

    tgbot.shutil.disk_usage = lambda p: DU
    up3 = Update()
    await tgbot.handle_quark_share(up3, None, link)
    check("一个都放不下" in (up3.sink[-1] or ""), "3 磁盘不足拦截", up3.sink[-1][:60])
    check(not tgbot.load_gopeed_map(), "3 磁盘不足未建任务")
    tgbot.shutil.disk_usage = real_du

    # ---- 4. 部分文件取直链失败 ----
    tgbot._share_cache.clear()
    tgbot.save_gopeed_map({})
    tgbot.gopeed = FakeGopeed()
    FakeQuark.fail_names = {"F2"}
    up4 = Update()
    await tgbot.handle_quark_share(up4, None, link)
    m = tgbot.load_gopeed_map()
    check(len(m) == 2, "4 失败文件未入队", str(len(m)))
    check("剩下 1 个" in (up4.sink[-1] or ""), "4 失败文件留在队列", up4.sink[-1][:80])
    FakeQuark.fail_names = set()

    # ---- 5. 超过文件数上限 ----
    tgbot._share_cache.clear()
    tgbot.MAX_SHARE_FILES = 1000
    tgbot.save_gopeed_map({})
    FakeQuark.files = [{"path": "d/f%d.bin" % i, "name": "f%d.bin" % i, "size": 1,
                        "fid": "G%d" % i, "share_fid_token": "S%d" % i} for i in range(1001)]
    up5 = Update()
    await tgbot.handle_quark_share(up5, None, link)
    check("超过单次上限" in (up5.sink[-1] or ""), "5 文件数上限拦截", up5.sink[-1][:80])

    tgbot.MAX_SHARE_FILES = 20000
    tgbot._share_cache.clear()
    # ---- 6. 空分享 ----
    tgbot._share_cache.clear()
    FakeQuark.files = []
    up6 = Update()
    await tgbot.handle_quark_share(up6, None, link)
    check("没有能下载的文件" in (up6.sink[-1] or ""), "6 空分享提示")

    # ---- 7. 解析失败（无链接） ----
    up7 = Update()
    await tgbot.handle_quark_share(up7, None, "随便一段话")
    check("没解析成功" in (up7.sink[-1] or ""), "7 非法链接提示", up7.sink[-1][:40])

    # ---- 8. 单根文件分享 ----
    tgbot._share_cache.clear()
    tgbot.save_queue({})
    tgbot.save_gopeed_map({})
    tgbot.gopeed = FakeGopeed()
    FakeQuark.files = [{"path": "only.zip", "name": "only.zip", "size": 10,
                        "fid": "H1", "share_fid_token": "S1"}]
    up8 = Update()
    await tgbot.handle_quark_share(up8, None, link)
    m = tgbot.load_gopeed_map()
    check(len(m) == 1, "8 单文件入队", str(len(m)))
    check(list(m.values())[0]["share"].startswith("only_"), "8 单文件分享名",
          list(m.values())[0]["share"])

    # ---- 9. 文本入口：非法内容 ----
    up9 = Update()
    up9.message.text = "hello"
    await tgbot.handle_text(up9, None)
    check("没看懂" in (up9.message.text or "") or "没看懂" in up9.sink[-1],
          "9 无关文本提示")

    # ---- 10. 磁力链接格式校验 ----
    up10 = Update()
    up10.message.text = "magnet:?dn=x"
    await tgbot.handle_text(up10, None)
    check("磁力链接不完整" in up10.sink[-1], "10 非法磁力提示")


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
