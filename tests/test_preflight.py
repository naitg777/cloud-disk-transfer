# -*- coding: utf-8 -*-
"""提交前自检：环境/分享能不能下，要能在开工前拦住。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/pf /data/venv/bin/python3 test_preflight.py
"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

SERVER_DIR = os.environ.get(
    "SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src"))
WORK = Path(os.environ.get("WORK", "/tmp/pf"))
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
import tgbot  # noqa: E402

FAILS, N = [], [0]
MB = 1024 * 1024


def check(cond, label, extra=""):
    N[0] += 1
    print((" ✓ " if cond else " ✗ ") + label + (f"  {extra}" if not cond else ""))
    if not cond:
        FAILS.append(label)


class Disk:
    def __init__(self, free):
        self.total = 100 * 1024 ** 3
        self.free = free
        self.used = self.total - free


class FakeGopeed:
    fail = False

    def list_tasks(self):
        if FakeGopeed.fail:
            raise RuntimeError("gopeed down")
        return []

    def add_task(self, *a, **k):
        return "tid1"

    def remove_task(self, *a, **k):
        pass


class FakeQuark:
    def __init__(self, *a, **k):
        pass

    @staticmethod
    def parse_link(t):
        return "pwdpre", ""

    def get_stoken(self, pwd_id, passcode=""):
        return "st"

    files = []

    def walk_share(self, pwd_id, st, *a, **kw):
        return list(type(self).files)

    def download_headers(self):
        return {"Cookie": "c"}

    def get_direct_url(self, fid, tok, pwd_id, st):
        return "https://x/%s" % fid

    def member_info(self):
        return {"member_type": "VIP", "total_capacity": 100 * 1024 ** 3,
                "use_capacity": 10 * 1024 ** 3}

    def own_direct_url(self, fid):
        return "https://own/%s" % fid

    def ensure_dir(self, name, pdir_fid="0"):
        return "B"

    def save(self, *a, **k):
        return "task1"

    def wait_task_fids(self, tid, timeout=300, interval=2.0):
        return ["OWN1"]

    def delete(self, fids):
        pass


async def setup():
    tgbot.gopeed = FakeGopeed()
    tgbot.QuarkShare = FakeQuark
    tgbot._client_for = lambda kind="main": FakeQuark()
    tgbot.BRIDGE_COOKIE_FILE = str(WORK / "no_bridge.cookie")
    tgbot.shutil.disk_usage = lambda p: Disk(50 * 1024 ** 3)
    tgbot.PREFLIGHT_CACHE["gdrive"] = (time.time(), True, "")
    tgbot._aria2 = lambda m, p=None, timeout=10: ({} if "getVersion" in m else [])
    tgbot._share_cache.clear()
    tgbot._stoken_cache.clear()
    tgbot._learned_cap.update(main=0, bridge=0)
    tgbot.save_gopeed_map({})
    tgbot.save_queue({})


async def case_env_ok():
    await setup()
    ok, rows = await tgbot.preflight_common()
    text = "\n".join(rows)
    check(ok, "环境正常时通过", text)
    check("❌" not in text, "环境正常时没有红叉", text)


async def case_env_disk():
    await setup()
    tgbot.shutil.disk_usage = lambda p: Disk(1 * 1024 ** 3)   # 低于 2GB 安全线
    ok, rows = await tgbot.preflight_common()
    check(not ok, "磁盘不够时拦住")
    check(any("磁盘" in r and "❌" in r for r in rows), "报出磁盘问题", str(rows))


async def case_env_gopeed_down():
    await setup()
    FakeGopeed.fail = True
    try:
        ok, rows = await tgbot.preflight_common()
        check(not ok, "下载器挂了时拦住")
        check(any("Gopeed" in r and "❌" in r for r in rows), "报出下载器问题", str(rows))
    finally:
        FakeGopeed.fail = False


async def case_share_ok():
    await setup()
    tgbot._probe_url = lambda url, hdrs: True
    files = [{"fid": "F1", "name": "a.bin", "size": 10 * MB},
             {"fid": "F2", "name": "b.bin", "size": 20 * MB}]
    verdict, rows, okn, badn = await tgbot.preflight_share(
        FakeQuark(), "st", files, "pwdpre")
    check(verdict == "ok", "分享可下时判定 ok", f"{verdict} {rows}")
    check(okn == 2 and badn == 0, "统计可下数量", f"{okn}/{badn}")


async def case_share_blocked():
    """直链被拒、转存后下载也拿不到数据 → 必须判定 blocked。"""
    await setup()
    tgbot._probe_url = lambda url, hdrs: False

    class NoSave(FakeQuark):
        def save(self, *a, **k):
            raise tgbot.QuarkError("转存文件token校验异常（code=41020）")

    tgbot._client_for = lambda kind="main": NoSave()
    verdict, rows, okn, badn = await tgbot.preflight_share(
        NoSave(), "st", [{"fid": "F1", "name": "a.bin", "size": 10 * MB,
                          "share_fid_token": "T"}], "pwdpre")
    check(verdict == "blocked", "全下不了时判定 blocked", f"{verdict} {rows}")
    check(any("❌" in r for r in rows), "报出失败项", str(rows))


async def case_size_cap():
    """撞 23018 → 记上限，并把超限文件算进「跳过」。"""
    await setup()
    tgbot._probe_url = lambda url, hdrs: False

    class CapSave(FakeQuark):
        def save(self, *a, **k):
            raise tgbot.QuarkError("download file size limit（code=23018）")

    tgbot.QuarkShare = CapSave
    tgbot._client_for = lambda kind="main": CapSave()
    files = [{"fid": "BIG", "name": "big.bin", "size": 200 * MB,
              "share_fid_token": "T"}]
    CapSave.files = [{"fid": "BIG", "name": "big.bin", "size": 200 * MB,
                      "pdir": "", "share_fid_token": "T"}]
    verdict, rows, okn, badn = await tgbot.preflight_share(
        CapSave(), "st", files, "pwdpre")
    check(verdict == "blocked", "超大文件判定 blocked", f"{verdict} {rows}")
    check(tgbot._learned_cap.get("main", 0) > 0, "记下了账号下载上限",
          str(tgbot._learned_cap))


async def case_pending_roundtrip():
    await setup()
    tgbot._save_pending_submit("https://pan.quark.cn/s/abc123")
    check(tgbot._load_pending_submit() == "https://pan.quark.cn/s/abc123",
          "待重试内容能存能取")
    tgbot.PENDING_FILE = WORK / "logs" / "none.json"
    check(tgbot._load_pending_submit() == "", "没有待重试时返回空")


async def main():
    await case_env_ok()
    await case_env_disk()
    await case_env_gopeed_down()
    await case_share_ok()
    await case_share_blocked()
    await case_size_cap()
    await case_pending_roundtrip()


asyncio.run(main())
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
