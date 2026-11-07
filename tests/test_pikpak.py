# -*- coding: utf-8 -*-
"""PikPak 支持：链接解析、签名、目录遍历、直链挑选、建任务。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/pk /data/venv/bin/python3 test_pikpak.py
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/pk"))
shutil.rmtree(WORK, ignore_errors=True)
for sub in ("gdl", "tgdl", "logs"):
    (WORK / sub).mkdir(parents=True)
os.environ.update({
    "BOT_TOKEN": "T", "ALLOWED_CHAT": "42", "ARIA2_SECRET": "T",
    "DOWNLOAD_DIR": str(WORK / "tgdl"), "GOPEED_DLDIR": str(WORK / "gdl"),
    "GOPEED_MAP": str(WORK / "logs/map.json"),
    "GOPEED_QUEUE": str(WORK / "logs/queue.json"),
    "ALERT_STATE": str(WORK / "logs/a.json"), "RCLONE_CONF": str(WORK / "r.json"),
    "QUARK_BRIDGE_COOKIE_FILE": str(WORK / "no_bridge.cookie"),
})
sys.path.insert(0, SERVER_DIR)
import pikpak_share as pk  # noqa: E402
import tgbot  # noqa: E402

FAILS, N = [], [0]
MB = 1024 * 1024


def check(cond, label, extra=""):
    N[0] += 1
    print((" ✓ " if cond else " ✗ ") + label + (f"  {extra}" if not cond else ""))
    if not cond:
        FAILS.append(label)


# ---------------- 纯函数 ----------------
def case_parse():
    sid, pwd = pk.parse_share("看这个 https://mypikpak.com/s/VOabc123_X 密码: k9d2")
    check(sid == "VOabc123_X" and pwd == "k9d2", "解析分享链接+密码", f"{sid}/{pwd}")
    sid2, pwd2 = pk.parse_share("https://mypikpak.net/s/abcDEF?foo=1")
    check(sid2 == "abcDEF" and pwd2 == "", "支持 .net 域名", f"{sid2}/{pwd2}")
    try:
        pk.parse_share("https://example.com/x")
        check(False, "非 PikPak 链接应报错")
    except pk.PikPakError:
        check(True, "非 PikPak 链接应报错")


def case_sign():
    a = pk.captcha_sign("0" * 32, "1700000000000")
    b = pk.captcha_sign("0" * 32, "1700000000000")
    c = pk.captcha_sign("0" * 32, "1700000000001")
    check(a == b and a.startswith("1.") and len(a) == 34, "签名稳定且带 1. 前缀", a[:12])
    check(a != c, "时间戳不同签名不同")


def case_pick():
    check(pk.PikPakShare.pick_url({"web_content_link": "W",
                                   "medias": [{"link": {"url": "M"}}]}) == "W",
          "优先 web_content_link")
    check(pk.PikPakShare.pick_url({"medias": [{"link": {"url": "M0"}}]}) == "M0",
          "没有直链时用 media")
    check(pk.PikPakShare.pick_url(
        {"medias": [{"link": {"url": "M0"}}, {"link": {"url": "M1"}}]}, True) == "M1",
        "可选转码地址")
    check(pk.PikPakShare.pick_url({}) == "", "空信息返回空")


def case_walk():
    """假 share_detail：根目录 2 个文件 + 1 个文件夹，文件夹里 1 个文件。"""
    pages = {
        ("", ""): {"share_status": "OK", "files": [
            {"id": "f1", "name": "a.mp4", "size": "10", "kind": "drive#file"},
            {"id": "d1", "name": "合集", "kind": "drive#folder"},
        ], "next_page_token": "p2"},
        ("", "p2"): {"share_status": "OK", "files": [
            {"id": "f2", "name": "b.zip", "size": "20", "kind": "drive#file"},
        ], "next_page_token": ""},
        ("d1", ""): {"share_status": "OK", "files": [
            {"id": "f3", "name": "c.mp4", "size": "30", "kind": "drive#file"},
        ], "next_page_token": ""},
    }

    class Fake(pk.PikPakShare):
        def share_detail(self, share_id, parent_id="", page_token=""):
            return pages[(parent_id, page_token)]

    files = Fake().walk_share("sid")
    names = sorted(f["name"] for f in files)
    check(names == ["a.mp4", "b.zip", "c.mp4"], "递归+翻页列出文件", str(names))
    paths = {f["name"]: f["path"] for f in files}
    check(paths["c.mp4"] == "合集/c.mp4", "嵌套文件带目录前缀", paths["c.mp4"])
    check(files[0]["size"] == 10, "size 转成 int", str(files[0]["size"]))


def case_walk_prohibited():
    class Bad(pk.PikPakShare):
        def share_detail(self, share_id, parent_id="", page_token=""):
            return {"share_status": "PROHIBITED", "files": []}

    try:
        Bad().walk_share("sid")
        check(False, "PROHIBITED 应报错")
    except pk.PikPakError as exc:
        check("PROHIBITED" in str(exc) or "住宅" in str(exc),
              "PROHIBITED 给出可读原因", str(exc)[:40])


# ---------------- 处理流程 ----------------
class Msg:
    def __init__(self, sink):
        self.sink = sink

    async def edit_text(self, text, **kw):
        self.sink.append(text)
        return self

    async def reply_text(self, text, **kw):
        self.sink.append(text)
        return self

    async def edit_reply_markup(self, **kw):
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
        self.calls = []

    def add_task(self, url, name, path, headers=None, connections=None):
        self.calls.append((url, headers, name))
        return "tid%d" % len(self.calls)

    def list_tasks(self):
        return []

    def remove_task(self, *a, **k):
        pass


class FakePikPak:
    files = []
    info_url = {}
    logged = False

    def __init__(self, *a, **k):
        pass

    def ensure_login(self):
        return FakePikPak.logged

    def unlock(self, share_id):
        return ""

    def walk_share(self, share_id, limit=5000):
        return list(FakePikPak.files)

    def file_info(self, share_id, file_id):
        return {"web_content_link": FakePikPak.info_url.get(file_id, "")}

    @staticmethod
    def pick_url(info, prefer_transcode=False):
        return (info or {}).get("web_content_link") or ""

    def restore(self, *a, **k):
        return {}

    def own_file_url(self, own_id):
        return ""

    def download_headers(self):
        return {"User-Agent": "UA", "Referer": "https://mypikpak.com/"}


class FakeMain:
    def member_info(self):
        return {"member_type": "VIP", "total_capacity": 200 * 1024 ** 3,
                "use_capacity": 20 * 1024 ** 3}


def _pf_stub():
    tgbot.PREFLIGHT_CACHE["gdrive"] = (time.time(), True, "")
    tgbot._aria2 = lambda m, p=None, timeout=10: ({} if "getVersion" in m else [])
    tgbot._client_for = lambda kind="main": FakeMain()

async def case_handler_video():
    tgbot.gopeed = FakeGopeed()
    tgbot.PikPakShare = FakePikPak
    tgbot.save_gopeed_map({})
    _pf_stub()
    FakePikPak.files = [
        {"id": "f1", "name": "a.mp4", "size": 10 * MB, "path": "a.mp4",
         "ancestors": []},
        {"id": "f2", "name": "b.zip", "size": 20 * MB, "path": "b.zip",
         "ancestors": []},
    ]
    FakePikPak.info_url = {"f1": "https://dl/a.mp4"}      # 只有视频有直链
    up = Update()
    await tgbot.handle_pikpak_share(up, None, "https://mypikpak.com/s/VOsid1")
    m = tgbot.load_gopeed_map()
    text = up.sink[-1]
    check(len(m) == 1, "只给能下的建任务", str(len(m)))
    check(any("a.mp4" in v.get("orig_name", "") for v in m.values()),
          "建的是那个视频", str([v.get("orig_name") for v in m.values()]))
    check("已开始下载 1 个" in text, "汇报开始下载数", text[:60])
    check("非视频" in text or "1 个非视频" in text, "提示非视频需要登录", text[-80:])
    url, hdrs, _ = tgbot.gopeed.calls[0]
    check(url == "https://dl/a.mp4", "任务用的是直链", url)
    check("mypikpak" in hdrs.get("Referer", ""), "带 Referer", str(hdrs))


async def case_handler_blocked():
    tgbot.gopeed = FakeGopeed()
    tgbot.save_gopeed_map({})
    FakePikPak.files = [{"id": "f9", "name": "x.zip", "size": MB,
                         "path": "x.zip", "ancestors": []}]
    FakePikPak.info_url = {}
    up = Update()
    await tgbot.handle_pikpak_share(up, None, "https://mypikpak.com/s/VOsid2")
    check(len(tgbot.load_gopeed_map()) == 0, "全都没有直链时不建任务")
    check("暂时下不了" in up.sink[-1], "给出明确原因", up.sink[-1][:60])
    check(tgbot._load_pending_submit().endswith("VOsid2"), "存了待重试")


async def main():
    case_parse()
    case_sign()
    case_pick()
    case_walk()
    case_walk_prohibited()
    await case_handler_video()
    await case_handler_blocked()


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
