# -*- coding: utf-8 -*-
"""本地离线单元测试：不依赖 Telegram / 网络 / 生产目录。

运行： python tests\test_pure.py
"""
import os
import sys
import types
import tempfile
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.environ.get("SERVER_DIR", str(ROOT / "src")))

TMP = ROOT / ".testwork"
shutil.rmtree(TMP, ignore_errors=True)
(os.makedirs if hasattr(os, "makedirs") else os.mkdir)(str(TMP), exist_ok=True)
os.environ.update({
    "BOT_TOKEN": "TEST",
    "ALLOWED_CHAT": "1",
    "ARIA2_SECRET": "TEST",
    "DOWNLOAD_DIR": str(TMP / "tgdl"),
    "GOPEED_DLDIR": str(TMP / "gdl"),
    "GOPEED_MAP": str(TMP / "map.json"),
    "ALERT_STATE": str(TMP / "alert.json"),
    "RCLONE_CONF": str(TMP / "rclone.conf"),
})


def _mk(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Any:
    def __init__(self, *a, **kw):
        self.args, self.kw = a, kw


class _ContextTypes:
    DEFAULT_TYPE = object


try:
    import requests  # noqa
except Exception:
    _mk("requests", post=lambda *a, **k: None, get=lambda *a, **k: None)

_tg = _mk("telegram", Update=_Any, InlineKeyboardButton=_Any,
          InlineKeyboardMarkup=_Any, ReplyKeyboardMarkup=_Any, KeyboardButton=_Any)
_tg.error = _mk("telegram.error", BadRequest=type("BadRequest", (Exception,), {}))
_tg.ext = _mk("telegram.ext", Application=_Any, CommandHandler=_Any, MessageHandler=_Any,
              CallbackQueryHandler=_Any, ContextTypes=_ContextTypes,
              filters=types.SimpleNamespace(
                  TEXT=None, COMMAND=None, PHOTO=1, VIDEO=2, AUDIO=3, VOICE=4,
                  Document=types.SimpleNamespace(ALL=None)))

import tgbot  # noqa: E402

FAILS = []
CHECKS = [0]


def check(cond, label, extra=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append(f"{label}  {extra}")


def eq(a, b, label):
    check(a == b, label, f"got={a!r} want={b!r}")


# ---------------- safe_name ----------------
eq(tgbot.safe_name("movie.mkv"), "movie.mkv", "safe_name 普通名")
long_name = "字" * 300 + ".mkv"
out = tgbot.safe_name(long_name)
check(out.endswith(".mkv"), "safe_name 长名保留扩展名", out[-12:])
check(len(out.encode()) <= 200 + 16, "safe_name 长名字节上限", len(out.encode()))
check("\ufffd" not in out, "safe_name 无乱码替换符")
eq(tgbot.safe_name("../../etc/passwd"), "passwd", "safe_name 去路径")
eq(tgbot.safe_name("a*b?c|d.txt"), "a_b_c_d.txt", "safe_name 非法字符")
if os.name != "nt":
    # Windows 上 ":" 会被 os.path.basename 当作盘符分隔，Linux 上才会被替换
    eq(tgbot.safe_name("a:b*.txt"), "a_b_.txt", "safe_name 冒号替换")
eq(tgbot.safe_name(""), "unnamed", "safe_name 空名")
eq(tgbot.safe_name("..."), "unnamed", "safe_name 全点")
eq(tgbot.safe_name(" .hidden "), " .hidden", "safe_name 尾随空格剥离")
eq(tgbot.safe_name("x." + "e" * 40), "x." + "e" * 15, "safe_name 超长扩展名截断")
eq(tgbot.safe_name("noext"), "noext", "safe_name 无扩展名")

# ---------------- safe_rel_path ----------------
p = tgbot.safe_rel_path("../../etc/passwd")
check(".." not in p.parts, "safe_rel_path 阻止穿越", str(p))
eq(str(p), "etc/passwd" if os.sep == "/" else str(Path("etc/passwd")), "safe_rel_path 结果")
p = tgbot.safe_rel_path("a//b//c.txt")
eq(len(p.parts), 3, "safe_rel_path 忽略空段")
p = tgbot.safe_rel_path("")
eq(str(p), "unnamed", "safe_rel_path 空值兜底")
p = tgbot.safe_rel_path("/abs/root.txt")
eq(p.parts[0], "abs", "safe_rel_path 绝对路径变相对")

# ---------------- _share_layout ----------------
files_single_dir = [
    {"name": "a.bin", "path": "软件/a.bin", "size": 1},
    {"name": "b.bin", "path": "软件/sub/b.bin", "size": 1},
]
share, rels = tgbot._share_layout(files_single_dir, "d02af8aa11")
check(share.startswith("软件_"), "share_layout 单目录名带后缀", share)
eq(rels, ["a.bin", "sub/b.bin"], "share_layout 去掉顶层目录")

files_root = [{"name": "only.zip", "path": "only.zip", "size": 1}]
share, rels = tgbot._share_layout(files_root, "abcdef123")
check(share.startswith("only_"), "share_layout 单根文件", share)
eq(rels, ["only.zip"], "share_layout 单文件相对路径")

files_mixed = [{"name": "x", "path": "A/x", "size": 1},
               {"name": "y", "path": "B/y", "size": 1},
               {"name": "z", "path": "z", "size": 1}]
share, rels = tgbot._share_layout(files_mixed, "12345678")
eq(share, "分享_12345678", "share_layout 混合用分享名")
eq(rels, ["A/x", "B/y", "z"], "share_layout 混合保留路径")

# 同名顶层目录不同分享必须不同
s1, _ = tgbot._share_layout(files_single_dir, "aaaaaa11")
s2, _ = tgbot._share_layout(files_single_dir, "bbbbbb22")
check(s1 != s2, "share_layout 不同分享不冲突", f"{s1} vs {s2}")

# ---------------- gopeed_saved_name ----------------
n1 = tgbot.gopeed_saved_name("pwdid001", "dir/f.bin", "f.bin")
n2 = tgbot.gopeed_saved_name("pwdid001", "dir2/f.bin", "f.bin")
check(n1 != n2, "saved_name 同分享同名文件不冲突", f"{n1} vs {n2}")
check(n1.endswith(".bin"), "saved_name 保留扩展名", n1)
check(len(n1.encode()) <= 240, "saved_name 字节上限", len(n1.encode()))
n3 = tgbot.gopeed_saved_name("pwdid001", "x/" + "长" * 200, "长" * 200 + ".mkv")
check(len(n3.encode()) <= 240 and n3.endswith(".mkv"), "saved_name 超长仍保留后缀", len(n3.encode()))

# ---------------- 文本工具 ----------------
eq(tgbot.human(0), "0 字节", "human 0")
eq(tgbot.human(512), "512 字节", "human 字节")
eq(tgbot.human(1536), "1.5 KB", "human KB")
eq(tgbot.bar(0), "░" * 10, "bar 0")
eq(tgbot.bar(100), "█" * 10, "bar 100")
eq(tgbot.bar(55, 10), "██████░░░░", "bar 55")
eq(tgbot.eta(100, 10), "约 10 秒", "eta 秒")
eq(tgbot.eta(200, 1), "约 3 分 20 秒", "eta 分钟")
eq(tgbot.eta(3600, 1), "约 1 小时 0 分", "eta 小时")
eq(tgbot.eta(10, 0), "计算中", "eta 速度0")
check("授权" in tgbot.human_error("invalid_grant"), "human_error 授权", tgbot.human_error("invalid_grant"))
check("磁盘" in tgbot.human_error("No space left on device"), "human_error 磁盘")
check("夸克账号" in tgbot.human_error("download file size limit"), "human_error 23018")
check("限流" in tgbot.human_error("googleapi: Error 403: User rate limit exceeded"),
      "human_error Google 限流")
check("失效" in tgbot.human_error("share has been cancelled"), "human_error 分享失效")
eq(tgbot.status_zh("active"), "下载中", "状态中文 active")
eq(tgbot.status_zh("done"), "已完成", "状态中文 done")
eq(tgbot.drive_label("gdrive:Aria2Files"), "Google Drive › Aria2Files", "网盘名称中文化")
eq(tgbot.human_error(""), "未知原因，请稍后重试", "human_error 空")

# ---------------- _task_paths / _completed_file_exists ----------------
base = Path(tgbot.GOPEED_DLDIR)
base.mkdir(parents=True, exist_ok=True)
info = {"saved_name": "s_abc.bin", "date": "2026-11-01", "share": "分享_x",
        "rel_path": "sub/f.bin", "size": 5}
src, dest = tgbot._task_paths(info)
eq(src, base / "s_abc.bin", "_task_paths 源文件")
check(str(dest).replace("\\", "/").endswith("gdl/2026-11-01/分享_x/sub/f.bin"),
      "_task_paths 归档路径", str(dest))

bad = dict(info, rel_path="../../../../tmp/evil.bin")
srcb, destb = tgbot._task_paths(bad)
base_res = Path(tgbot.GOPEED_DLDIR).resolve()
check(base_res in destb.parents, "_task_paths 穿越被限制在下载根内", str(destb))

try:
    tgbot._task_paths({"saved_name": "", "date": "x"})
    check(False, "_task_paths 缺字段应报错")
except ValueError:
    check(True, "_task_paths 缺字段报错")

# 完整落盘判断
src.write_bytes(b"12345")
check(tgbot._completed_file_exists(info), "_completed_file_exists 源文件")
src.write_bytes(b"123")
check(not tgbot._completed_file_exists(info), "_completed_file_exists 大小不符")
src.unlink()
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_bytes(b"12345")
check(tgbot._completed_file_exists(info), "_completed_file_exists 归档文件")
dest.unlink()

# ---------------- finalize_gopeed_task ----------------
calls = []


def fake_rclone(args, timeout=60):
    calls.append(list(args))
    # 模拟 rclone move：成功时把源文件搬走
    if fake_rclone.rc == 0 and len(args) > 1 and os.path.exists(args[1]):
        try:
            os.remove(args[1])
        except OSError:
            pass
    return fake_rclone.rc, "", fake_rclone.err


fake_rclone.rc, fake_rclone.err = 0, ""
tgbot._rclone_run = fake_rclone

# 成功路径 + 空目录清理
case = TMP / "case1"
shutil.rmtree(case, ignore_errors=True)
gdir = case / "gdl"
gdir.mkdir(parents=True)
srcf = gdir / info["saved_name"]
srcf.write_bytes(b"12345")
info1 = dict(info, saved_name=srcf.name)
tgbot.GOPEED_DLDIR = str(gdir)
ok, kind = tgbot.finalize_gopeed_task("T1", info1)
eq((ok, kind), (True, None), "finalize 成功")
check(not srcf.exists(), "finalize 源文件已移走")
check(not (gdir / "2026-11-01").exists(), "finalize 清理空目录")
eq(len(calls), 1, "finalize 调用一次 rclone")
check(calls[0][0] == "move", "finalize rclone move")

# 大小不符
srcf.write_bytes(b"12")
info2 = dict(info, saved_name=srcf.name)
ok, kind = tgbot.finalize_gopeed_task("T2", info2)
eq((ok, kind), (False, "incomplete"), "finalize 截断判定")
check(not srcf.exists(), "finalize 删除残缺文件")

# 文件不存在
ok, kind = tgbot.finalize_gopeed_task("T3", dict(info, saved_name="nope.bin"))
eq((ok, kind), (False, "missing"), "finalize 缺文件")

# 上传失败（普通）
srcf.write_bytes(b"12345")
fake_rclone.rc, fake_rclone.err = 1, "connection reset by peer"
ok, kind = tgbot.finalize_gopeed_task("T4", dict(info, saved_name=srcf.name))
eq((ok, kind), (False, "upload"), "finalize 上传失败")
check((gdir / "2026-11-01" / "分享_x" / "sub" / "f.bin").exists(),
      "finalize 上传失败保留归档文件（可重试）")

# 上传失败（授权）
fake_rclone.rc, fake_rclone.err = 1, "oauth2: cannot fetch token: 400 Bad Request"
ok, kind = tgbot.finalize_gopeed_task("T5", dict(info, saved_name=srcf.name))
eq((ok, kind), (False, "gdrive"), "finalize 授权失效判定")
fake_rclone.rc, fake_rclone.err = 0, ""

# ---------------- map 原子操作 ----------------
tgbot.GOPEED_MAP = TMP / "map2.json"
tgbot.save_gopeed_map({"a": {"fid": "1"}})
eq(tgbot.load_gopeed_map(), {"a": {"fid": "1"}}, "map 读写")
tgbot.map_add("b", {"fid": "2"})
eq(sorted(tgbot.load_gopeed_map()), ["a", "b"], "map_add")
tgbot.map_replace("a", "c", {"fid": "3"})
eq(sorted(tgbot.load_gopeed_map()), ["b", "c"], "map_replace")
tgbot.map_pop("b")
eq(sorted(tgbot.load_gopeed_map()), ["c"], "map_pop")
tgbot.GOPEED_MAP.write_text("{bad json", encoding="utf-8")
eq(tgbot.load_gopeed_map(), {}, "map 损坏时返回空")
tgbot.save_gopeed_map({})

print(f"checks={CHECKS[0]} failed={len(FAILS)}")
for f in FAILS:
    print(" ✗", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILS else 0)

