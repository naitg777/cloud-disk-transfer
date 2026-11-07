# -*- coding: utf-8 -*-
"""本轮修复点的回归测试：进度上报、janitor、on-complete.sh、死代码清理。

运行：SERVER_DIR=/opt/cloud-disk-transfer WORK=/tmp/ftest /data/venv/bin/python3 /tmp/test_fixes.py
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVER_DIR = Path(os.environ.get("SERVER_DIR", str(Path(__file__).resolve().parents[1] / "src")))
WORK = Path(os.environ.get("WORK", "/tmp/ftest"))
shutil.rmtree(WORK, ignore_errors=True)
(WORK / "scripts").mkdir(parents=True, exist_ok=True)
(WORK / "tgdl").mkdir(parents=True, exist_ok=True)
(WORK / "gdl").mkdir(parents=True, exist_ok=True)
(WORK / "logs").mkdir(parents=True, exist_ok=True)

# 复制源码到临时目录，避免污染生产 __pycache__ / 目录
for f in ("tgbot.py", "quark_share.py", "pikpak_share.py", "gopeed_client.py",
          "cleanup_leftovers.py", "on-complete.sh"):
    shutil.copy(SERVER_DIR / f, WORK / "scripts" / f)

os.environ.update({
    "BOT_TOKEN": "T", "ALLOWED_CHAT": "42", "ARIA2_SECRET": "T",
    "DOWNLOAD_DIR": str(WORK / "tgdl"),
    "GOPEED_DLDIR": str(WORK / "gdl"),
    "GOPEED_MAP": str(WORK / "logs" / "map.json"),
    "ALERT_STATE": str(WORK / "logs" / "alert.json"),
    "RCLONE_CONF": str(WORK / "rclone.conf"),
})
sys.path.insert(0, str(WORK / "scripts"))
import tgbot  # noqa: E402

FAILS, N = [], [0]


def check(cond, label, extra=""):
    N[0] += 1
    if not cond:
        FAILS.append(f"{label} {extra}")
        print(" ✗", label, extra)
    else:
        print(" ✓", label)


# ---------------- 1. rclone 上传进度参数 ----------------
class _Stdout:
    def __init__(self, lines):
        self._lines = lines

    def __aiter__(self):
        self._it = iter(self._lines)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Proc:
    def __init__(self, lines):
        self.stdout = _Stdout(lines)

    async def wait(self):
        return 0


class _Msg:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)


captured = {}


async def fake_exec(*cmd, **kw):
    captured["cmd"] = list(cmd)
    return _Proc([b"2026/11/01 10:00:00 NOTICE: 1.234 MiB / 5.678 MiB, 21%, 1.2 MiB/s, ETA 3s\n"])


async def case_progress():
    real = tgbot.asyncio.create_subprocess_exec
    tgbot.asyncio.create_subprocess_exec = fake_exec
    try:
        msg = _Msg()
        f = WORK / "tgdl" / "x.bin"
        f.write_bytes(b"0")
        rc, tail = await tgbot._upload_with_progress(msg, f)
    finally:
        tgbot.asyncio.create_subprocess_exec = real
    cmd = captured.get("cmd") or []
    check("--stats-log-level" in cmd, "上传命令带 --stats-log-level", str(cmd))
    check(cmd[cmd.index("--stats-log-level") + 1] == "NOTICE", "--stats-log-level=NOTICE")
    check(any("21%" in e for e in msg.edits), "进度消息被真实更新", str(msg.edits))
    check(rc == 0, "返回码透传", str(rc))


# ---------------- 2. cleanup_leftovers ----------------
def case_janitor():
    import cleanup_leftovers as cl

    tg, gd = WORK / "tgdl", WORK / "gdl"
    now = time.time()
    old = now - 10 * 86400
    keep_new = tg / "fresh.bin"
    keep_new.write_bytes(b"new")
    old_file = tg / "stale.bin"
    old_file.write_bytes(b"x" * 100)
    os.utime(old_file, (old, old))
    part = tg / "partial.bin.part"
    part.write_bytes(b"x")
    os.utime(part, (old, old))
    active = tg / "in_flight.bin"
    active.write_bytes(b"x")
    os.utime(active, (old, old))
    deep = gd / "2026-01-01" / "share" / "orphan.bin"
    deep.parent.mkdir(parents=True)
    deep.write_bytes(b"x" * 50)
    os.utime(deep, (old, old))
    in_map = gd / "mapped.bin"
    in_map.write_bytes(b"x")
    os.utime(in_map, (old, old))

    tgbot.save_gopeed_map({"tid1": {"saved_name": "mapped.bin", "orig_name": "mapped.bin"}})
    cl.GOPEED_MAP = WORK / "logs" / "map.json"
    cl.DEFAULT_DAYS = 7

    removed, freed, kept = cl.cleanup(gd, now - 7 * 86400, cl.active_names(), False)
    check(not deep.exists(), "janitor 删除孤儿文件")
    check(not (gd / "2026-01-01").exists(), "janitor 回收空目录")
    check(in_map.exists(), "janitor 保护在途任务文件")
    check(gd.exists(), "janitor 不删除根目录")

    removed2, _, _ = cl.cleanup(tg, now - 7 * 86400, cl.active_names(), False)
    check(not old_file.exists(), "janitor 删除过期残留")
    check(keep_new.exists(), "janitor 保留新文件")
    check(part.exists(), "janitor 保留下载中间件")
    check(not active.exists(), "janitor 清理未被跟踪的旧文件")
    check(tg.exists(), "janitor 保留 Telegram 根目录")

    removed3, _, _ = cl.cleanup(tg, now - 7 * 86400, cl.active_names(), True)
    check(removed3 == [], "dry-run 不删除任何文件")

    # 磁力元数据缓存：只删 .torrent，不动 aria2 数据文件
    aria = WORK / "aria2dl"
    aria.mkdir()
    tor = aria / "abc123.torrent"
    tor.write_bytes(b"d4:info")
    os.utime(tor, (old, old))
    data = aria / "movie.mkv"
    data.write_bytes(b"x" * 1000)
    os.utime(data, (old, old))
    fresh_tor = aria / "fresh.torrent"
    fresh_tor.write_bytes(b"d4:info")
    tr, _ = cl.cleanup_torrent_cache(aria, now - 7 * 86400, False)
    check(not tor.exists(), "janitor 清理过期磁力缓存 .torrent")
    check(data.exists(), "janitor 不动 aria2 下载数据")
    check(fresh_tor.exists(), "janitor 保留新 .torrent")


# ---------------- 3. on-complete.sh ----------------
def case_on_complete():
    p = WORK / "scripts" / "on-complete.sh"
    src = p.read_text(encoding="utf-8")
    check("done <<EOF" in src, "on-complete 使用 here-doc")
    check(not re.search(r"printf[^\n]*\|\s*while", src), "不再有管道喂 while")
    check('FAIL_ANY' in src and '[ "$FAIL_ANY" -eq 0 ]' in src, "FAIL_ANY 判定仍在")
    r = subprocess.run(["sh", "-n", str(p)], capture_output=True, text=True)
    check(r.returncode == 0, "sh -n 语法通过", r.stderr.strip()[:120])

    # 验证 here-doc 循环能把失败带回父 shell
    snippet = (
        'RPC_FILES="/nonexistent/a.bin\n/nonexistent/b.bin"\n'
        'FAIL_ANY=0\n'
        'while IFS= read -r f; do [ -f "$f" ] || continue; FAIL_ANY=1; done <<EOF\n'
        '$RPC_FILES\n'
        'EOF\n'
        'echo $FAIL_ANY\n'
    )
    r = subprocess.run(["sh", "-c", snippet], capture_output=True, text=True)
    check(r.stdout.strip() == "0", "跳过不存在文件时不计失败", r.stdout.strip())

    snippet2 = snippet.replace('[ -f "$f" ] || continue; ', '')
    r = subprocess.run(["sh", "-c", snippet2], capture_output=True, text=True)
    check(r.stdout.strip() == "1", "here-doc 循环失败可回传父 shell", r.stdout.strip())


# ---------------- 4. 死代码已清理 ----------------
def case_deadcode():
    src = (WORK / "scripts" / "tgbot.py").read_text(encoding="utf-8")
    check("_openlist" not in src, "OpenList 链路已移除")
    check("def map_has" not in src, "map_has 已移除")
    check("from quark_share import QuarkShare, QuarkGuest, QuarkError" in src, "transfer 导入已移除")


# ---------------- 4.5 磁盘页：数字必须可区分、且带统计时间 ----------------
def case_disk():
    real_drive = tgbot._drive_about
    tgbot._drive_about = lambda: (5 * 1024 ** 4, int(1.5 * 1024 ** 3), 5 * 1024 ** 4)
    try:
        text = tgbot.view_disk()
    finally:
        tgbot._drive_about = real_drive

    def grab(label):
        m = re.search(label + r"：(.+)", text)
        return m.group(1).strip() if m else ""

    total_s, used_s, free_s = grab("总容量"), grab("已占用"), grab("还剩余")
    check(bool(total_s and used_s and free_s), "磁盘页三项都有值", text[:80])
    check(len({total_s, used_s, free_s}) == 3, "磁盘页三个数字互不相同",
          f"{total_s} | {used_s} | {free_s}")
    check("统计时间" in text, "磁盘页带统计时间（证明是实时读的）")
    check("谷歌云盘" in text and "已用 1.5 GB / 共 5 TB" in text,
          "云盘容量用「已用/共」写法，不会出现两个 5 TB", text[:200])


# ---------------- 5. 提速相关：连接数分档 + rclone 参数 ----------------
def case_speed():
    check(tgbot.gopeed_connections(0) == 4, "小文件用 4 连接", str(tgbot.gopeed_connections(0)))
    check(tgbot.gopeed_connections(5 * 1024 ** 2) == 16, "5MB 用 16 连接")
    check(tgbot.gopeed_connections(50 * 1024 ** 2) == 24, "50MB 用 24 连接")
    check(tgbot.gopeed_connections(500 * 1024 ** 2) == 32, "500MB 用 32 连接")

    # rclone 上传参数（注意：必须桩掉 _rclone_run，否则会真的传 GDrive）
    calls = []

    def fake_rclone(args, timeout=60):
        calls.append(list(args))
        return 0, "", ""

    real_rclone = tgbot._rclone_run
    tgbot._rclone_run = fake_rclone
    try:
        gdir = WORK / "gdl2"
        gdir.mkdir(parents=True, exist_ok=True)
        tgbot.GOPEED_DLDIR = str(gdir)
        (gdir / "spd.bin").write_bytes(b"x" * 16)
        tgbot.finalize_gopeed_task("SPD", {
            "date": "2026-11-01", "share": "s", "orig_name": "spd.bin",
            "saved_name": "spd.bin", "rel_path": "spd.bin", "size": 16})
    finally:
        tgbot._rclone_run = real_rclone
    args = " ".join(map(str, calls[0])) if calls else ""
    check("--drive-chunk-size 64M" in args, "上传使用 64M 分片", args[:200])
    check("--transfers 4" in args, "上传并发 4")
    check("--checkers 8" in args, "校验并发 8")


async def main():
    await case_progress()


asyncio.run(main())
case_janitor()
case_on_complete()
case_deadcode()
case_disk()
case_speed()
print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
