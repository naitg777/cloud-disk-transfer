#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram -> Google Drive 转存机器人 (v2.0)

按《机器人 UI 升级方案》重构：
  - 底部常驻键盘（6 个入口）
  - 任务卡片：进度条 + 暂停 / 取消 / 详情 / 重试
  - 任务列表分页
  - 上传进度实时解析 rclone stats
  - 磁盘空间条形图 + 预警
  - 错误码翻译成人话
  - 我的网盘：最近文件 + 生成分享链接
  - 所有状态变更原地编辑，不刷屏
"""
import os
import sys
import re
import time
import json
import shutil
import hashlib
import asyncio
import logging
import subprocess
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quark_share import QuarkShare, QuarkGuest, QuarkError
from pikpak_share import PikPakShare, PikPakError, parse_share as pikpak_parse
from gopeed_client import GopeedClient, GopeedError

import requests as rq
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

# ========== 配置 ==========
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ALLOWED_CHAT = int(os.environ["ALLOWED_CHAT"])
ARIA2_SECRET = os.environ["ARIA2_SECRET"]


def _env_num(name: str, default, cast=int, minimum=1):
    """读数字型环境变量：写错了就用默认值，不让整个 Bot 起不来。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = cast(str(raw).strip())
    except (TypeError, ValueError):
        print("[warn] 环境变量 %s=%r 不是数字，改用默认 %r" % (name, raw, default),
              file=sys.stderr)
        return default
    if val < minimum:
        print("[warn] 环境变量 %s=%r 小于下限 %r，改用默认 %r"
              % (name, raw, minimum, default), file=sys.stderr)
        return default
    return val


RCLONE_DEST  = os.environ.get("RCLONE_DEST", "gdrive:TelegramFiles")
ARIA2_DEST   = os.environ.get("ARIA2_DEST", "gdrive:Aria2Files")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/data/tg_downloads"))
ARIA2_RPC    = os.environ.get("ARIA2_RPC", "http://127.0.0.1:6800/jsonrpc")
ARIA2_DLDIR  = os.environ.get("ARIA2_DLDIR", "/data/aria2/downloads")
RCLONE_BIN   = os.environ.get("RCLONE_BIN", "/usr/bin/rclone")
RCLONE_CONF  = os.environ.get("RCLONE_CONF", "/opt/cloud-disk-transfer/rclone.conf")
FAILED_LOG   = Path(os.environ.get("FAILED_LOG", "/opt/cloud-disk-transfer/logs/failed.log"))

GOPEED_API   = os.environ.get("GOPEED_API", "http://127.0.0.1:9999")
GOPEED_DLDIR = os.environ.get("GOPEED_DLDIR", "/data/gopeed/downloads")
GOPEED_MAP   = Path(os.environ.get("GOPEED_MAP", "/opt/cloud-disk-transfer/logs/gopeed_tasks.json"))
gopeed       = GopeedClient(GOPEED_API)

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
MIN_FREE_BYTES    = 2 * 1024 * 1024 * 1024
WARN_FREE_BYTES   = 5 * 1024 * 1024 * 1024
MAX_SHARE_FILES   = _env_num("MAX_SHARE_FILES", 20000)  # 单个分享最多处理多少个文件
UPLOAD_MAX_RETRY  = 5          # 非授权类上传失败最多重试次数
AUTH_MAX_RETRY    = 6          # 授权类失败每小时重试一次，最多 6 次
LEFTOVER_KEEP_DAYS = 7         # 失败残留文件保留天数（与 cleanup_leftovers.py 一致）
# Gopeed 下载分片连接数：实测吞吐基本正比于连接数（16→0.34 MB/s，32→1.27 MB/s），
# 但小文件连接太多会被握手开销拖慢，所以按大小分档。
GOPEED_CONNS_LARGE  = int(os.environ.get("GOPEED_CONNS_LARGE", 32))
GOPEED_CONNS_MEDIUM = int(os.environ.get("GOPEED_CONNS_MEDIUM", 24))
GOPEED_CONNS_SMALL  = int(os.environ.get("GOPEED_CONNS_SMALL", 16))
UPLOAD_CONCURRENCY  = int(os.environ.get("UPLOAD_CONCURRENCY", 3))
MAP_LOCK          = threading.Lock()


def gopeed_connections(size: int) -> int:
    """按文件大小选择分片连接数。"""
    size = int(size or 0)
    if size >= 100 * 1024 ** 2:
        return GOPEED_CONNS_LARGE
    if size >= 20 * 1024 ** 2:
        return GOPEED_CONNS_MEDIUM
    if size >= 2 * 1024 ** 2:
        return GOPEED_CONNS_SMALL
    return 4
# ==========================

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgbot")
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SEP = "─" * 22

# aria2 错误码
ARIA2_ERRORS = {
    1: "未知错误", 2: "超时", 3: "资源不存在（HTTP 404）",
    4: "反复返回 404，链接大概率已失效", 5: "下载速度过慢，已放弃",
    6: "网络问题（连接失败或被中断）", 7: "未完成的下载",
    8: "服务器不支持断点续传", 9: "磁盘空间不足",
    10: "分片长度与控制文件不符", 11: "重复下载同一任务",
    12: "重复的 info hash", 13: "文件已存在", 14: "重命名失败",
    15: "无法打开已有文件", 16: "无法创建或截断文件",
    17: "文件读写错误（I/O）", 18: "无法创建目录",
    19: "域名解析失败（DNS）", 20: "Metalink 解析失败",
    21: "FTP 命令失败", 22: "HTTP 响应头异常",
    23: "重定向次数过多", 24: "HTTP 认证失败（401 / 403）",
    25: "bencode 解析失败", 26: "种子文件损坏或缺少 info",
    27: "磁力链接格式错误", 28: "无法识别的选项",
    29: "服务器暂时无法处理请求", 30: "JSON-RPC 请求解析失败",
    32: "校验和不匹配（文件损坏）",
}

# 人话错误翻译：把英文报错翻成用户能看懂、能行动的中文
HUMAN_ERRORS = [
    (r"23018|download file size limit",
     "📦 该文件超过当前夸克账号的下载大小限制\n"
     "　 可以试试：升级夸克会员，或让对方换个更小的文件分享"),
    (r"41017", "🙅 这是你自己创建的分享，夸克不允许下载自己的分享"),
    (r"41008|提取码|密码错误", "🔑 提取码不对或缺失，请让对方确认后重发"),
    (r"4100[0-9]|分享已|share.*(cancel|expired|delete)",
     "🔗 分享已失效（被取消、过期或原文件被删），请让对方重新分享"),
    (r"31001|require login|未登录|unauthorized|invalid_grant|"
     r"token has been expired|token expired|401",
     "🔐 网盘登录已过期，需要重新授权"),
    (r"storageQuotaExceeded|storage quota|quota exceeded",
     "💾 Google Drive 空间不足，请先清理网盘"),
    (r"userRateLimitExceeded|rateLimitExceeded|User rate limit|rate limit|"
     r"too many requests|403",
     "⏳ 网盘接口限流（Google 侧），系统会自动重试，稍等即可"),
    (r"No space left", "💾 服务器本地磁盘已满，请先清理"),
    (r"SSL|handshake|tls: ", "🔒 加密连接失败（网络被干扰或节点异常），请重试"),
    (r"connection reset|broken pipe|unexpected EOF|EOF",
     "🌐 连接被中断，请重试"),
    (r"i/o timeout|timed out|timeout|context deadline exceeded",
     "🌐 网络超时，请重试"),
    (r"connection refused|dial tcp|no such host",
     "🔌 服务未响应，请稍后重试"),
    (r"permission denied|access denied", "🚫 权限不足，无法访问目标位置"),
    (r"file exists", "📁 目标文件已存在"),
    (r"not found|404", "🔍 目标文件或路径不存在"),
]


def human_error(text: str) -> str:
    """把各种英文报错翻成中文；实在翻不出来就明确告诉用户该怎么做。"""
    raw = (text or "").strip()
    for pattern, msg in HUMAN_ERRORS:
        if re.search(pattern, raw, re.I):
            return msg
    if not raw:
        return "未知原因，请稍后重试"
    short = raw.splitlines()[0][:80]
    if re.search(r"[\u4e00-\u9fff]", short):
        return short
    return f"操作失败，把下面这行发给我就能排查：\n{short}"


def human(n) -> str:
    """按中文习惯显示体积：1KB 以下用「字节」，其余用 KB/MB/GB/TB。

    整数就不带小数（100 MB），非整数保留一位（15.5 MB）。
    """
    n = float(n or 0)
    if n < 1024:
        return f"{int(n)} 字节"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            if abs(n - round(n)) < 0.05:
                return f"{round(n)} {unit}"
            return f"{n:.1f} {unit}"


def human_cap(n) -> str:
    """容量专用格式：固定两位小数，避免 4.99 TB 被四舍五入成 5 TB 后
    和"总容量 5 TB"看起来一模一样。"""
    n = float(n or 0)
    if n < 1024:
        return f"{int(n)} 字节"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}"

def bar(pct, width: int = 10) -> str:
    pct = max(0, min(100, int(pct)))
    filled = int(pct * width / 100 + 0.5)
    return "█" * filled + "░" * (width - filled)


def eta(remain: int, speed: int) -> str:
    """剩余时间用中文单位：约 3 分 20 秒 / 约 1 小时 5 分。"""
    if speed <= 0:
        return "计算中"
    s = max(0, int(remain / speed))
    if s < 60:
        return f"约 {s} 秒"
    if s < 3600:
        return f"约 {s // 60} 分 {s % 60} 秒"
    return f"约 {s // 3600} 小时 {(s % 3600) // 60} 分"


# 各下载器状态值统一翻成中文，界面上不再出现 active/paused/running
STATUS_ZH = {
    "active": "下载中", "running": "下载中",
    "waiting": "排队中", "queue": "排队中",
    "paused": "已暂停", "pause": "已暂停",
    "error": "已失败", "failed": "已失败",
    "complete": "已完成", "done": "已完成",
    "removed": "已移除", "seeding": "做种中",
}


def status_zh(st: str) -> str:
    return STATUS_ZH.get(str(st or "").lower(), "未知状态")


def drive_label(remote: str) -> str:
    """gdrive:Aria2Files -> Google Drive › Aria2Files，避免直接甩出 gdrive: 写法。"""
    name, _, path = (remote or "").partition(":")
    label = "Google Drive" if name.lower() == "gdrive" else (name or "网盘")
    return f"{label} › {path}" if path else label


def safe_name(name: str, fallback: str = "unnamed", max_bytes: int = 200) -> str:
    """清洗文件名，并按 UTF-8 字节数截断（Linux/GDrive 单段上限 255 字节）。

    修复：截断时先分离扩展名，仅截断主体，再拼回扩展名，避免后缀丢失。
    """
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[\x00-\x1f:*?"<>|]', "_", name).rstrip(" .")
    stem, ext = os.path.splitext(name)
    ext = ext[:16]  # 扩展名最长 16 字节（含点）
    # 只截断主体，保证 stem+ext 的 UTF-8 总字节不超过 max_bytes
    max_stem = max_bytes - len(ext.encode("utf-8"))
    raw = stem.encode("utf-8")
    if len(raw) > max_stem:
        stem = raw[:max(max_stem, 0)].decode("utf-8", "ignore").rstrip(" .")
    name = stem + ext
    return name.rstrip(" .") or fallback


def safe_rel_path(path: str, fallback: str = "unnamed") -> Path:
    """把分享内的相对路径拆成安全的分段，阻止 ../ 逃逸。"""
    parts = []
    for part in (path or "").replace("\\", "/").split("/"):
        clean = safe_name(part, fallback="", max_bytes=200)
        if clean and clean not in (".", ".."):
            parts.append(clean)
    return Path(*parts) if parts else Path(fallback)


def is_allowed(update: Update) -> bool:
    return update.effective_chat.id == ALLOWED_CHAT


# ==================== aria2 ====================

def _aria2(method: str, params: list, timeout: int = 10):
    payload = {"jsonrpc": "2.0", "id": "1", "method": method,
               "params": [f"token:{ARIA2_SECRET}", *params]}
    data = rq.post(ARIA2_RPC, json=payload, timeout=timeout).json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "rpc error"))
    return data.get("result")


def _task_name(task: dict) -> str:
    info = (task.get("bittorrent") or {}).get("info") or {}
    if info.get("name"):
        return info["name"]
    files = task.get("files") or [{}]
    return os.path.basename(files[0].get("path", "")) or "未知任务"


def _err_text(task: dict) -> str:
    code = int(task.get("errorCode") or 0)
    msg = (task.get("errorMessage") or "").strip()
    desc = ARIA2_ERRORS.get(code, f"未知错误码 {code}")
    return desc + (f"\n   ↳ 系统原始提示：{msg[:90]}" if msg else "")


def _progress_of(task: dict):
    total = int(task.get("totalLength") or 0)
    done  = int(task.get("completedLength") or 0)
    pct   = int(done / total * 100) if total else 0
    return total, done, pct


# ==================== rclone ====================

def _rclone_run(args: list, timeout: int = 60):
    try:
        r = subprocess.run([RCLONE_BIN, "--config", RCLONE_CONF, *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as exc:
        return 1, "", str(exc)


def _cloud_recent(limit: int = 10):
    rows = []
    for remote in (RCLONE_DEST, ARIA2_DEST):
        # 归档结构是 日期/分享名/子目录/文件，至少要到第 4 层；用 10 层兼顾深度
        rc, out, _ = _rclone_run(["lsl", remote, "--max-depth", "10"], timeout=60)
        if rc != 0:
            continue
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4:
                size, date, tm, rel_path = parts
                rows.append((f"{date} {tm}", rel_path, int(size or 0), remote))
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[:limit]


async def _upload_with_progress(msg, path: Path):
    """执行 rclone move 并实时解析 --stats 输出更新消息"""
    cmd = [RCLONE_BIN, "--config", RCLONE_CONF, "move", str(path), RCLONE_DEST,
           "--transfers", "1", "--checkers", "4", "--buffer-size", "16M",
           "--retries", "3", "--stats", "2s", "--stats-one-line",
           # rclone 的 stats 属于 INFO 级别，必须单独提升到 NOTICE，
           # 否则会被 --log-level NOTICE 一起压掉，进度条永远停在 0%
           "--stats-log-level", "NOTICE",
           "--log-level", "NOTICE"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    pat = re.compile(r"([\d.]+)\s*([KMGT]?i?B)\s*/\s*([\d.]+)\s*([KMGT]?i?B),\s*(\d+)%")
    tail, last_edit = [], 0.0

    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if line:
            tail = (tail + [line])[-15:]
        m = pat.search(line)
        if m and time.monotonic() - last_edit > 3:
            last_edit = time.monotonic()
            pct = int(m.group(5))
            try:
                await msg.edit_text(
                    f"⬆️ 正在保存到网盘：{safe_name(path.name)[:40]}\n"
                    f"{bar(pct)} {pct}%\n"
                    f"已上传 {m.group(1)} {m.group(2)} / 共 {m.group(3)} {m.group(4)}"
                )
            except BadRequest:
                pass

    rc = await proc.wait()
    return rc, "\n".join(tail[-4:])


# ==================== 键盘 ====================

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📥 新建下载"), KeyboardButton("📋 任务列表")],
        [KeyboardButton("☁️ 我的网盘"), KeyboardButton("💾 磁盘空间")],
        [KeyboardButton("⚙️ 设置"), KeyboardButton("❓ 帮助")],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

MENU_TEXT = (
    f"🤖 网盘助手\n{SEP}\n\n"
    "把下面这些东西直接发给我，会自动下载并保存到谷歌云盘（Google Drive）：\n\n"
    "· 夸克网盘分享链接\n"
    "· 磁力链接 / 直链地址\n"
    f"· 文件（单个不超过 {human(TG_DOWNLOAD_LIMIT)}）\n\n"
    "也可以点下面的按钮操作 👇"
)


def kb_task(task: dict) -> InlineKeyboardMarkup:
    gid = task.get("gid", "")
    st = task.get("status")
    if st == "active":
        row = [InlineKeyboardButton("⏸️ 暂停", callback_data=f"pause:{gid}"),
               InlineKeyboardButton("❌ 取消", callback_data=f"cancel:{gid}")]
    elif st == "paused":
        row = [InlineKeyboardButton("▶️ 继续", callback_data=f"unpause:{gid}"),
               InlineKeyboardButton("❌ 取消", callback_data=f"cancel:{gid}")]
    elif st == "error":
        row = [InlineKeyboardButton("🔄 重试", callback_data=f"retry:{gid}"),
               InlineKeyboardButton("🗑 移除", callback_data=f"drop:{gid}")]
    else:
        row = [InlineKeyboardButton("🗑 移除", callback_data=f"drop:{gid}")]
    row.append(InlineKeyboardButton("🔍 详情", callback_data=f"detail:{gid}"))
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton("🔄 刷新进度", callback_data=f"card:{gid}")],
    ])


def kb_back(extra=None, src: str = "menu"):
    rows = []
    if extra:
        rows.append(extra)
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data=f"rf:{src}"),
                 InlineKeyboardButton("⬅️ 菜单", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


# ==================== 卡片 ====================

def card_task(task: dict) -> str:
    st = task.get("status")
    name = _task_name(task)[:34]
    total, done, pct = _progress_of(task)

    if st == "active":
        spd = int(task.get("downloadSpeed") or 0)
        return (f"📥 正在下载：{name}\n"
                f"{bar(pct)} {pct}%\n"
                f"⬇️ 速度 {human(spd)}/秒  ·  ⏱️ 剩余{eta(total - done, spd)}\n"
                f"已下载 {human(done)} / 共 {human(total)}")

    if st == "paused":
        return (f"⏸️ 已暂停：{name}\n"
                f"{bar(pct)} {pct}%\n"
                f"已下载 {human(done)} / 共 {human(total)}\n"
                f"点「▶️ 继续」可以接着下")

    if st == "waiting":
        return f"⏳ 排队中：{name}\n文件大小 {human(total)}"

    if st == "error":
        return (f"❌ 下载失败：{name}\n"
                f"{_err_text(task)}\n"
                f"📊 已下载 {human(done)} / 共 {human(total)}\n"
                f"可以点「🔄 重试」再试一次")

    if st == "complete":
        return (f"✅ 下载完成：{name}\n"
                f"📊 共 {human(total)}\n"
                f"稍后会自动上传到网盘")

    return f"🗑 {name}\n当前状态：{status_zh(st)}"


def card_detail(task: dict) -> str:
    total, done, pct = _progress_of(task)
    spd = int(task.get("downloadSpeed") or 0)
    lines = [
        f"🔍 任务详情\n{SEP}\n",
        f"文件名：{_task_name(task)[:40]}",
        f"状态　：{status_zh(task.get('status'))}",
        f"大小　：{human(total)}",
        f"已下载：{human(done)}（{pct}%）",
        f"速度　：{human(spd)}/秒",
        f"连接数：{task.get('connections', '—')} 条",
    ]
    d = task.get("dir")
    if d:
        lines.append(f"存在服务器：{d}")
    lines.append(f"任务编号：{task.get('gid', '—')}")
    if task.get("status") == "error":
        lines += ["", f"{SEP}", f"❌ 失败原因\n{_err_text(task)}"]
    return "\n".join(lines)


def view_list(page: int = 0, per: int = 5):
    """合并 aria2 与 Gopeed 的任务列表"""
    tasks = []
    aria2_err = None

    # ---- aria2 任务 ----
    try:
        active = _aria2("aria2.tellActive", [])
        waiting = _aria2("aria2.tellWaiting", [0, 50])
        stopped = _aria2("aria2.tellStopped", [0, 50])
        aria_icons = {"active": "⬇️", "waiting": "⏳", "paused": "⏸️",
                      "error": "❌", "complete": "✅", "removed": "🗑"}
        for t in active + waiting + stopped:
            # 已完成/已移除的不再展示：aria2 会把它们一直留在 tellStopped 里，
            # 全列出来就会让任务清单越挂越多、看着像"永远下不完"。
            if t.get("status") in ("complete", "removed"):
                continue
            if t.get("status") == "error":
                gid = t.get("gid") or ""
                first = _aria2_err_seen.setdefault(gid, time.time())
                if time.time() - first > 3600:
                    continue          # 失败超过 1 小时的不再占位
            total, done, pct = _progress_of(t)
            tasks.append({
                "icon": aria_icons.get(t.get("status"), "•"),
                "name": _task_name(t)[:26],
                "pct": pct,
                "detail": human(total),
                "tag": "aria2",
            })
    except Exception as exc:
        aria2_err = str(exc)[:60]

    # ---- Gopeed 任务 ----
    try:
        mapping = load_gopeed_map()
        g_icons = {"running": "⬇️", "waiting": "⏳", "pause": "⏸️",
                   "error": "❌", "done": "✅", "removed": "🗑"}
        for t in gopeed.list_tasks():
            tid = t.get("id") or ""
            info = mapping.get(tid, {})
            name = (info.get("orig_name") or info.get("saved_name")
                    or "未命名任务")
            prog = t.get("progress") or {}
            done = prog.get("downloaded") or 0
            total = info.get("size") or 0
            speed = prog.get("speed") or 0
            pct = int(done * 100 / total) if total else 0
            if total:
                detail = f"{human(done)} / 共 {human(total)} · {human(speed)}/秒"
            else:
                detail = f"{human(done)} · {human(speed)}/秒"
            tasks.append({
                "icon": g_icons.get(t.get("status"), "•"),
                "name": name[:26],
                "pct": pct,
                "detail": detail,
                "tag": "Gopeed",
            })
    except Exception as exc:
        log.warning("查询 Gopeed 任务失败: %s", exc)

    if not tasks:
        msg = f"📋 任务列表\n{SEP}\n\n✅ 现在没有正在跑的任务\n发个链接或文件就能开工"
        if aria2_err:
            msg += "\n\n⚠️ 下载服务暂时连不上，过一会儿会自己恢复"
        return msg, kb_back()

    pages = max(1, (len(tasks) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = tasks[page * per:(page + 1) * per]

    header = f"📋 任务列表（第 {page + 1}/{pages} 页，共 {len(tasks)} 个）\n{SEP}"
    entries = []
    for i, t in enumerate(chunk, start=page * per + 1):
        entries.append(
            f"{i}. {t['icon']} {t['name']}\n"
            f"   {bar(t['pct'], 8)} {t['pct']}% · {t['detail']}")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"list:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"list:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data=f"list:{page}"),
                 InlineKeyboardButton("⬅️ 菜单", callback_data="menu")])
    return header + "\n\n" + "\n\n".join(entries), InlineKeyboardMarkup(rows)


def _disk_usage():
    """读磁盘用量；下载目录万一被删了也不至于让整个按钮报错。"""
    for p in (str(DOWNLOAD_DIR), "/data", "/"):
        try:
            return shutil.disk_usage(p)
        except OSError:
            continue
    return None


_DRIVE_ABOUT_CACHE: dict = {"t": 0.0, "v": None}


def _drive_about():
    """查 Google Drive 的容量（60 秒缓存，避免每次点按钮都调一次 API）。"""
    now = time.time()
    if now - _DRIVE_ABOUT_CACHE["t"] < 60:
        return _DRIVE_ABOUT_CACHE["v"]
    val = None
    rc, out, _ = _rclone_run(["about", "gdrive:", "--json"], timeout=25)
    if rc == 0 and out:
        try:
            d = json.loads(out)
            total = int(d.get("total") or 0)
            used = int(d.get("used") or 0)
            free = d.get("free")
            free = int(free) if free is not None else max(0, total - used)
            if total:
                val = (total, used, free)
        except Exception:
            val = None
    _DRIVE_ABOUT_CACHE["t"] = now
    _DRIVE_ABOUT_CACHE["v"] = val
    return val


def view_disk() -> str:
    usage = _disk_usage()
    if usage is None:
        return (f"💾 空间占用\n{SEP}\n\n"
                f"⚠️ 读不到服务器磁盘信息，可能目录被删了，稍后再试")
    total = usage.total or 0
    free = usage.free or 0
    # 用 total - free 反推已占用，保证「已占用 + 还剩余 = 总容量」，
    # 否则会像 df 那样因保留块差出几十 MB，看起来像算错了。
    used = max(0, total - free)
    pct = int(used / total * 100) if total else 0
    if free < MIN_FREE_BYTES:
        warn = "🔴 空间严重不足，新任务会被拒绝，请先清理"
    elif free < WARN_FREE_BYTES:
        warn = "🟠 空间有点紧张，建议清理一下"
    else:
        warn = "🟢 空间充足，可以放心下载"

    drive = _drive_about()
    if drive:
        d_total, d_used, d_free = drive
        d_pct = (d_used / d_total * 100) if d_total else 0
        # 云盘动辄 5TB，"总容量 5 TB / 还剩余 5 TB" 会看起来一模一样，
        # 所以这里改成「已用 x / 共 y」的写法，数字天然不会重复。
        drive_block = (
            f"\n☁️ 谷歌云盘（Google Drive）\n"
            f"{bar(d_pct, 14)} 已用 {d_pct:.2f}%\n"
            f"已用 {human(d_used)} / 共 {human(d_total)}\n"
        )
    else:
        drive_block = "\n☁️ 谷歌云盘：暂时查不到（稍后刷新看看）\n"

    now_bj = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))
    return (
        f"💾 空间占用\n{SEP}\n\n"
        f"🖥 服务器磁盘（下载临时占用）\n"
        f"{bar(pct, 14)} 已用 {pct}%\n\n"
        f"总容量：{human_cap(total)}\n"
        f"已占用：{human_cap(used)}\n"
        f"还剩余：{human_cap(free)}\n"
        f"{drive_block}\n"
        f"{_quark_block()}"
        f"统计时间：{now_bj}（北京时间）\n"
        f"{SEP}\n{warn}"
    )


def _one_quark_block(kind: str, title: str) -> str:
    c = _quark_quota_cache.get(kind) or {}
    if not c.get("total"):
        return f"☁️ {title}：暂时查不到\n\n"
    total, used = c["total"], c["used"]
    free = max(0, total - used)
    pct = (used / total * 100) if total else 0
    if free < QUARK_MIN_FREE_MB * 1024 * 1024:
        tip = "🔴 空间不够，大文件只能搁着"
    else:
        tip = "🟢 可用"
    return (f"☁️ {title}\n"
            f"{bar(pct, 14)} 已用 {pct:.2f}%\n"
            f"已用 {human(used)} / 共 {human(total)}（剩 {human(free)}）\n"
            f"{tip}\n\n")


def _quark_block() -> str:
    """两个账号的容量：主号（下大文件/兜底）+ 专用转存号（省主号空间）。"""
    out = _one_quark_block("main", "夸克主号（兜底转存）")
    if bridge_account_ready():
        out += _one_quark_block("bridge", "夸克转存号（小文件中转）")
    return out


def view_cloud() -> str:
    rows = _cloud_recent(10)
    if not rows:
        return f"☁️ 我的网盘\n{SEP}\n\n网盘里还没有文件\n上传完成后会显示在这里"
    entries = []
    for ts, rel_path, size, remote in rows:
        name = os.path.basename(rel_path)
        parent = os.path.dirname(rel_path)
        where = drive_label(remote) + (f" › {parent.replace('/', ' › ')}" if parent else "")
        entries.append(f"📄 {name[:28]}\n   {human(size)} · {ts[:16]}\n   位置：{where}")
    return f"☁️ 最近上传（{len(rows)} 个）\n{SEP}\n\n" + "\n\n".join(entries)


def view_settings() -> str:
    usage = shutil.disk_usage(str(DOWNLOAD_DIR))
    try:
        _aria2("aria2.getVersion", [])
        rc_a = "✅ 正常"
    except Exception:
        rc_a = "❌ 连不上"
    # 修复：增加 Gopeed 服务探活，避免 Gopeed 挂了界面仍显示正常
    try:
        gopeed.info()
        rc_g = "✅ 正常"
    except Exception:
        rc_g = "❌ 连不上"
    return (
        f"⚙️ 设置与状态\n{SEP}\n\n"
        f"📤 Telegram 文件保存到\n   {drive_label(RCLONE_DEST)}\n"
        f"📥 链接 / 分享下载后保存到\n   {drive_label(ARIA2_DEST)}\n\n"
        f"📎 单个文件上限\n   {human(TG_DOWNLOAD_LIMIT)}（Telegram 官方限制）\n"
        f"💾 磁盘安全线\n   低于 {human(MIN_FREE_BYTES)} 不再接新任务\n"
        f"🟠 空间预警线\n   低于 {human(WARN_FREE_BYTES)} 会提示清理\n\n"
        f"{SEP}\n"
        f"🔌 下载服务（Aria2）：{rc_a}\n"
        f"🔌 下载服务（Gopeed）：{rc_g}\n"
        f"💽 服务器剩余空间：{human(usage.free)}"
    )


def view_help() -> str:
    return (
        f"❓ 使用帮助\n{SEP}\n\n"
        "① 夸克网盘分享链接\n"
        "   形如 https://pan.quark.cn/s/xxxxx\n"
        "   有提取码也可以一起发过来\n\n"
        "② PikPak 分享链接\n"
        "   形如 https://mypikpak.com/s/xxxxx\n"
        "   视频可直接下；zip 等需要配置登录\n\n"
        "③ 磁力链接\n"
        "   形如 magnet:?xt=urn:btih:...\n\n"
        "④ 直链地址\n"
        "   形如 https://example.com/file.zip\n\n"
        "⑤ 直接把文件发给我\n"
        f"   单个不超过 {human(TG_DOWNLOAD_LIMIT)}\n\n"
        f"{SEP}\n"
        "📁 保存位置\n"
        f"   链接 / 分享 → {drive_label(ARIA2_DEST)} › 日期 › 分享名\n"
        f"   Telegram 文件 → {drive_label(RCLONE_DEST)}\n"
        "   （分享名后面那 6 位字符是自动加的，用来防止不同分享重名）\n\n"
        "🧩 大分享怎么处理\n"
        "   一批文件总量超过服务器空间也没关系：\n"
        "   会边下载边上传、分几批自动下完，最后都放进同一个文件夹\n"
        "   只有单个文件本身比剩余空间还大时才会跳过\n\n"
        "⏱️ 速度参考\n"
        "   夸克分享约 3–4 MB/秒（受夸克限速影响）\n"
        "   直链 / 磁力约 8–9 MB/秒\n\n"
        "⚠️ 暂不支持\n"
        "   • 百度网盘、阿里云盘等其它网盘链接\n"
        "   • 需要登录才能下载的地址\n"
        f"   • 超过 {human(TG_DOWNLOAD_LIMIT)} 的 Telegram 文件\n"
        "     （这种请改用直链或磁力）"
    )


# ==================== 命令 ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(MENU_TEXT, reply_markup=MAIN_MENU)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(MENU_TEXT, reply_markup=MAIN_MENU)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text, kb = await asyncio.to_thread(view_list, 0)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_disk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await _quark_quota("main")
    _bridge_refresh = bridge_account_ready()
    if _bridge_refresh:
        await _quark_quota("bridge")
    await update.message.reply_text(view_disk(), reply_markup=kb_back(src="disk"))


# ==================== PikPak 登录（可选，压缩包才需要） ====================
# PikPak 的 refresh_token 每次刷新都会轮换，所以必须落盘保存最新值，
# 只放在环境变量里的话，重启一次就失效了。
PIKPAK_TOKEN_FILE = Path(os.environ.get(
    "PIKPAK_TOKEN_FILE", "/opt/cloud-disk-transfer/logs/pikpak_token.json"))


def _read_pikpak_token() -> str:
    try:
        d = json.loads(PIKPAK_TOKEN_FILE.read_text(encoding="utf-8"))
        tok = (d.get("refresh_token") or "").strip()
        if tok:
            return tok
    except Exception:
        pass
    return os.environ.get("PIKPAK_REFRESH_TOKEN", "").strip()


def _save_pikpak_token(tok: str) -> None:
    if not tok:
        return
    try:
        PIKPAK_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        PIKPAK_TOKEN_FILE.write_text(
            json.dumps({"refresh_token": tok, "ts": int(time.time())}),
            encoding="utf-8")
        os.chmod(PIKPAK_TOKEN_FILE, 0o600)
    except Exception as exc:
        log.warning("保存 PikPak token 失败: %s", exc)


async def cmd_pikpak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PikPak 状态：有没有配登录（决定能不能下压缩包）。"""
    if not is_allowed(update):
        return
    token = _read_pikpak_token()
    if not token:
        await update.message.reply_text(
            f"📦 PikPak 状态\n{SEP}\n\n"
            f"当前：🔓 未登录模式（只能下视频）\n\n"
            f"想下压缩包（zip/rar/7z）必须登录你的 PikPak 账号：\n"
            f"1. 无痕窗口打开 mypikpak.com 并登录\n"
            f"2. F12 → 控制台执行：\n"
            f"   JSON.parse(localStorage.getItem(\"credentials_YUMx5nI8ZU8Ap8pm\")).refresh_token\n"
            f"3. 把输出的字符串发给我，或者粘到服务器上执行：\n"
            f"   python3 /opt/cloud-disk-transfer/pikpak_setup.py '<token>'\n"
            f"4. 配好后这里会显示 ✅")
        return
    try:
        cli = PikPakShare(refresh_token=token)
        ok = await asyncio.to_thread(cli.ensure_login)
        if not ok:
            raise PikPakError("token 为空")
        await update.message.reply_text(
            f"📦 PikPak 状态\n{SEP}\n\n"
            f"✅ 已登录（可下视频 + 压缩包）\n"
            f"账号 ID：{cli.user_id[:8] + '…' if cli.user_id else '未知'}\n\n"
            f"压缩包会先转存到你的 PikPak 网盘再取直链，"
            f"用完不占空间（系统不管它，建议自己定期清）")
    except Exception as exc:
        await update.message.reply_text(
            f"📦 PikPak 状态\n{SEP}\n\n"
            f"❌ 登录已失效：{human_error(str(exc))}\n\n"
            f"重新拿一个 refresh_token 发给我，或执行：\n"
            f"python3 /opt/cloud-disk-transfer/pikpak_setup.py '<新token>'")


# ==================== 按钮回调 ====================

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat.id != ALLOWED_CHAT:
        await q.answer("无权限", show_alert=True)
        return

    data = q.data or ""
    await q.answer()

    try:
        if data == "menu":
            await q.edit_message_text(MENU_TEXT, reply_markup=kb_back())
            return
        if data.startswith("rf:"):
            src = data.split(":", 1)[1]
            if src == "disk":
                await _quark_quota("main")
                _bridge_refresh = bridge_account_ready()
                if _bridge_refresh:
                    await _quark_quota("bridge")
                await q.edit_message_text(view_disk(), reply_markup=kb_back(src="disk"))
            elif src == "cloud":
                t = await asyncio.to_thread(view_cloud)
                await q.edit_message_text(t, reply_markup=kb_back(
                    [InlineKeyboardButton("🔗 最新文件分享链接", callback_data="link:latest")],
                    src="cloud"))
            elif src == "settings":
                await q.edit_message_text(view_settings(), reply_markup=kb_back(src="settings"))
            elif src == "help":
                await q.edit_message_text(view_help(), reply_markup=kb_back(src="help"))
            else:
                await q.edit_message_text(MENU_TEXT, reply_markup=kb_back())
            return
        if data.startswith("list:"):
            page = int(data.split(":", 1)[1])
            text, kb = await asyncio.to_thread(view_list, page)
            await q.edit_message_text(text, reply_markup=kb)
            return

        action, gid = (data.split(":", 1) + [""])[:2]

        if action == "detail":
            t = await asyncio.to_thread(_aria2, "aria2.tellStatus", [gid])
            await q.edit_message_text(card_detail(t), reply_markup=kb_task(t))
            return

        if action == "pause":
            await asyncio.to_thread(_aria2, "aria2.pause", [gid])
        elif action == "unpause":
            await asyncio.to_thread(_aria2, "aria2.unpause", [gid])
        elif action == "cancel":
            await asyncio.to_thread(_aria2, "aria2.forceRemove", [gid])
        elif action == "drop":
            await asyncio.to_thread(_aria2, "aria2.removeDownloadResult", [gid])
            await q.edit_message_text("🗑 已经从列表里删掉了", reply_markup=kb_back())
            return
        elif action == "retry":
            st = await asyncio.to_thread(_aria2, "aria2.tellStatus", [gid, ["files"]])
            uris = []
            for f in st.get("files", []):
                for u in f.get("uris", []):
                    if u.get("uri") and u["uri"] not in uris:
                        uris.append(u["uri"])
            if not uris:
                await q.edit_message_text(
                    "⚠️ 这个任务没留下原始链接，没法自动重试\n"
                    "把链接重新发我一次就行", reply_markup=kb_back())
                return
            new_gid = await asyncio.to_thread(
                _aria2, "aria2.addUri", [uris, {"dir": ARIA2_DLDIR}])
            await q.edit_message_text(
                "🔄 已经重新排队下载了\n"
                f"任务编号 {str(new_gid)[:8]}\n"
                "发「📋 任务列表」可以看进度", reply_markup=kb_back())
            return

        await asyncio.sleep(0.6)
        t = await asyncio.to_thread(_aria2, "aria2.tellStatus", [gid])
        await q.edit_message_text(card_task(t), reply_markup=kb_task(t))

    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            log.warning("edit failed: %s", exc)
    except Exception as exc:
        log.exception("button failed")
        try:
            await q.edit_message_text(f"⚠️ 这个操作没成功\n{human_error(str(exc))}",
                                      reply_markup=kb_back())
        except Exception:
            pass


# ==================== 文件 ====================

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    m = update.message
    if m.document:
        file_obj, raw_name = m.document, m.document.file_name
    elif m.video:
        file_obj, raw_name = m.video, f"video_{m.video.file_id}.mp4"
    elif m.audio:
        file_obj, raw_name = m.audio, m.audio.file_name or f"audio_{m.audio.file_id}.mp3"
    elif m.photo:
        file_obj, raw_name = m.photo[-1], f"photo_{m.photo[-1].file_id}.jpg"
    elif m.voice:
        file_obj, raw_name = m.voice, f"voice_{m.voice.file_id}.ogg"
    else:
        return

    file_name = safe_name(raw_name)
    size = int(getattr(file_obj, "file_size", 0) or 0)

    if size > TG_DOWNLOAD_LIMIT:
        await m.reply_text(
            f"📎 这个文件有 {human(size)}，超过 Telegram 允许机器人下载的 "
            f"{human(TG_DOWNLOAD_LIMIT)} 上限\n\n"
            f"换个办法：\n"
            f"· 要是有直链，直接发链接给我\n"
            f"· 或者发磁力链接\n"
            f"· 也可以先传到夸克网盘，再把分享链接发我"
        )
        return

    usage = await asyncio.to_thread(shutil.disk_usage, str(DOWNLOAD_DIR))
    if usage.free - size < MIN_FREE_BYTES:
        await m.reply_text(
            f"💾 服务器空间不够了（现在剩 {human(usage.free)}）\n"
            f"需要至少留出 {human(MIN_FREE_BYTES)} 的余量，请先清理")
        return

    msg = await m.reply_text(f"⬇️ 正在接收：{file_name}\n{bar(0)} 0%")
    local_path = DOWNLOAD_DIR / file_name
    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        await tg_file.download_to_drive(str(local_path))
    except Exception as exc:
        log.exception("download failed")
        await msg.edit_text(f"❌ 接收失败\n{human_error(str(exc))}")
        return

    await msg.edit_text(f"⬆️ 正在保存到网盘：{file_name}\n{bar(0)} 0%")
    rc, tail = await _upload_with_progress(msg, local_path)

    if rc == 0:
        await msg.edit_text(
            f"✅ 已保存到网盘\n"
            f"📄 {file_name}\n"
            f"📊 大小 {human(size)}\n"
            f"📁 位置 {drive_label(RCLONE_DEST)}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("☁️ 看看网盘里有什么", callback_data="cloud"),
                InlineKeyboardButton("❓ 帮助", callback_data="help"),
            ]]),
        )
    else:
        if is_auth_error(tail, "gdrive"):
            await send_auth_alert("gdrive", tail)
        await msg.edit_text(
            f"❌ 保存失败：{file_name}\n"
            f"{human_error(tail)}\n\n"
            f"文件我留在服务器上了（{DOWNLOAD_DIR}）\n"
            f"{LEFTOVER_KEEP_DAYS} 天内还可以补传，过期会自动清理"
        )


# ==================== 授权失效告警 ====================

ALERT_STATE = Path(os.environ.get("ALERT_STATE", "/opt/cloud-disk-transfer/logs/alert_state.json"))

AUTH_PATTERNS = {
    "gdrive": ["invalid_grant", "token has been expired", "token expired",
               "failed to load config", "oauth2: cannot fetch token"],
    # 注意：不要用裸的 "401" / "登录" 做匹配——夸克的报错里常带十六进制 token，
    # 里面很容易出现 401 这三个字符，会造成「授权掉了」的误报（2026-11-05 踩过）。
    "quark": ["require login", "未登录", "登录已过期", "登录失效",
              "code=401", "unauthorized", "31001"],
}

AUTH_NAMES = {"gdrive": "Google Drive（谷歌云盘）授权", "quark": "夸克网盘登录状态"}

AUTH_HINTS = {
    "gdrive": ("怎么恢复：\n"
               "在服务器上执行下面这条命令，按提示在浏览器里点「允许」：\n"
               "rclone config reconnect gdrive:\n"
               "（需要我帮忙的话，直接把这条消息转发给我）"),
    "quark": ("怎么恢复：\n"
              "1. 浏览器登录 pan.quark.cn，按 F12 → 应用 → Cookie，复制完整 Cookie\n"
              "2. 把新 Cookie 更新到 OpenList 的夸克存储配置里\n"
              "（改完不用重启，下一次解析自动生效）"),
}


def is_auth_error(text: str, kind: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in AUTH_PATTERNS.get(kind, []))


def _alert_allowed(kind: str, cooldown: int = 3600) -> bool:
    """同类告警在 cooldown 秒内只发一次"""
    try:
        state = json.loads(ALERT_STATE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    now = int(time.time())
    try:
        last = int(state.get(kind, 0))
    except (TypeError, ValueError):
        last = 0
    if now - last < cooldown:
        return False
    state[kind] = now
    try:
        ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ALERT_STATE.with_name(ALERT_STATE.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ALERT_STATE)
    except Exception as exc:
        # 不能静默失败，否则同一告警会每 5 秒刷一次
        log.error("写入告警状态失败，冷却可能失效: %s", exc)
    return True


async def send_auth_alert(kind: str, detail: str) -> None:
    """发送授权失效告警（异步）"""
    if not _bot_app:
        return
    if not _alert_allowed(kind):
        log.info("授权告警 %s 处于冷却期，跳过", kind)
        return
    name = AUTH_NAMES.get(kind, kind)
    hint = AUTH_HINTS.get(kind, "")
    try:
        await _bot_app.bot.send_message(
            chat_id=ALLOWED_CHAT,
            text=(f"🚨 网盘授权掉了，暂时存不了新文件\n{SEP}\n\n"
                  f"哪里出问题：{name}\n"
                  f"具体情况：{human_error(detail)}\n\n"
                  f"{hint}\n\n"
                  f"（这条提醒 1 小时内只发一次，不用重复处理）"))
        log.warning("已发送授权告警: %s", kind)
    except Exception as exc:
        log.error("发送授权告警失败: %s", exc)


async def _notify(text: str) -> None:
    """给管理员发通知；发送失败只记日志，不能影响 watcher 主循环。"""
    if not _bot_app:
        return
    try:
        await _bot_app.bot.send_message(chat_id=ALLOWED_CHAT, text=text)
    except Exception as exc:
        log.error("发送通知失败: %s", exc)


# ==================== Gopeed 任务管理 ====================

def load_gopeed_map() -> dict:
    try:
        data = json.loads(GOPEED_MAP.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.error("读取 Gopeed 映射失败: %s", exc)
        return {}


def save_gopeed_map(mapping: dict) -> bool:
    """原子写入，避免进程中断时留下半个 JSON 文件。"""
    try:
        GOPEED_MAP.parent.mkdir(parents=True, exist_ok=True)
        tmp = GOPEED_MAP.with_name(GOPEED_MAP.name + ".tmp")
        tmp.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, GOPEED_MAP)
        return True
    except Exception as exc:
        log.error("保存 Gopeed 映射失败: %s", exc)
        return False


# 所有映射修改都走下面四个函数：读-改-写在同一把锁内完成。
# 不能像旧代码那样在 await / 长时间上传之前加载一份 map，最后再把整份旧 map 写回去，
# 否则会覆盖期间新增的任务（用户发新链接后任务永远不上传）。
def map_add(tid: str, info: dict) -> None:
    with MAP_LOCK:
        mapping = load_gopeed_map()
        mapping[tid] = info
        save_gopeed_map(mapping)


def map_add_many(items: list[tuple[str, dict]]) -> None:
    """一次写入多个任务，避免大分享逐条读写 JSON 造成 O(n²) I/O。"""
    if not items:
        return
    with MAP_LOCK:
        mapping = load_gopeed_map()
        for tid, info in items:
            mapping[tid] = info
        save_gopeed_map(mapping)


def map_pop(tid: str) -> None:
    with MAP_LOCK:
        mapping = load_gopeed_map()
        if tid not in mapping:
            return
        mapping.pop(tid, None)
        save_gopeed_map(mapping)


def map_replace(old_tid: str, new_tid: str, info: dict) -> None:
    with MAP_LOCK:
        mapping = load_gopeed_map()
        mapping.pop(old_tid, None)
        mapping[new_tid] = info
        save_gopeed_map(mapping)


def map_update(tid: str, info: dict) -> bool:
    with MAP_LOCK:
        mapping = load_gopeed_map()
        if tid not in mapping:
            return False
        mapping[tid] = info
        save_gopeed_map(mapping)
        return True


# ==================== 分批队列（大分享拆波下载） ====================
# 为什么需要它：链路是「下载 → 上传 → 立刻删本地」，任何时刻磁盘上只有少数几个
# 文件。所以「整批文件总量 > 磁盘」并不代表做不了，按波次喂给下载器即可。
# 默认跟任务映射放同一个目录：测试里只要覆盖 GOPEED_MAP，队列也会跟着隔离，
QUEUE_FILE = Path(os.environ.get("GOPEED_QUEUE", str(GOPEED_MAP.parent / "gopeed_queue.json")))
QUEUE_LOCK = threading.Lock()
WAVE_FILL_RATIO = 0.6      # 每波最多占用「可用空间 - 安全线」的 60%，留出缓冲
# 每波建多少个任务：Gopeed 同时只跑 3 个，队列里留 40 个足够喂饱它，
# 再大只会让单波耗时变长（每个任务之间有节流间隔），反而拖慢整体吞吐。
MAX_WAVE_FILES = _env_num("MAX_WAVE_FILES", 40)
FEED_AT_MAP_SIZE = 2       # 在途任务少于这个数时补下一波
WAVE_PACE = _env_num("WAVE_PACE", 0.2, float, minimum=0.0)   # 每建一个任务之间的间隔（秒）


def load_queue() -> dict:
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.error("读取分批队列失败: %s", exc)
        return {}


def save_queue(queues: dict) -> bool:
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = QUEUE_FILE.with_name(QUEUE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(queues, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, QUEUE_FILE)
        return True
    except Exception as exc:
        log.error("保存分批队列失败: %s", exc)
        return False


def queue_put(pwd_id: str, share: str, date_str: str, passcode: str,
              items: list[dict]) -> int:
    """把暂不下载的文件记进队列；返回队列里这个分享的待下载总数。"""
    with QUEUE_LOCK:
        queues = load_queue()
        entry = queues.setdefault(pwd_id, {
            "share": share, "date": date_str, "passcode": passcode,
            "added_at": int(time.time()), "items": [],
        })
        entry["share"] = share
        entry["date"] = date_str
        entry["passcode"] = passcode
        known = {it.get("fid") for it in entry["items"]}
        for it in items:
            if it.get("fid") not in known:
                entry["items"].append(dict(it, tries=0))
                known.add(it.get("fid"))
        save_queue(queues)
        return len(entry["items"])


def queue_drop(pwd_id: str, fids: set[str]) -> None:
    """把已经建好任务 / 确认下不了的文件从队列里去掉。"""
    if not fids:
        return
    with QUEUE_LOCK:
        queues = load_queue()
        entry = queues.get(pwd_id)
        if not entry:
            return
        entry["items"] = [it for it in entry["items"] if it.get("fid") not in fids]
        if not entry["items"]:
            queues.pop(pwd_id, None)
        save_queue(queues)


def queue_pending() -> int:
    return sum(len(v.get("items") or []) for v in load_queue().values())


def plan_wave(items: list[dict], capacity: int, hard_limit: int):
    """从队列里挑一波：累计大小不超过 capacity。

    第一个文件就超过 capacity 但没超过 hard_limit 时，允许它单独成波，
    否则大文件会永远卡在队列里。
    返回 (本波, 剩余)。
    """
    wave, rest, used = [], [], 0
    for it in items:
        size = int(it.get("size") or 0)
        if len(wave) < MAX_WAVE_FILES and used + size <= capacity:
            wave.append(it)
            used += size
        elif not wave and size <= hard_limit:
            wave.append(it)
            used += size
        else:
            rest.append(it)
    return wave, rest


# 夸克访问策略：
#   auto（默认）= 账号优先：分享直链能用就直接下（快、不占网盘）；
#                 探到 412（夸克现在对分享直链一律拒绝）就自动改用「转存后下载」
#   guest        = 只用公开分享会话（适合小文件）
#   account      = 只用账号 Cookie
#   bridge       = 强制走「转存到自己网盘再下载」
QUARK_MODE = os.environ.get("QUARK_MODE", "auto").lower()
QUARK_GUEST_FIRST = QUARK_MODE == "guest"

# 中转目录：转存到这里再从自己网盘下，下完就删
QUARK_BRIDGE_DIR = os.environ.get("QUARK_BRIDGE_DIR", "tg_transfer_bridge")

# 空间策略：会员过期后夸克只剩 ~10GB 免费额度，而账号里可能已经塞了上 TB，
# 这时「转存」必然失败，系统必须提前判断并降级，而不是一头撞上去。
QUARK_MIN_FREE_MB = _env_num("QUARK_MIN_FREE_MB", 300)          # 低于这个空间就不转存
QUARK_FREE_RATIO = 0.6                                          # 一波最多占剩余空间的 60%
GUEST_MAX_MB = _env_num("GUEST_MAX_MB", 45)                     # 游客公开分享直链实测上限 ~50MB
GUEST_MAX_BYTES = GUEST_MAX_MB * 1024 * 1024

# 专用「转存账号」：另一个夸克号，只用来做中转，不占主号空间。
# Cookie 放独立文件（600 权限），改了不用重启：按 mtime 自动重载。
BRIDGE_COOKIE_FILE = os.environ.get(
    "QUARK_BRIDGE_COOKIE_FILE", "/opt/cloud-disk-transfer/quark_bridge.cookie")
# 非会员账号自己网盘直链也有单文件大小限制（实测 23018），超过就走主号
BRIDGE_ACC_MAX_MB = _env_num("BRIDGE_ACC_MAX_MB", 45)
BRIDGE_ACC_MAX_BYTES = BRIDGE_ACC_MAX_MB * 1024 * 1024

_bridge_cookie_cache = {"mtime": 0.0, "cookie": ""}


def _bridge_cookie() -> str:
    """读专用转存账号的 Cookie；文件不存在就返回空（退回主号）。"""
    try:
        st = os.stat(BRIDGE_COOKIE_FILE)
    except OSError:
        return ""
    c = _bridge_cookie_cache
    if st.st_mtime != c["mtime"]:
        try:
            with open(BRIDGE_COOKIE_FILE, encoding="utf-8") as fh:
                c["cookie"] = fh.read().strip()
            c["mtime"] = st.st_mtime
            log.info("已载入专用转存账号（cookie %d 字节）", len(c["cookie"]))
        except Exception as exc:
            log.error("读取转存账号 Cookie 失败: %s", exc)
            return ""
    return c["cookie"]


def _client_for(kind: str = "main") -> "QuarkShare":
    """main=主号（能下大文件），bridge=专用转存号（省主号空间）。"""
    if kind == "bridge":
        ck = _bridge_cookie()
        if ck:
            return QuarkShare(cookie=ck)
    return QuarkShare()


def bridge_account_ready() -> bool:
    return bool(_bridge_cookie())

# 分享直链是否可用：None=还没测过，True/False=10 分钟内的探测结论
_share_direct_known = None
_share_direct_until = 0.0
ACCOUNT_BLOCK_SECONDS = 600


def account_blocked_for() -> int:
    """分享直链被拒还剩多少秒（0 = 可用或未知）。"""
    if _share_direct_known is False:
        return max(0, int(_share_direct_until - time.time()))
    return 0


def _set_share_direct(ok: bool) -> None:
    global _share_direct_known, _share_direct_until
    _share_direct_known = ok
    _share_direct_until = time.time() + ACCOUNT_BLOCK_SECONDS


def _probe_url(url: str, headers: dict) -> bool:
    """用 Range: 0-0 试一下直链真的能下（412/403 会被这里发现）。"""
    try:
        r = rq.get(url, headers={**headers, "Range": "bytes=0-0"}, timeout=10)
        return r.status_code in (200, 206)
    except Exception:
        return False


# 分享解析结果缓存：大分享（几千个文件）每波都重扫一遍太浪费，
# 10 分钟内复用同一次 stoken / 文件列表 / 会话。
_share_cache: dict[str, tuple] = {}
SHARE_CACHE_TTL = 600


def _drop_share_cache(pwd_id: str) -> None:
    _share_cache.pop(pwd_id, None)


async def _open_share(pwd_id: str, passcode: str = "", refresh: bool = False):
    """打开一个分享，返回 (客户端, stoken, 文件列表)。

    顺序：账号优先（可转存、适合大文件）→ 失败再退回公开分享会话。
    结果缓存 10 分钟，避免大分享每波都重扫。
    """
    if not refresh:
        hit = _share_cache.get(pwd_id)
        if hit and time.time() - hit[0] < SHARE_CACHE_TTL:
            return hit[1], hit[2], hit[3]

    if QUARK_MODE == "guest":
        order = [QuarkGuest]
    else:
        order = [QuarkShare, QuarkGuest]

    errors = []
    for factory in order:
        try:
            q = factory()
            st = await asyncio.to_thread(q.get_stoken, pwd_id, passcode)
            files = await asyncio.to_thread(
                q.walk_share, pwd_id, st, "0", "", 0, MAX_SHARE_FILES + 1)
            if isinstance(q, QuarkGuest):
                log.info("使用公开分享模式解析分享 %s", pwd_id)
            # 缓存最多留 3 个分享：大分享每个可能上万条，不能无限攒
            if len(_share_cache) >= 3:
                oldest = min(_share_cache, key=lambda k: _share_cache[k][0])
                _share_cache.pop(oldest, None)
            _share_cache[pwd_id] = (time.time(), q, st, files)
            return q, st, files
        except Exception as exc:
            errors.append(f"{factory.__name__}: {exc}")
            log.warning("打开分享失败（%s）：%s", factory.__name__, exc)
            continue
    raise QuarkError("；".join(errors) or "打开分享失败")


async def _check_account_direct(q, files, pwd_id: str, stoken: str) -> bool:
    """验证「分享直链」还能不能直接用；不能就记下冷却，改用转存桥接。

    2026-11-04 起夸克对分享直链一律返回 412（与文件大小、账号状态无关），
    而「自己网盘的直链」仍然正常，所以兜底方案是：转存到自己网盘再下载。
    """
    if not files:
        return True
    f = max(files, key=lambda x: x.get("size") or 0)   # 拿最大的那个试，最能暴露问题
    try:
        url = await asyncio.to_thread(
            q.get_direct_url, f["fid"], f.get("share_fid_token", ""), pwd_id, stoken)
        hdrs = await asyncio.to_thread(q.download_headers)
    except Exception as exc:
        log.warning("分享直链取不到，改用转存桥接: %s", exc)
        _set_share_direct(False)
        return False
    ok = await asyncio.to_thread(_probe_url, url, hdrs)
    if not ok:
        log.warning("分享直链被拒（412），%d 秒内改用「转存后下载」",
                    ACCOUNT_BLOCK_SECONDS)
        _set_share_direct(False)
    else:
        _set_share_direct(True)
    return ok


async def _share_direct_for(q, files, pwd_id: str, stoken: str) -> bool:
    """这一波该不该直接用分享直链。

    公开会话永远是分享直链（它没有账号 Cookie，也转存不了）；
    账号模式先看 10 分钟内的探测结论，没测过就现场测一次。
    """
    if isinstance(q, QuarkGuest) or QUARK_MODE == "guest":
        return True
    if QUARK_MODE == "bridge":
        return False
    if _share_direct_known is not None and time.time() < _share_direct_until:
        return bool(_share_direct_known)
    if not files:
        return True
    return await _check_account_direct(q, files, pwd_id, stoken)


async def _probe_direct_once(q, item: dict, pwd_id: str, stoken: str,
                             passcode: str = "") -> bool:
    """用队列里的一个文件探一次「分享直链能不能下」，结果缓存 10 分钟。

    token 过期（41020）不算「被拒」：刷新 token 再试一次，避免误判成 412。
    刷新后仍然 41020 说明这个文件本身有问题（作者设置/已损坏），
    把它从队列里摘掉，否则它会一直堵在队头让整条流水线卡死。
    """
    for attempt in (1, 2):
        try:
            url = await asyncio.to_thread(
                q.get_direct_url, item["fid"], item.get("fid_token", ""),
                pwd_id, stoken)
            hdrs = await asyncio.to_thread(q.download_headers)
            break
        except Exception as exc:
            if attempt == 1 and _token_error(exc) and not _refresh_recent(pwd_id):
                log.info("探测用的 token 过期了，刷新后重试")
                await _refresh_tokens(pwd_id, passcode)
                fresh = {x.get("fid"): x for x in
                         (load_queue().get(pwd_id) or {}).get("items", [])}
                tok = (fresh.get(item.get("fid")) or {}).get("fid_token", "")
                if not tok:
                    _set_share_direct(False)
                    return False
                item = dict(item, fid_token=tok)
                continue
            if _token_error(exc):
                # 刷新过还是不行：这个文件取不到直链，摘掉它，别堵队头
                _cool_down_fid(pwd_id, item.get("fid"), "取直链一直 41020")
            log.warning("探测分享直链失败（%s），改用转存桥接: %s",
                        item.get("name"), exc)
            _set_share_direct(False)
            return False
    ok = await asyncio.to_thread(_probe_url, url, hdrs)
    if ok:
        if _share_direct_known is not True:
            log.info("分享直链可用，直接下载")
        _set_share_direct(True)
    else:
        log.warning("分享直链被拒（412），改用「转存后下载」")
        _set_share_direct(False)
    return ok


# stoken 缓存（不缓存文件列表）：取一次能用很久，避免每波都重扫分享
_stoken_cache: dict[str, tuple] = {}
STOKEN_TTL = 600

# 每个分享上次「全量重扫」的时间：刚扫过就别再扫，否则一个坏文件会让每次重试
# 都触发 4 分钟的全量扫描，进度被拖死。
_last_refresh: dict[str, float] = {}
REFRESH_MIN_INTERVAL = 300


def _refresh_recent(pwd_id: str) -> bool:
    return time.time() - _last_refresh.get(pwd_id, 0) < REFRESH_MIN_INTERVAL


def _retarget_queue_fids(pwd_id: str, mapping: dict) -> None:
    """作者换了文件后 fid 会变：把队列里的旧 fid 改成新的，避免一直 41020。"""
    if not mapping:
        return
    with QUEUE_LOCK:
        queues = load_queue()
        ent = queues.get(pwd_id)
        if not ent:
            return
        n = 0
        for it in ent.get("items") or []:
            new = mapping.get(it.get("fid"))
            if new:
                it["fid"] = new
                it["fid_token"] = ""
                n += 1
        if n:
            save_queue(queues)
            log.info("分享里的文件被替换过，已更新 %d 个文件的 fid", n)


_quark_quota_cache = {
    "main": {"ts": 0.0, "total": 0, "used": 0},
    "bridge": {"ts": 0.0, "total": 0, "used": 0},
}


async def _quark_quota(kind: str = "main") -> tuple[int, int]:
    """某个账号的 (总容量, 已用容量)，缓存 10 分钟。拿不到就返回上次的值。"""
    c = _quark_quota_cache.setdefault(kind, {"ts": 0.0, "total": 0, "used": 0})
    if c["ts"] and time.time() - c["ts"] < 600:
        return c["total"], c["used"]
    try:
        q = _client_for(kind)
        total, used = await asyncio.to_thread(q.quota)
        if total:
            c.update(ts=time.time(), total=total, used=used)
    except Exception as exc:
        log.warning("查询夸克空间失败（%s，按上次结果处理）: %s", kind, exc)
    return c["total"], c["used"]


async def _quark_free(kind: str = "main") -> int:
    total, used = await _quark_quota(kind)
    return max(0, total - used)


_member_cache = {"main": {"ts": 0.0, "type": ""}, "bridge": {"ts": 0.0, "type": ""}}


_learned_cap = {"main": 0, "bridge": 0}   # 实测撞到 23018 学到的上限（0=未知）


def _note_size_limit(kind: str, size: int) -> None:
    """撞到 23018：说明这个账号下不了这么大的文件，记下来，后面别再做无用转存。"""
    if size <= 0:
        return
    cur = _learned_cap.get(kind) or 0
    new = size - 1
    if not cur or new < cur:
        _learned_cap[kind] = new
        log.warning("记下账号 %s 的下载上限约 %d MB（该文件下不了）",
                    kind, new // 1048576)


async def _account_cap(kind: str = "main") -> int:
    """该账号「自己网盘直链」能下多大的文件；0 表示不限。

    实测（2026-11-04）：非会员账号下 >50MB 的文件，夸克直接回 23018；
    而且主号虽然 member_type 显示 VIP，实际未付费，大文件同样会被拒
    （20GB 的 gguf 就是撞了这个）。所以除了看会员类型，还要用实测值兜底。
    """
    c = _member_cache.setdefault(kind, {"ts": 0.0, "type": ""})
    if not c["ts"] or time.time() - c["ts"] > 600:
        try:
            info = await asyncio.to_thread(_client_for(kind).member_info)
            c.update(ts=time.time(), type=str(info.get("member_type") or ""))
        except Exception as exc:
            log.warning("查询账号类型失败（%s）: %s", kind, exc)
    learned = _learned_cap.get(kind) or 0
    if learned:
        log.info("账号 %s 曾撞到 %d MB 的下载上限（仅作参考）", kind, learned // 1048576)
    if "VIP" in c["type"].upper():
        # 主号不按「学到的上限」过滤：同一个账号对不同文件的表现并不一致
        # （实测 86MB 能下、某些 51MB 反而报 23018），按实际结果冷却更稳。
        return 0
    return BRIDGE_ACC_MAX_BYTES


_guest_cache: dict = {}


async def _guest_link(pwd_id: str, fid: str, token: str) -> tuple:
    """公开分享直链 + 对应请求头（不占账号空间，但只支持小文件）。"""
    hit = _guest_cache.get(pwd_id)
    if not (hit and time.time() - hit[0] < 1800):
        g = QuarkGuest()
        gst = await asyncio.to_thread(g.get_stoken, pwd_id, "")
        hit = (time.time(), g, gst)
        _guest_cache[pwd_id] = hit
    _, g, gst = hit
    url = await asyncio.to_thread(g.get_direct_url, fid, token, pwd_id, gst)
    hdrs = await asyncio.to_thread(g.download_headers)
    return url, hdrs


async def _share_stoken(pwd_id: str, passcode: str = ""):
    hit = _stoken_cache.get(pwd_id)
    if hit and time.time() - hit[0] < STOKEN_TTL:
        return hit[1], hit[2]
    q = QuarkGuest() if QUARK_MODE == "guest" else QuarkShare()
    st = await asyncio.to_thread(q.get_stoken, pwd_id, passcode)
    if len(_stoken_cache) >= 5:
        oldest = min(_stoken_cache, key=lambda k: _stoken_cache[k][0])
        _stoken_cache.pop(oldest, None)
    _stoken_cache[pwd_id] = (time.time(), q, st)
    return q, st


def _drop_stoken(pwd_id: str) -> None:
    _stoken_cache.pop(pwd_id, None)


COOLDOWN_SECONDS = 6 * 3600


def _cool_down_fid(pwd_id: str, fid: str, reason: str = "",
                   seconds: int = 0) -> bool:
    """把这个文件标记为「暂时下不了」，冷却一段时间后再试。

    绝对不删队列里的文件：删了就是用户的数据永久丢失（之前踩过这个坑）。
    """
    if not fid:
        return False
    until = int(time.time() + (seconds or COOLDOWN_SECONDS))
    hit = False
    with QUEUE_LOCK:
        queues = load_queue()
        ent = queues.get(pwd_id)
        if not ent:
            return False
        for it in ent.get("items") or []:
            if it.get("fid") == fid:
                it["skip_until"] = until
                it["last_error"] = reason
                hit = True
        if hit:
            save_queue(queues)
    if hit:
        log.warning("文件暂时下不了，冷却 %d 小时后再试（%s）：%s",
                    int((until - time.time()) // 3600) or 1, reason, fid[:12])
    return hit


def _drop_queue_fid(pwd_id: str, fid: str, reason: str = "") -> bool:
    """（保留兼容）等同冷却，不再真删。"""
    return _cool_down_fid(pwd_id, fid, reason)


async def _refresh_tokens(pwd_id: str, passcode: str = "") -> tuple:
    """重扫分享，把新的 fid_token 写回队列；顺便清掉「分享里已经没有」的文件。

    关键：分享里的文件可能被作者删掉/替换，这类文件的 token 永远刷新不到，
    留在队列里会让每一波都 41020 失败、整条流水线卡死，所以必须移除。
    """
    _drop_stoken(pwd_id)
    try:
        q, st, files = await _open_share(pwd_id, passcode, refresh=True)
    except Exception as exc:
        log.error("重扫分享失败 %s: %s", pwd_id, exc)
        return 0, 0
    tok = {f["fid"]: f.get("share_fid_token", "") for f in files}
    with QUEUE_LOCK:
        queues = load_queue()
        ent = queues.get(pwd_id)
        if not ent:
            return 0, 0
        n = gone = 0
        kept = []
        for it in ent.get("items") or []:
            fid = it.get("fid")
            t = tok.get(fid)
            if not t:
                gone += 1
                log.warning("分享里已经没有这个文件，移出队列: %s", it.get("name"))
                continue
            if t != it.get("fid_token"):
                it["fid_token"] = t
                n += 1
            kept.append(it)
        ent["items"] = kept
        if not kept:
            queues.pop(pwd_id, None)
        save_queue(queues)
    _last_refresh[pwd_id] = time.time()
    log.info("重扫分享 %s：刷新 %d 个 token，移除 %d 个失效文件", pwd_id, n, gone)
    return n, gone


async def _bridge_dir(q) -> str:
    """自己网盘里的转存中转目录 fid。"""
    return await asyncio.to_thread(q.ensure_dir, QUARK_BRIDGE_DIR)


async def _bridge_save(q, pwd_id: str, stoken: str, fids: list[str],
                       tokens: list[str], names: dict | None = None) -> dict:
    """批量转存到中转目录，返回 {原 fid: 自己网盘 fid}。

    转存前先清空中转目录：同名文件会让夸克去重、返回的 fid 数量对不上，
    进而导致「原 fid → 新 fid」的映射错位。
    """
    if not fids:
        return {}
    bridge = await _bridge_dir(q)
    # 只清「没有任务在用的」旧副本：清空整个目录会把上一波还在下载的副本删掉，
    # 导致后面取直链时报 file not found (21001)。
    try:
        alive = {v.get("own_fid") for v in load_gopeed_map().values()
                 if v.get("own_fid")}
        old = await asyncio.to_thread(q.list_dir, bridge, 200)
        dead = [x["fid"] for x in old
                if not x.get("dir") and x["fid"] not in alive]
        if dead:
            await asyncio.to_thread(q.delete, dead)
            log.info("清理中转目录 %d 个无主副本", len(dead))
    except Exception as exc:
        log.warning("清理中转目录失败（继续转存）: %s", exc)
    task_id = await asyncio.to_thread(
        q.save, pwd_id, stoken, fids, tokens, bridge)
    new_fids = await asyncio.to_thread(q.wait_task_fids, task_id, 300)
    if len(new_fids) == len(fids):
        return dict(zip(fids, new_fids))
    if not new_fids:
        raise QuarkError(f"转存结果为空（{len(fids)} 个都没成功）")
    # 部分成功时按顺序 zip 会错位：改成「列目录 + 按文件名/大小匹配」
    log.warning("转存结果不全（%d/%d），按文件名重新对齐", len(new_fids), len(fids))
    try:
        items = await asyncio.to_thread(q.list_dir, bridge, 500)
    except Exception as exc:
        log.warning("列中转目录失败，退回顺序映射: %s", exc)
        return dict(zip(fids, new_fids))
    got = {}
    for x in items:
        if not x.get("dir"):
            got[(x.get("file_name"), int(x.get("size") or 0))] = x["fid"]
    out = {}
    for fid in fids:
        name = names.get(fid) if names else None
        if not name:
            continue
        hit = got.get(name) or next(
            (f for (nm, _sz), f in got.items() if nm == name[0]), None)
        if hit:
            out[fid] = hit
    if not out:
        log.warning("按名字没匹配上，退回顺序映射")
        return dict(zip(fids, new_fids))
    return out


def _token_error(exc) -> bool:
    text = str(exc)
    return "41020" in text or "token" in text.lower()


async def _tokens_for_dirs(pwd_id: str, st: str, pdirs: list,
                           q=None) -> dict:
    """只重新列出这些目录，拿最新 fid→token（比全量扫描快几十倍）。

    夸克的 share_fid_token 存活时间不长，1000+ 文件的分享如果每次全量重扫
    要 4 分钟；而每一波只涉及几个目录，按目录重列只要几个请求。
    """
    q = q or _client_for("main")
    out = {}
    for pdir in {p for p in pdirs if p}:
        try:
            items = await asyncio.to_thread(q._fetch_page, pwd_id, st, pdir, 1)
        except Exception as exc:
            log.warning("重列目录 %s 失败: %s", str(pdir)[:8], exc)
            continue
        for it in items:
            if it.get("fid") and not it.get("dir"):
                out[it["fid"]] = it.get("share_fid_token", "")
    return out


async def _bridge_save_fresh(pwd_id: str, passcode: str, wave: list,
                             q, st: str, tokens: dict,
                             kind: str = "main") -> dict:
    """用指定账号转存一波文件。

    fid / fid_token 的唯一可靠来源是「刚扫过一遍的分享列表」：
    作者换过文件后，队列里的旧 fid 会一直 41020（夸克报错里给的是新 fid）。
    所以这里按「文件名 + 大小」去最新列表里找对应文件，找不到才用 fid 兜底。
    """
    client = _client_for(kind)
    hit = _share_cache.get(pwd_id)
    if hit and time.time() - hit[0] < SHARE_CACHE_TTL:
        files = hit[3]
    else:
        q, st, files = await _open_share(pwd_id, passcode, refresh=True)

    # 队列里存了 each 文件所在目录：按目录重列拿最新 token（快且准）
    dirs = [it.get("pdir") for it in wave if it.get("pdir")]
    fresh_tok = {}
    if dirs:
        fresh_tok = await _tokens_for_dirs(pwd_id, st, dirs, q)
    by_key = {(f["name"], int(f.get("size") or 0)): f for f in files}
    by_fid = {f["fid"]: f for f in files}
    fids, toks, retarget, names = [], [], {}, {}
    for it in wave:
        f = (by_key.get((it.get("name"), int(it.get("size") or 0)))
             or by_fid.get(it.get("fid")))
        if f and fresh_tok.get(f["fid"]):
            f = dict(f, share_fid_token=fresh_tok[f["fid"]])
        if not f or not f.get("share_fid_token"):
            _cool_down_fid(pwd_id, it.get("fid"), "分享里已找不到这个文件")
            continue
        if f["fid"] != it.get("fid"):
            retarget[it.get("fid")] = f["fid"]
        fids.append(f["fid"])
        toks.append(f["share_fid_token"])
        names[f["fid"]] = (f["name"], int(f.get("size") or 0))
    if retarget:
        _retarget_queue_fids(pwd_id, retarget)
    if not fids:
        return {}
    try:
        return await _bridge_save(client, pwd_id, st, fids, toks, names)
    except Exception as exc:
        if not _token_error(exc):
            raise
        # token 不可信时用「最新一次扫描」重来；只有缓存过期才真的去重扫（4 分钟）
        hit2 = _share_cache.get(pwd_id)
        if hit2 and time.time() - hit2[0] < SHARE_CACHE_TTL:
            files = hit2[3]
        else:
            log.warning("转存 token 校验失败，重扫分享后重试")
            q, st, files = await _open_share(pwd_id, passcode, refresh=True)
        fresh = {f["fid"]: f.get("share_fid_token", "") for f in files}
        fids = [x for x in fids if fresh.get(x)]
        if not fids:
            raise
        toks = [fresh[x] for x in fids]
        try:
            return await _bridge_save(client, pwd_id, st, fids, toks)
        except Exception as exc2:
            if not _token_error(exc2):
                raise
            # 用最新 token 整批仍失败：逐个转存，把真坏的文件隔离出来
            log.warning("整批转存仍失败，改为逐个转存以隔离问题文件")
            out = {}
            for fid in fids:
                try:
                    out.update(await _bridge_save(
                        client, pwd_id, st, [fid], [fresh[fid]]))
                except Exception as exc3:
                    if _token_error(exc3):
                        _cool_down_fid(pwd_id, fid, "用最新 token 仍转存失败")
                        continue
                    raise
            return out


async def _bridge_group(pwd_id: str, passcode: str, items: list, q, st: str,
                        tokens: dict, prefer: str) -> tuple:
    """把一组文件转存到 prefer 账号；空间/权限不行就换另一个账号。

    返回 ({fid: (中转 fid, 账号)}, 实际转存的那批文件)。
    """
    if not items:
        return {}, []
    order = [prefer] + [k for k in ("main", "bridge") if k != prefer]
    # 转存前换一个新 stoken：夸克的 stoken 也会过期，用旧的会让整批 41020
    _drop_stoken(pwd_id)
    try:
        q, st = await _share_stoken(pwd_id, passcode)
    except Exception as exc:
        log.warning("取 stoken 失败（转存前）: %s", exc)
    for kind in order:
        if kind == "bridge" and not bridge_account_ready():
            continue
        cap = await _account_cap(kind)          # 该账号能下多大（0=不限）
        usable = [it for it in items
                  if not cap or int(it.get("size") or 0) <= cap]
        if not usable:
            continue
        free = await _quark_free(kind)
        if free < QUARK_MIN_FREE_MB * 1024 * 1024:
            log.info("账号 %s 空间不足（剩 %.0f MB），跳过", kind, free / 1048576)
            continue
        budget = int(free * QUARK_FREE_RATIO)
        fit, acc = [], 0
        for it in usable:
            sz = int(it.get("size") or 0)
            if acc + sz <= budget:
                fit.append(it)
                acc += sz
        if not fit:
            continue
        try:
            mapping = await _bridge_save_fresh(
                pwd_id, passcode, fit, q, st, tokens, kind)
        except Exception as exc:
            log.warning("转存失败（%s 账号）: %s", kind, exc)
            continue
        return {fid: (own, kind) for fid, own in mapping.items()}, fit
    return {}, []


def _disk_budget() -> tuple[int, int]:
    """返回 (本次可用预算, 硬上限)：预算 = 可用空间-安全线 的 60%，硬上限 = 全部余量。"""
    try:
        usage = shutil.disk_usage(str(DOWNLOAD_DIR))
    except OSError:        usage = shutil.disk_usage("/")
    hard = max(0, usage.free - MIN_FREE_BYTES)
    return int(hard * WAVE_FILL_RATIO), hard


_ADMIT_LOCK = asyncio.Lock()


async def admit_wave(force: bool = False) -> dict:
    """从队列里放一波文件进下载器；watcher 每轮也会调它来续上。

    force=True 用于用户刚提交时（此时不看在途任务数量，只看磁盘余量）。
    整个函数串行执行：两个分享同时提交时，避免两次调用读到同一份队列、
    把同一个文件放行两次。
    """
    async with _ADMIT_LOCK:
        return await _admit_wave_locked(force)


async def _admit_wave_locked(force: bool) -> dict:
    stats = {"admitted": 0, "left": 0, "failed": 0}
    queues = load_queue()
    if not queues:
        return stats
    if not force and len(load_gopeed_map()) > FEED_AT_MAP_SIZE:
        return stats

    capacity, hard = _disk_budget()
    if hard <= 0:
        return stats

    for pwd_id, entry in list(queues.items()):
        items = list(entry.get("items") or [])
        # 冷却中的文件这波先跳过（它们会保留在队列里，到点自动再试）
        _now = time.time()
        items = [x for x in items if float(x.get("skip_until") or 0) <= _now]
        if not items:
            continue
        wave, rest = plan_wave(items, capacity, hard)
        if not wave:
            continue

        # 不再每波重扫整个分享（1000+ 文件要几分钟）：直接用队列里存的文件 token，
        # 只有真过期（夸克回 41020）时才重扫一次并刷新队列。
        try:
            q, st = await _share_stoken(pwd_id, entry.get("passcode", ""))
        except Exception as exc:
            log.error("分批续传：取 stoken 失败 %s: %s", pwd_id, exc)
            if is_auth_error(str(exc), "quark"):
                await send_auth_alert("quark", str(exc))
            continue

        tokens = {it.get("fid"): {"share_fid_token": it.get("fid_token", "")}
                  for it in items}

        # 选下载模式：分享直链能用就直接下；被夸克拒（412）就整波转存到自己网盘再下
        if isinstance(q, QuarkGuest) or QUARK_MODE == "guest":
            use_bridge = False
        elif QUARK_MODE == "bridge":
            use_bridge = True
        else:
            if _share_direct_known is None or time.time() >= _share_direct_until:
                probe = wave[0] if wave else items[0]
                await _probe_direct_once(q, probe, pwd_id, st,
                                          entry.get("passcode", ""))
            use_bridge = _share_direct_known is False

        # 转存要占网盘空间：小文件优先用「专用转存账号」（不占主号空间），
        # 大文件只能走主号（非会员账号下不了大文件，夸克回 23018）。
        guest_ok = False
        bmap: dict = {}          # fid -> (中转 fid, 账号 kind)
        if use_bridge:
            # 小文件优先交给专用转存号（它有 10G 空间，能下 ≤45MB）；大文件只能主号
            small = [it for it in wave
                     if int(it.get("size") or 0) <= BRIDGE_ACC_MAX_BYTES]
            large = [it for it in wave
                     if int(it.get("size") or 0) > BRIDGE_ACC_MAX_BYTES]
            got_all = []
            if small:
                m1, fit1 = await _bridge_group(
                    pwd_id, entry.get("passcode", ""), small, q, st, tokens, "bridge")
                bmap.update(m1)
                got_all += fit1
            if large:
                m2, fit2 = await _bridge_group(
                    pwd_id, entry.get("passcode", ""), large, q, st, tokens, "main")
                bmap.update(m2)
                got_all += fit2
            if bmap:
                kinds = {}
                for _fid, (_own, kind) in bmap.items():
                    kinds[kind] = kinds.get(kind, 0) + 1
                log.info("分批续传：已转存 %d 个（%s），改从自己网盘下载",
                         len(bmap), "，".join(f"{k}:{v}" for k, v in kinds.items()))
            if not bmap:
                # 两个账号都没转成（空间/权限），这一波小文件改走公开通道
                guest_ok = True
                free_main = await _quark_free("main")
                log.warning("转存不可用（主号剩 %.0f MB，转存号%s），小文件走公开通道",
                            free_main / 1048576,
                            "已配置" if bridge_account_ready() else "未配置")

        done_fids, dead_fids, tries_updates = set(), set(), {}
        refreshed = False
        space_blocked = 0
        space_biggest = 0
        for it in wave:
            fid = it.get("fid")
            fresh = tokens.get(fid)
            if not fresh:
                # 队列里没有这个文件的信息（异常情况），跳过
                dead_fids.add(fid)
                log.warning("分批续传：队列里缺 token，丢弃 %s", it.get("name"))
                continue
            try:
                # 空间不够又转不了存时，超过上限的大文件先留队列，避免反复请求失败
                size = int(it.get("size") or 0)
                hit_bridge = bmap.get(fid) if bmap else None
                if not hit_bridge and (guest_ok or use_bridge) and size > GUEST_MAX_BYTES:
                    # 没转存上、又超过上限：先留队列，避免反复请求失败
                    space_blocked += 1
                    space_biggest = max(space_biggest, size)
                    continue
                own_fid = ""
                own_acct = ""
                hdrs = None
                if hit_bridge:
                    own_fid, own_acct = hit_bridge
                    bq = _client_for(own_acct)
                    url = await asyncio.to_thread(bq.own_direct_url, own_fid)
                    hdrs = await asyncio.to_thread(bq.download_headers)
                elif guest_ok:
                    url, hdrs = await _guest_link(
                        pwd_id, fid, fresh.get("share_fid_token", ""))
                else:
                    url = await asyncio.to_thread(
                        q.get_direct_url, fid, fresh.get("share_fid_token", ""), pwd_id, st)
                # 公开会话下 __sdid 是在"取直链"这一步才写入会话的，
                # 所以请求头必须在拿到 URL 之后再取一次
                if hdrs is None:
                    hdrs = await asyncio.to_thread(q.download_headers)
                saved_name = gopeed_saved_name(pwd_id, it["rel"], it["name"])
                tid = await asyncio.to_thread(
                    gopeed.add_task, url, saved_name, GOPEED_DLDIR, hdrs,
                    gopeed_connections(int(it.get("size") or 0)))
                map_add(tid, {
                    "date": entry.get("date"), "share": entry.get("share"),
                    "orig_name": it["name"], "saved_name": saved_name,
                    "rel_path": it["rel"], "size": it.get("size") or 0,
                    "added_at": int(time.time()), "pwd_id": pwd_id,
                    "passcode": entry.get("passcode", ""), "fid": fid,
                    "fid_token": fresh.get("share_fid_token", ""), "retry": 0,
                    "own_fid": own_fid,          # 自己网盘的临时副本，传完就删
                    "own_acct": own_acct,        # 副本存在哪个账号（main/bridge）
                })
                done_fids.add(fid)
                stats["admitted"] += 1
                # 控制节奏：一口气建几十个任务容易触发夸克侧限流（HTTP 412）
                await asyncio.sleep(WAVE_PACE)
            except Exception as exc:
                if _token_error(exc) and not refreshed:
                    # token 过期不是文件的错：重扫一次刷新队列，不计重试次数
                    refreshed = True
                    log.warning("分批续传：token 过期，重扫分享后重试")
                    await _refresh_tokens(pwd_id, entry.get("passcode", ""))
                    continue
                if "23018" in str(exc) or "size limit" in str(exc).lower():
                    # 文件超过该账号的下载上限：记下上限、冷却这个文件，别反复白转存
                    _note_size_limit(own_acct or "main", size)
                    _cool_down_fid(pwd_id, fid, "超过账号下载上限",
                                   seconds=6 * 3600)
                    stats["failed"] += 1
                    continue
                n = int(it.get("tries") or 0) + 1
                tries_updates[fid] = n
                log.error("分批续传：建任务失败 %s (%d/3): %s",
                          it.get("name"), n, exc)
                if n >= 3:
                    dead_fids.add(fid)
                else:
                    stats["failed"] += 1
                if is_auth_error(str(exc), "quark"):
                    await send_auth_alert("quark", str(exc))

        with QUEUE_LOCK:
            cur = load_queue()
            ent = cur.get(pwd_id)
            if ent:
                kept = []
                for x in ent.get("items", []):
                    xfid = x.get("fid")
                    if xfid in done_fids or xfid in dead_fids:
                        continue
                    if xfid in tries_updates:
                        x["tries"] = tries_updates[xfid]
                    kept.append(x)
                ent["items"] = kept
                if not kept:
                    cur.pop(pwd_id, None)
                save_queue(cur)
                stats["left"] = len(kept)
                # 有失败说明 token 可能过期了，清掉缓存，下一波重新扫
                if tries_updates:
                    _drop_share_cache(pwd_id)

            # 夸克空间不够、大文件被搁下：每 6 小时提醒一次就够了
            if space_blocked:
                last = _space_warned.get(pwd_id, 0)
                if time.time() - last > 6 * 3600:
                    _space_warned[pwd_id] = time.time()
                    free_q = await _quark_free()
                    await _notify(
                        f"⚠️ 夸克空间不够，部分文件先搁着\n{SEP}\n\n"
                        f"这个分享里有 {space_blocked} 个文件偏大"
                        f"（最大 {human(space_biggest)}），"
                        f"夸克那边只剩 {human(free_q)} 可用空间\n\n"
                        f"已经改用的办法：≤{GUEST_MAX_MB}MB 的文件走公开通道，不占空间\n"
                        f"大文件要么清一清夸克网盘、要么等会员续上，之后我会自动接着下")

    return stats


_space_warned: dict = {}


def _task_paths(info: dict):
    """返回 (Gopeed 临时文件, 归档目标文件)；路径非法时抛 ValueError。"""
    saved_name = info.get("saved_name") or ""
    date_str = safe_name(info.get("date") or "", fallback="")
    share = safe_name(info.get("share") or "", fallback="unnamed")
    rel_path = safe_rel_path(
        info.get("rel_path") or info.get("orig_name") or saved_name)
    if not saved_name or not date_str:
        raise ValueError("缺少 saved_name/date")
    base = Path(GOPEED_DLDIR).resolve()
    dest_dir = (base / date_str / share / rel_path.parent).resolve()
    dest_dir.relative_to(base)          # 阻止 ../ 逃逸
    return Path(GOPEED_DLDIR) / saved_name, dest_dir / rel_path.name


def _resolve_actual_src(info: dict):
    """找出真正落盘的文件。

    Gopeed 会按服务端返回的 `Content-Disposition: filename` 命名文件
    （PikPak 的 CDN 就带这个头），从而和我们自己的 saved_name 不一致。
    这里按「原名」或「大小唯一匹配」把真实文件找回来，避免误判成没下成。
    """
    try:
        src, dest = _task_paths(info)
    except Exception:
        return None, None
    if src.exists():
        return src, dest
    base = Path(GOPEED_DLDIR)
    orig = (info.get("orig_name") or "").strip()
    expected = int(info.get("size") or 0)
    try:
        cands = [p for p in base.iterdir()
                 if p.is_file() and p.suffix not in (".aria2", ".part", ".tmp")]
    except OSError:
        return None, dest
    for p in cands:
        if orig and p.name == orig:
            return p, dest
    if expected:
        same = []
        for p in cands:
            try:
                if p.stat().st_size == expected:
                    same.append(p)
            except OSError:
                continue
        if len(same) == 1:
            return same[0], dest
    return None, dest


def _completed_file_exists(info: dict) -> bool:
    """判断任务文件是否已完整落盘（源文件或归档文件）。"""
    try:
        src, dest = _task_paths(info)
    except Exception:
        return False
    expected = int(info.get("size") or 0)
    actual, _ = _resolve_actual_src(info)
    for p in (actual, dest):
        if p is None:
            continue
        try:
            if p.exists() and (not expected or p.stat().st_size == expected):
                return True
        except OSError:
            continue
    return False


def finalize_gopeed_task(tid: str, info: dict):
    """把下载完成的文件移到归档目录，然后 rclone 上传到 GDrive。

    返回 (是否成功, 错误类型)：
      (True, None)          上传成功
      (False, "missing")    源文件和归档文件都不存在（Gopeed 假 done），应终止
      (False, "incomplete") 文件大小与预期不符（下载被截断），应终止
      (False, "move")       移动失败，可重试
      (False, "upload")     上传失败，可重试
      (False, "gdrive")     上传失败且疑似授权失效，可重试并告警
    """
    try:
        src, dest = _resolve_actual_src(info)
    except Exception:
        log.error("任务映射缺少 saved_name/date 或归档路径非法，放弃: %s %s",
                  tid, info.get("rel_path"))
        return False, "missing"
    if src is None:
        # 没找到源文件：可能已经被移走，看看归档位置
        try:
            _, dest = _task_paths(info)
        except Exception:
            dest = None

    expected = int(info.get("size") or 0)
    dest_dir = dest.parent if dest else None

    if src is not None and src.exists():
        actual = src.stat().st_size
        if expected and actual != expected:
            log.error("源文件大小不完整，放弃该任务: %s (%d/%d)",
                      src, actual, expected)
            try:
                src.unlink()
            except OSError:
                pass
            return False, "incomplete"
        try:
            if dest_dir is None or dest is None:
                return False, "move"
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except Exception as exc:
            log.error("移动文件失败: %s", exc)
            return False, "move"
    elif dest is not None and dest.exists():
        actual = dest.stat().st_size
        if expected and actual != expected:
            log.error("归档文件大小不完整: %s (%d/%d)", dest, actual, expected)
            try:
                dest.unlink()
            except OSError:
                pass
            return False, "incomplete"
    else:
        # Gopeed 报告 done 但文件未落盘（如文件名含特殊字符），放弃避免无限重试
        log.error("源文件与归档文件均不存在，放弃该任务: %s", src)
        return False, "missing"
    # 已归档则直接上传（支持重试）

    try:
        rel_dir = dest.parent.relative_to(Path(GOPEED_DLDIR).resolve()).as_posix()
    except ValueError:
        return False, "missing"

    rc, out, err = _rclone_run(
        ["move", str(dest), f"{ARIA2_DEST}/{rel_dir}",
         # 大分片 + 多传输：实测 100MB 上传 8.37 → 9.52 MB/s，小文件也不受影响
         "--drive-chunk-size", "64M",
         "--buffer-size", "32M",
         "--transfers", "4",
         "--checkers", "8",
         # 防卡死：Google 侧偶发把上传连接挂住（发送队列堆积、CPU 为 0），
         # 没有这两个超时，一个卡住的 rclone 会永久占住上传并发名额。
         "--timeout", "120s",
         "--contimeout", "30s",
         "--low-level-retries", "10"],
        timeout=3600)
    if rc != 0:
        err_text = (err or out or "")
        log.error("上传失败 %s: %s", dest.name, err_text[:200])
        return False, ("gdrive" if is_auth_error(err_text, "gdrive") else "upload")
    log.info("上传成功: %s -> %s/%s", dest.name, ARIA2_DEST, rel_dir)

    # 修复空目录残留：清理本地可能已空的父级目录，避免空目录堆积
    try:
        p = dest.parent
        base = Path(GOPEED_DLDIR).resolve()
        while p != base and p.is_relative_to(base):
            if not any(p.iterdir()):
                p.rmdir()
                p = p.parent
            else:
                break
    except Exception as exc:
        log.debug("清理空目录失败: %s", exc)

    return True, None


def _archive_display(info: dict) -> str:
    """给用户展示的归档相对目录（不含文件名）。"""
    rel = safe_rel_path(
        info.get("rel_path") or info.get("orig_name") or "", fallback="")
    base = f"{info.get('date', '')}/{info.get('share', '')}"
    parent = str(rel.parent)
    if parent not in ("", "."):
        base += "/" + parent.replace(os.sep, "/")
    return base.rstrip("/")


_uploading_tasks: set[str] = set()
_UPLOAD_SEM = asyncio.Semaphore(UPLOAD_CONCURRENCY)

# 成功通知合并发送：一个分享几千个文件时，逐个发会被 Telegram 限流也会刷屏
_saved_buf: list[tuple[str, int, str]] = []
_saved_task = None
SAVED_NOTIFY_DELAY = 8.0


_saved_count = 0          # 累计已保存文件数（测试和排查用）


def _notify_saved(name: str, size: int, where: str) -> None:
    global _saved_task, _saved_count
    _saved_count += 1
    _saved_buf.append((name, int(size or 0), where))
    if _saved_task is None or _saved_task.done():
        _saved_task = asyncio.create_task(_flush_saved())


async def _flush_saved() -> None:
    """把 8 秒内攒下的"已保存"合并成一条消息。"""
    global _saved_task
    await asyncio.sleep(SAVED_NOTIFY_DELAY)
    batch = _saved_buf[:]
    _saved_buf.clear()
    _saved_task = None
    if not batch:
        return
    if len(batch) == 1:
        name, size, where = batch[0]
        await _notify(f"✅ 已保存到网盘\n{SEP}\n\n"
                      f"📄 {name}\n📦 {human(size)}\n📁 位置 {where}")
        return
    names = "\n".join(f"· {n}" for n, _, _ in batch[:8])
    more = f"\n… 还有 {len(batch) - 8} 个" if len(batch) > 8 else ""
    total = sum(s for _, s, _ in batch)
    await _notify(f"✅ 又保存了 {len(batch)} 个文件\n{SEP}\n\n{names}{more}\n\n"
                  f"📦 合计 {human(total)}\n📁 位置 {batch[0][2]}")


_feeding = False
_last_feed = 0.0
FEED_MIN_INTERVAL = _env_num("FEED_MIN_INTERVAL", 10.0, float, minimum=0.0)   # 两次续传最小间隔（分享信息有缓存，不用每波重扫）


async def _feed_queue():
    """把分批队列里的下一波放进下载器；同一时刻只允许一个续传协程。"""
    global _feeding, _last_feed
    if _feeding:
        return
    if time.time() - _last_feed < FEED_MIN_INTERVAL:
        return
    _feeding = True
    try:
        stats = await admit_wave()
        if stats["admitted"]:
            _last_feed = time.time()
            log.info("分批续传：放行 %d 个，队列剩 %d", stats["admitted"], stats["left"])
            if stats["left"] == 0:
                await _notify(
                    f"📥 大分享已全部开始下载\n{SEP}\n\n"
                    f"这批文件比较多，一直在边下载边上传\n"
                    f"现在剩下的也都安排上了，全部完成会陆续通知")
    except Exception as exc:
        log.error("分批续传失败: %s", exc)
    finally:
        _feeding = False


# ==================== aria2 分批队列（磁力 / 直链） ====================
# 一次发很多磁力/直链时，同时开太多任务会把磁盘塞满，也容易触发站方/网盘限流。
# 这里同样做排队：同时最多跑 ARIA2_MAX_ACTIVE 个，其余排队，磁盘不够就等。
ARIA2_QUEUE = Path(os.environ.get(
    "ARIA2_QUEUE", str(GOPEED_MAP.parent / "aria2_queue.json")))
ARIA2_MAX_ACTIVE = int(os.environ.get("ARIA2_MAX_ACTIVE", 2))
_aria2_stuck: dict = {}    # gid -> 第一次发现它「零进度」的时间
_aria2_err_seen: dict = {}  # gid -> 第一次看到它失败的时间（失败记录 1 小时后清掉）
ARIA2_QUEUE_LOCK = threading.Lock()
_ARIA2_FEEDING = False


def load_aria2_queue() -> list:
    try:
        data = json.loads(ARIA2_QUEUE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.error("读取 aria2 队列失败: %s", exc)
        return []


def save_aria2_queue(items: list) -> bool:
    try:
        ARIA2_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ARIA2_QUEUE.with_name(ARIA2_QUEUE.name + ".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, ARIA2_QUEUE)
        return True
    except Exception as exc:
        log.error("保存 aria2 队列失败: %s", exc)
        return False


def aria2_queue_add(uri: str) -> int:
    """把链接放进排队表，返回排队总数。"""
    with ARIA2_QUEUE_LOCK:
        items = load_aria2_queue()
        if not any(it.get("uri") == uri for it in items):
            items.append({"uri": uri, "added_at": int(time.time()), "tries": 0})
            save_aria2_queue(items)
        return len(items)


def aria2_queue_size() -> int:
    return len(load_aria2_queue())


async def feed_aria2(force: bool = False) -> dict:
    """按「并发上限 + 磁盘余量」把排队中的链接放进 aria2。"""
    global _ARIA2_FEEDING
    res = {"started": 0, "left": aria2_queue_size(), "gids": []}
    if _ARIA2_FEEDING:
        return res
    _ARIA2_FEEDING = True
    try:
        while True:
            items = load_aria2_queue()
            if not items:
                break
            try:
                active = await asyncio.to_thread(_aria2, "aria2.tellActive", [])
            except Exception as exc:
                log.error("aria2 不可用，暂停放行: %s", exc)
                break
            slots = ARIA2_MAX_ACTIVE - len(active)
            if slots <= 0:
                break
            _, hard = _disk_budget()
            if hard <= 0:
                log.warning("磁盘余量不足，暂停放行 aria2 任务")
                break
            item = items[0]
            try:
                gid = await asyncio.to_thread(
                    _aria2, "aria2.addUri", [[item["uri"]], {"dir": ARIA2_DLDIR}])
            except Exception as exc:
                with ARIA2_QUEUE_LOCK:
                    cur = load_aria2_queue()
                    if cur:
                        cur[0]["tries"] = int(cur[0].get("tries") or 0) + 1
                        if cur[0]["tries"] >= 3:
                            cur.pop(0)
                        save_aria2_queue(cur)
                log.error("aria2 添加失败（%d/3）: %s", int(item.get("tries") or 0) + 1, exc)
                if is_auth_error(str(exc), "gdrive"):
                    await send_auth_alert("gdrive", str(exc))
                continue
            with ARIA2_QUEUE_LOCK:
                cur = load_aria2_queue()
                if cur:
                    cur.pop(0)
                    save_aria2_queue(cur)
            res["started"] += 1
            res["gids"].append(gid)
            await asyncio.sleep(0.3)
    except Exception as exc:
        log.error("aria2 放行异常: %s", exc)
    finally:
        _ARIA2_FEEDING = False
        res["left"] = aria2_queue_size()
    return res


# ==================== 下载失败熔断 ====================
# 夸克/CDN 侧偶发短时间拒绝（HTTP 412），此时重试只会把重试次数烧光。
# 连续多个失败就整体暂停一段时间，文件留在映射里，过后自动重试。
_cdn_fail_times: list[float] = []
_cdn_block_until = 0.0
CDN_BREAK_WINDOW = 120.0
CDN_BREAK_THRESHOLD = 3
CDN_BREAK_SECONDS = 600


def _note_download_failure() -> bool:
    """记录一次下载失败；短时间内多次失败则熔断，返回是否刚触发。"""
    global _cdn_block_until
    now = time.time()
    _cdn_fail_times.append(now)
    cutoff = now - CDN_BREAK_WINDOW
    while _cdn_fail_times and _cdn_fail_times[0] < cutoff:
        _cdn_fail_times.pop(0)
    if len(_cdn_fail_times) >= CDN_BREAK_THRESHOLD and now > _cdn_block_until:
        _cdn_block_until = now + CDN_BREAK_SECONDS
        _cdn_fail_times.clear()
        return True
    return False


def cdn_blocked_for() -> int:
    """距离熔断结束还有多少秒（0 表示没熔断）。"""
    return max(0, int(_cdn_block_until - time.time()))


async def _drop_bridge_copy(info: dict) -> None:
    """删掉中转副本（下载完成或上传成功后调用）。

    own_acct: main/bridge = 夸克中转目录；pikpak = PikPak 自己网盘。
    """
    own_fid = info.get("own_fid")
    if not own_fid:
        return
    kind = info.get("own_acct") or "main"
    info["own_fid"] = ""          # 先清掉，避免重复删
    try:
        if kind == "pikpak":
            tok = _read_pikpak_token()
            if tok:
                cli = PikPakShare(refresh_token=tok)
                await asyncio.to_thread(cli.ensure_login)
                await asyncio.to_thread(cli.delete_files, [own_fid])
                log.info("已删除 PikPak 中转副本: %s", info.get("orig_name"))
            return
        q = _client_for(kind)
        await asyncio.to_thread(q.delete, [own_fid])
        log.info("已删除中转副本（%s 账号）: %s", kind, info.get("orig_name"))
    except Exception as exc:
        log.warning("删除中转副本失败（不影响结果，稍后由清理任务处理）: %s", exc)


async def _upload_and_finalize(tid: str, info: dict):
    """异步执行上传与归档，由 _UPLOAD_SEM 限制最多 2 个并发上传，不阻塞 watcher 轮询。"""
    _uploading_tasks.add(tid)
    try:
        async with _UPLOAD_SEM:
            ok, err_kind = await asyncio.to_thread(
                finalize_gopeed_task, tid, info)

        if ok:
            map_pop(tid)
            try:
                await asyncio.to_thread(gopeed.remove_task, tid)
            except Exception:
                pass
            await _drop_bridge_copy(info)
            _notify_saved(info.get("orig_name") or "文件",
                          info.get("size") or 0,
                          f"Aria2Files › {_archive_display(info).replace('/', ' › ')}")
        elif err_kind in ("missing", "incomplete"):
            # Gopeed 假 done 或文件被截断，重试没有意义
            reason = ("下载显示完成了，但文件其实没落盘（文件名可能带特殊字符）"
                      if err_kind == "missing"
                      else "下载下来的文件大小不对，可能被截断了")
            await _drop_bridge_copy(info)
            map_pop(tid)
            try:
                await asyncio.to_thread(gopeed.remove_task, tid)
            except Exception:
                pass
            await _notify(
                f"❌ 这个文件没存成功\n{SEP}\n\n"
                f"📄 {info.get('orig_name')}\n"
                f"原因：{reason}\n"
                f"麻烦重新发一次链接，我再来一遍")
        else:
            if err_kind == "gdrive":
                await send_auth_alert(
                    "gdrive", f"上传 {info.get('orig_name')} 时失败")
            retry = int(info.get("upload_retry") or 0)
            limit = (AUTH_MAX_RETRY if err_kind == "gdrive"
                     else UPLOAD_MAX_RETRY)
            if retry < limit:
                delay = (3600 if err_kind == "gdrive"
                         else min(60 * (2 ** retry), 3600))
                info["upload_retry"] = retry + 1
                info["next_retry_at"] = int(time.time() + delay)
                info["last_error"] = err_kind
                map_update(tid, info)
                log.warning("上传失败，%d 秒后重试 %d/%d: %s",
                            delay, retry + 1, limit,
                            info.get("orig_name"))
            else:
                map_pop(tid)
                try:
                    await asyncio.to_thread(gopeed.remove_task, tid)
                except Exception:
                    pass
                await _notify(
                    f"❌ 这个文件没能存进网盘\n{SEP}\n\n"
                    f"📄 {info.get('orig_name')}\n"
                    f"已经自动重试 {retry} 次还是失败。\n"
                    f"文件我留在服务器上了（Aria2Files › {_archive_display(info).replace('/', ' › ')}），"
                    f"{LEFTOVER_KEEP_DAYS} 天内可以补传")
    except Exception as exc:
        log.error("上传处理异常 %s: %s", tid, exc)
    finally:
        _uploading_tasks.discard(tid)


async def _aria2_janitor():
    """清掉 aria2 里「卡住不动」和「已完成」的任务。
    为什么需要：aria2 会把已完成任务一直留在 tellStopped 里，卡住的任务
    也永远停在 active，任务清单就会一直显示几条下不完的（2026-11-05 现场：
    两条 zz_direct_speed.bin 卡了整整几天）。
    """
    now = time.time()
    try:
        active = await asyncio.to_thread(_aria2, "aria2.tellActive", [])
    except Exception as exc:
        log.debug("aria2 清理：取活动任务失败 %s", exc)
        return
    for t in active:
        gid = t.get("gid")
        if not gid:
            continue
        done = int(t.get("completedLength") or 0)
        total = int(t.get("totalLength") or 0)
        if done == 0 and total == 0:
            first = _aria2_stuck.setdefault(gid, now)
            if now - first > 1800:
                try:
                    await asyncio.to_thread(_aria2, "aria2.forceRemove", [gid])
                    await asyncio.to_thread(
                        _aria2, "aria2.removeDownloadResult", [gid])
                    log.warning("清理卡住的 aria2 任务（30 分钟无任何进度）: %s", gid)
                except Exception as exc:
                    log.debug("清理卡住任务失败: %s", exc)
                _aria2_stuck.pop(gid, None)
        else:
            _aria2_stuck.pop(gid, None)
    try:
        stopped = await asyncio.to_thread(_aria2, "aria2.tellStopped", [0, 200])
        drop = []
        for t in stopped:
            gid = t.get("gid")
            st = t.get("status")
            if st in ("complete", "removed"):
                drop.append(gid)
            elif st == "error":
                # 失败记录留 1 小时给用户看，之后清掉，否则任务清单里会一直挂着
                first = _aria2_err_seen.setdefault(gid, now)
                if now - first > 3600:
                    drop.append(gid)
        for gid in drop:
            try:
                await asyncio.to_thread(_aria2, "aria2.removeDownloadResult", [gid])
                _aria2_err_seen.pop(gid, None)
            except Exception:
                pass
        if drop:
            log.info("清理 aria2 历史记录 %d 条", len(drop))
    except Exception as exc:
        log.debug("aria2 清理：取历史失败 %s", exc)


async def gopeed_watcher():
    """后台轮询 Gopeed 任务，完成后自动上传到 Google Drive。

    所有映射修改都通过 map_* 原子完成，避免长时间上传期间把新增任务覆盖掉。
    上传采用后台并发任务，不再串行阻塞 watcher 循环。
    下载失败重试先清旧任务与残件，彻底解决删除竞态。
    """
    await asyncio.sleep(5)
    log.info("Gopeed watcher 已启动")
    _loop_count = 0
    poll_errors: dict[str, int] = {}
    last_list_warn = 0.0
    while True:
        try:
            now = time.time()
            mapping = load_gopeed_map()
            # 一轮只拉一次任务列表：映射里有上千个任务时，
            # 逐任务 get_task 会变成每 5 秒上千次串行 HTTP，把轮询周期拖垮。
            try:
                snapshot = {t.get("id"): t
                            for t in await asyncio.to_thread(gopeed.list_tasks)
                            if t.get("id")}
                list_error = None
            except Exception as exc:
                snapshot, list_error = None, exc

            for tid, info in list(mapping.items()):
                if float(info.get("next_retry_at") or 0) > now:
                    continue

                if snapshot is None:
                    # Gopeed 暂时不可用（重启/崩溃）：整轮跳过，
                    # 不能据此把任务判成「已消失」，否则会误报失败。
                    if time.time() - last_list_warn > 60:
                        last_list_warn = time.time()
                        log.warning("拉取 Gopeed 任务列表失败，本轮跳过: %s", list_error)
                    break

                task = snapshot.get(tid)
                if task is None:
                    # Gopeed 重启后任务会消失。连续 5 分钟取不到时：
                    # 文件已落盘就继续上传，否则判定任务丢失并终止。
                    n = poll_errors.get(tid, 0) + 1
                    poll_errors[tid] = n
                    if n < 60:
                        continue
                    poll_errors.pop(tid, None)
                    if _completed_file_exists(info):
                        task = {"status": "done"}
                    else:
                        log.error("任务在 Gopeed 中消失，放弃: %s", tid)
                        map_pop(tid)
                        await _notify(
                            f"❌ 这个文件没下载成\n{SEP}\n\n"
                            f"📄 {info.get('orig_name')}\n"
                            f"原因：下载器那边已经找不到这个任务了（可能重启过），文件也没下完\n"
                            f"麻烦重新发一次链接")
                        continue
                else:
                    poll_errors.pop(tid, None)

                if gopeed.is_done(task):
                    if info.get("own_fid"):
                        # 下载已完成，中转副本可以立刻删，尽早把夸克空间还回去
                        asyncio.create_task(_drop_bridge_copy(info))
                    if tid not in _uploading_tasks:
                        asyncio.create_task(_upload_and_finalize(tid, info))
                elif gopeed.is_failed(task):
                    if cdn_blocked_for():
                        # 熔断中：既不重试也不计次数，等窗口过去再说
                        continue
                    retry = int(info.get("retry") or 0)
                    if retry >= 3 or not (info.get("pwd_id") and info.get("fid")
                                          and info.get("fid_token")):
                        log.warning("任务最终失败（已重试 %d 次）: %s",
                                    retry, info.get("orig_name"))
                        # 失败也要清中转副本：PikPak/夸克的空间不能一直被占着
                        await _drop_bridge_copy(info)
                        map_pop(tid)
                        try:
                            await asyncio.to_thread(gopeed.remove_task, tid)
                        except Exception:
                            pass
                        await _notify(
                            f"❌ 这个文件没下载成\n{SEP}\n\n"
                            f"📄 {info.get('orig_name')}\n"
                            f"已经自动重试 {retry} 次还是失败，多半是夸克的下载节点连不上\n"
                            f"过一会儿再发一次链接试试")
                        continue

                    attempt = retry + 1
                    info["retry"] = attempt
                    try:
                        # 1. 先向夸克重新获取直链（优先公开分享）
                        q2, st2, files2 = await _open_share(
                            info["pwd_id"], info.get("passcode", ""))
                        # share_fid_token 会过期（夸克返回 41020「token 校验异常」），
                        # 重试时必须重扫分享换新 token，否则重试注定失败。
                        fresh = next((x for x in files2
                                      if x.get("fid") == info.get("fid")), None)
                        if fresh and fresh.get("share_fid_token"):
                            if fresh["share_fid_token"] != info.get("fid_token"):
                                log.info("刷新 fid_token: %s", info.get("orig_name"))
                            info["fid_token"] = fresh["share_fid_token"]
                        # 分享直链不可用（或这个文件本来就走转存）时，重试也走转存桥接
                        if not await _share_direct_for(q2, files2, info["pwd_id"], st2):
                            size2 = int(info.get("size") or 0)
                            pref = "bridge" if size2 <= BRIDGE_ACC_MAX_BYTES else "main"
                            m2, fit2 = await _bridge_group(
                                info["pwd_id"], info.get("passcode", ""),
                                [{"fid": info["fid"], "size": size2}], q2, st2,
                                {info["fid"]: {"share_fid_token": info["fid_token"]}},
                                pref)
                            if not m2:
                                raise QuarkError("两个账号都转存失败（空间或权限）")
                            own_fid2, own_acct2 = m2[info["fid"]]
                            info["own_fid"] = own_fid2
                            info["own_acct"] = own_acct2
                            bq2 = _client_for(own_acct2)
                            url2 = await asyncio.to_thread(bq2.own_direct_url, own_fid2)
                            hdrs2 = await asyncio.to_thread(bq2.download_headers)
                        else:
                            info["own_fid"] = ""
                            info["own_acct"] = ""
                            url2 = await asyncio.to_thread(
                                q2.get_direct_url, info["fid"], info["fid_token"],
                                info["pwd_id"], st2)
                            hdrs2 = await asyncio.to_thread(q2.download_headers)

                        # 2. 直链获取成功后，先删除旧任务及磁盘残件，彻底避免新任务被旧任务误删
                        try:
                            await asyncio.to_thread(gopeed.remove_task, tid)
                        except Exception:
                            pass
                        part = Path(GOPEED_DLDIR) / (info.get("saved_name") or "")
                        for f in (part, Path(str(part) + ".aria2")):
                            if f.exists():
                                try:
                                    f.unlink()
                                except OSError:
                                    pass

                        # 3. 创建全新干净任务
                        new_tid = await asyncio.to_thread(
                            gopeed.add_task, url2, info["saved_name"],
                            GOPEED_DLDIR, hdrs2,
                            gopeed_connections(int(info.get("size") or 0)))
                        info["next_retry_at"] = 0
                        map_replace(tid, new_tid, info)
                        log.info("下载失败，已重新加入队列 %d/3: %s",
                                 attempt, info.get("orig_name"))
                    except Exception as exc:
                        log.error("重试失败 %s: %s", info.get("orig_name"), exc)
                        if _token_error(exc):
                            # token 过期：丢掉缓存，下次重试用全新 token
                            _drop_share_cache(info.get("pwd_id", ""))
                        if _note_download_failure():
                            log.warning("连续下载失败，熔断 %d 秒", CDN_BREAK_SECONDS)
                            await _notify(
                                f"⚠️ 夸克那边暂时拒绝下载，已自动暂停\n{SEP}\n\n"
                                f"短时间内好几个文件同时失败，多半是被临时限流了\n"
                                f"我把所有下载暂停了 {CDN_BREAK_SECONDS // 60} 分钟，"
                                f"到点会自动重试\n"
                                f"文件都还在队列里，不用重新发链接")
                        auth_err = is_auth_error(str(exc), "quark")
                        if auth_err:
                            await send_auth_alert("quark", str(exc))
                        delay = 3600 if auth_err else min(10 * (2 ** retry), 120)
                        info["next_retry_at"] = int(time.time() + delay)
                        info["last_error"] = str(exc)[:200]
                        if attempt >= 3:
                            map_pop(tid)
                            try:
                                await asyncio.to_thread(gopeed.remove_task, tid)
                            except Exception:
                                pass
                            await _notify(
                                f"❌ 这个文件没下载成\n{SEP}\n\n"
                                f"📄 {info.get('orig_name')}\n"
                                f"重新换下载地址也失败了\n"
                                f"{human_error(str(exc))}\n"
                                f"过一会儿再发一次链接试试")
                        else:
                            map_update(tid, info)
                            log.warning("重新入队失败，%d 秒后再试 %d/3",
                                        delay, attempt)
        except Exception as exc:
            log.error("gopeed watcher 异常: %s", exc)

        # 分批队列：在途任务少下来、队列还有货，就补下一波（磁盘够才放行）
        try:
            if queue_pending() and len(load_gopeed_map()) <= FEED_AT_MAP_SIZE:
                asyncio.create_task(_feed_queue())
        except Exception as exc:
            log.debug("触发分批续传失败: %s", exc)

        # aria2 排队表：磁力/直链同样按并发数 + 磁盘余量分批放行
        try:
            if aria2_queue_size():
                asyncio.create_task(feed_aria2())
        except Exception as exc:
            log.debug("触发 aria2 放行失败: %s", exc)

        # PikPak 队列：空间小，转存一个下一个，下完立刻删
        try:
            if pk_queue_left() and pk_inflight() < PIKPAK_MAX_INFLIGHT:
                asyncio.create_task(feed_pikpak())
        except Exception as exc:
            log.debug("触发 PikPak 放行失败: %s", exc)

        # aria2 清理：卡住不动 / 已完成的记录，避免任务清单里永远挂着几条
        _loop_count += 1
        if _loop_count % 60 == 1:          # 约每 5 分钟一次
            asyncio.create_task(_aria2_janitor())

        await asyncio.sleep(5)


def gopeed_saved_name(pwd_id: str, rel_path: str, orig_name: str) -> str:
    """生成全局唯一的下载文件名，避免同名文件并发写同一路径。

    保留原扩展名，总字节数控制在 240 以内（文件系统单段上限 255 字节）。
    """
    digest = hashlib.sha1(
        f"{pwd_id}/{rel_path}".encode("utf-8")).hexdigest()[:10]
    ext = os.path.splitext(safe_name(orig_name, max_bytes=32))[1][:16]
    stem = os.path.splitext(safe_name(orig_name, max_bytes=200))[0]
    prefix = f"{pwd_id[:8]}_{digest}_"
    room = 240 - len((prefix + ext).encode("utf-8"))
    raw = stem.encode("utf-8")
    if len(raw) > room:
        stem = raw[:max(room, 0)].decode("utf-8", "ignore").rstrip(" .")
    return f"{prefix}{stem}{ext}"


def _share_layout(files: list[dict], pwd_id: str):
    """决定归档目录名和每个文件在分享内的相对路径。

    - 整个分享只有一个顶层目录：用该目录名+短pwd_id作为分享名，并去掉这一层前缀，彻底避免不同分享同名目录冲突
    - 单个根文件：用文件名（不含扩展名）+短pwd_id作为分享名
    - 其他（多个顶层目录/根文件混合）：用「分享_<pwd_id 前 8 位>」，保留完整路径
    """
    paths = [(f.get("path") or f.get("name") or "").replace("\\", "/")
             for f in files]
    tops = {p.split("/", 1)[0] for p in paths if p}
    suffix = f"_{pwd_id[:6]}"
    if len(tops) == 1 and all("/" in p for p in paths):
        share_name = f"{safe_name(next(iter(tops)))}{suffix}"
        rel_paths = [p.split("/", 1)[1] for p in paths]
    elif len(files) == 1:
        share_name = f"{safe_name(os.path.splitext(files[0]['name'])[0])}{suffix}"
        rel_paths = [files[0]["name"]]
    else:
        share_name = f"分享_{pwd_id[:8]}"
        rel_paths = paths
    return share_name, rel_paths


# 同一分享的并发提交必须串行处理：去重是「读快照 → 异步取直链 → 写映射」，
# 不加锁时两条并发消息会双双通过去重，生成两个写同一路径的 Gopeed 任务，
# 先完成的那个上传后还会 force-delete 掉后一个任务正在写的文件。
_SHARE_LOCKS: dict[str, asyncio.Lock] = {}


def _share_lock(pwd_id: str) -> asyncio.Lock:
    lock = _SHARE_LOCKS.get(pwd_id)
    if lock is None:
        lock = asyncio.Lock()
        _SHARE_LOCKS[pwd_id] = lock
    return lock


# ==================== PikPak 分批队列（空间小，下完即删） ====================
# PikPak 免费号往往只有几 GB，转存进去必须第一时间删，绝对不能攒着。
# 所以这里和夸克一样做"排队 + 一波几个"，每个文件下载完立刻删副本。
PIKPAK_QUEUE = Path(os.environ.get(
    "PIKPAK_QUEUE", str(GOPEED_MAP.parent / "pikpak_queue.json")))
PIKPAK_MAX_INFLIGHT = _env_num("PIKPAK_MAX_INFLIGHT", 2)
PIKPAK_FREE_RATIO = 0.5          # 一波最多占 PikPak 剩余空间的一半
_pk_lock = threading.Lock()
_pk_feeding = False
_pk_quota_cache = {"ts": 0.0, "limit": 0, "usage": 0}


def load_pk_queue() -> dict:
    try:
        d = json.loads(PIKPAK_QUEUE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.error("读取 PikPak 队列失败: %s", exc)
        return {}


def save_pk_queue(q: dict) -> bool:
    try:
        PIKPAK_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PIKPAK_QUEUE.with_name(PIKPAK_QUEUE.name + ".tmp")
        tmp.write_text(json.dumps(q, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, PIKPAK_QUEUE)
        return True
    except Exception as exc:
        log.error("保存 PikPak 队列失败: %s", exc)
        return False


def pk_queue_put(share_id: str, name: str, date_str: str, items: list) -> int:
    with _pk_lock:
        q = load_pk_queue()
        ent = q.setdefault(share_id, {"name": name, "date": date_str, "items": []})
        ent["name"], ent["date"] = name, date_str
        have = {x.get("id") for x in ent.get("items") or []}
        for it in items:
            if it.get("id") not in have:
                ent["items"].append(it)
                have.add(it.get("id"))
        save_pk_queue(q)
        return sum(len(v.get("items") or []) for v in q.values())


def pk_queue_left() -> int:
    return sum(len(v.get("items") or []) for v in load_pk_queue().values())


def pk_inflight() -> int:
    return sum(1 for v in load_gopeed_map().values()
               if v.get("own_acct") == "pikpak")


async def _pk_about(refresh: bool = False) -> tuple:
    """PikPak 容量（缓存 2 分钟）。"""
    c = _pk_quota_cache
    if not refresh and c["ts"] and time.time() - c["ts"] < 120:
        return c["limit"], c["usage"]
    tok = _read_pikpak_token()
    if not tok:
        return c["limit"], c["usage"]
    try:
        cli = PikPakShare(refresh_token=tok)
        await asyncio.to_thread(cli.ensure_login)
        limit, usage = await asyncio.to_thread(cli.about)
        if limit:
            c.update(ts=time.time(), limit=limit, usage=usage)
    except Exception as exc:
        log.warning("查询 PikPak 容量失败（按上次值）: %s", exc)
    return c["limit"], c["usage"]


async def feed_pikpak(force: bool = False) -> dict:
    """按「PikPak 剩余空间 + 在途数量」放行下一批转存。"""
    global _pk_feeding
    res = {"started": 0, "left": pk_queue_left()}
    if _pk_feeding:
        return res
    q = load_pk_queue()
    if not q:
        return res
    tok = _read_pikpak_token()
    if not tok:
        return res
    if not force and pk_inflight() >= PIKPAK_MAX_INFLIGHT:
        return res
    _pk_feeding = True
    try:
        cli = PikPakShare(refresh_token=tok)
        await asyncio.to_thread(cli.ensure_login)
        limit, usage = await _pk_about(refresh=True)
        free = max(0, limit - usage)
        _, hard = _disk_budget()
        budget = int(min(free * PIKPAK_FREE_RATIO, hard * 0.5))
        slots = PIKPAK_MAX_INFLIGHT - pk_inflight()
        hdrs = cli.download_headers()
        for share_id, ent in list(q.items()):
            items = ent.get("items") or []
            while items and slots > 0:
                if budget <= 0:
                    break
                it = items[0]
                size = int(it.get("size") or 0)
                if size > budget:
                    # 单个文件就超过可用空间：留着，别硬转存（会塞满）
                    log.warning("PikPak 空间不足（剩 %.1f MB），先不转存 %s",
                                budget / 1048576, (it.get("name") or "")[:30])
                    break
                try:
                    trace = await asyncio.to_thread(
                        cli.restore, share_id, [it["id"]], it.get("ancestors") or [])
                    own = trace.get(it["id"])
                    if not own:
                        raise PikPakError("转存没返回文件 id")
                    url = await asyncio.to_thread(cli.own_file_url, own)
                    if not url:
                        raise PikPakError("转存后取不到直链")
                    saved_name = gopeed_saved_name(
                        "pk" + share_id, it.get("path") or it["name"], it["name"])
                    tid = await asyncio.to_thread(
                        gopeed.add_task, url, saved_name, GOPEED_DLDIR, hdrs,
                        gopeed_connections(size))
                    map_add(tid, {
                        "date": ent.get("date"),
                        "share": ent.get("name") or ("PikPak_" + share_id[-6:]),
                        "orig_name": it["name"], "saved_name": saved_name,
                        "rel_path": it.get("path") or it["name"],
                        "size": size, "added_at": int(time.time()),
                        "pwd_id": "", "passcode": "", "fid": it["id"],
                        "fid_token": "", "retry": 0,
                        "own_fid": own, "own_acct": "pikpak",
                    })
                    items.pop(0)
                    res["started"] += 1
                    slots -= 1
                    budget -= size
                    log.info("PikPak 转存并开始下载: %s", (it.get("name") or "")[:40])
                    await asyncio.sleep(WAVE_PACE)
                except Exception as exc:
                    log.error("PikPak 转存失败 %s: %s",
                              (it.get("name") or "")[:30], exc)
                    break
            ent["items"] = items
            if not items:
                q.pop(share_id, None)
        save_pk_queue(q)
    except Exception as exc:
        log.error("PikPak 放行异常: %s", exc)
    finally:
        _pk_feeding = False
        res["left"] = pk_queue_left()
    return res


# ==================== 提交前自检（pre-flight） ====================
# 目的：用户发来链接/文件时先体检，确认「真的能下、真的能存」再开工，
# 而不是先建一堆任务、然后挨个失败。
PREFLIGHT_CACHE = {"gdrive": (0.0, True, "")}

# 自检没过时把用户这次发的内容存下来，修好后点按钮就能直接续上
PENDING_FILE = Path(os.environ.get(
    "PENDING_SUBMIT", "/opt/cloud-disk-transfer/logs/pending_submit.json"))


def _save_pending_submit(text: str) -> None:
    try:
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        PENDING_FILE.write_text(
            json.dumps({"text": text, "ts": int(time.time())},
                       ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.warning("保存待重试提交失败: %s", exc)


def _load_pending_submit() -> str:
    try:
        d = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        if time.time() - int(d.get("ts") or 0) < 86400:
            return d.get("text") or ""
    except Exception:
        pass
    return ""


async def _check_gdrive() -> tuple:
    """rclone 能不能读到 gdrive（缓存 10 分钟）。"""
    ts, ok, detail = PREFLIGHT_CACHE["gdrive"]
    if ts and time.time() - ts < 600:
        return ok, detail
    try:
        r = await asyncio.to_thread(
            lambda: subprocess.run(
                [RCLONE_BIN, "--config", RCLONE_CONF, "about", "gdrive:"],
                capture_output=True, text=True, timeout=30))
        ok = r.returncode == 0
        detail = "" if ok else (r.stderr or r.stdout or "")[:120]
    except Exception as exc:
        ok, detail = False, str(exc)[:120]
    PREFLIGHT_CACHE["gdrive"] = (time.time(), ok, detail)
    return ok, detail


def _fmt_check(ok: bool, name: str, detail: str = "") -> str:
    return f"{'✅' if ok else '❌'} {name}" + (f"：{detail}" if detail else "")


async def preflight_common() -> tuple:
    """服务层自检：磁盘、下载器、网盘授权、两个夸克账号。"""
    rows = []
    ok_all = True

    hard = _disk_budget()[1]
    ok = hard > 0
    ok_all = ok_all and ok
    rows.append(_fmt_check(ok, "服务器磁盘",
                           f"可用于下载 {human(hard)}" if ok
                           else f"只剩 {human(hard)}，低于安全线"))

    try:
        tasks = await asyncio.to_thread(gopeed.list_tasks)
        rows.append(_fmt_check(True, "下载器 Gopeed", f"{len(tasks)} 个任务"))
    except Exception as exc:
        ok_all = False
        rows.append(_fmt_check(False, "下载器 Gopeed", str(exc)[:60]))

    try:
        await asyncio.to_thread(_aria2, "aria2.getVersion", [])
        rows.append(_fmt_check(True, "下载器 aria2", "正常"))
    except Exception as exc:
        ok_all = False
        rows.append(_fmt_check(False, "下载器 aria2", str(exc)[:60]))

    ok, detail = await _check_gdrive()
    ok_all = ok_all and ok
    rows.append(_fmt_check(ok, "Google Drive 授权",
                           "正常" if ok else (detail or "读不到，需要 reconnect")))

    try:
        info = await asyncio.to_thread(_client_for("main").member_info)
        total = int(info.get("total_capacity") or 0)
        used = int(info.get("use_capacity") or 0)
        free = max(0, total - used)
        ok = free > QUARK_MIN_FREE_MB * 1024 * 1024
        ok_all = ok_all and ok
        rows.append(_fmt_check(ok, "夸克主号",
                               f"{info.get('member_type', '?')}，剩 {human(free)}"))
    except Exception as exc:
        ok_all = False
        rows.append(_fmt_check(False, "夸克主号",
                               f"Cookie 可能失效：{str(exc)[:60]}"))

    if bridge_account_ready():
        try:
            free = await _quark_free("bridge")
            ok = free > 20 * 1024 * 1024
            ok_all = ok_all and ok
            rows.append(_fmt_check(ok, "夸克转存号", f"剩 {human(free)}"))
        except Exception as exc:
            ok_all = False
            rows.append(_fmt_check(False, "夸克转存号", str(exc)[:60]))

    return ok_all, rows


async def preflight_share(q, st: str, files: list, pwd_id: str) -> tuple:
    """分享层自检：抽样验证这个分享能不能下。

    返回 (verdict, rows, downloadable, blocked)
      verdict: "ok" / "partial" / "blocked"
    """
    rows = []
    if not files:
        return "blocked", ["❌ 分享里没有文件"], 0, 0

    biggest = max(files, key=lambda f: f.get("size") or 0)
    smallest = min(files, key=lambda f: f.get("size") or 0)
    sample = [smallest] if smallest is biggest else [smallest, biggest]

    direct_ok = False
    bridge_ok = False
    for f in sample:
        tag = f"{f['name'][:20]}（{human(f.get('size') or 0)}）"
        url, hdrs = None, None
        try:
            url = await asyncio.to_thread(
                q.get_direct_url, f["fid"], f.get("share_fid_token", ""),
                pwd_id, st)
            hdrs = await asyncio.to_thread(q.download_headers)
        except Exception as exc:
            rows.append(_fmt_check(False, f"分享直链 · {tag}",
                                   human_error(str(exc))))
        if url and await asyncio.to_thread(_probe_url, url, hdrs):
            direct_ok = True
            rows.append(_fmt_check(True, f"分享直链 · {tag}", "可下载"))
            continue
        try:
            mapping = await _bridge_save_fresh(
                pwd_id, "", [{"fid": f["fid"], "name": f["name"],
                              "size": f.get("size") or 0,
                              "pdir": f.get("pdir", "")}],
                q, st, {f["fid"]: f})
            own = mapping.get(f["fid"])
            if not own:
                rows.append(_fmt_check(False, f"转存后下载 · {tag}", "转存没返回文件"))
                continue
            mc = _client_for("main")
            u2 = await asyncio.to_thread(mc.own_direct_url, own)
            h2 = await asyncio.to_thread(mc.download_headers)
            good = await asyncio.to_thread(_probe_url, u2, h2)
            await _drop_bridge_copy({"own_fid": own, "own_acct": "main",
                                     "orig_name": f["name"]})
            if good:
                bridge_ok = True
                rows.append(_fmt_check(True, f"转存后下载 · {tag}", "可下载"))
            else:
                rows.append(_fmt_check(False, f"转存后下载 · {tag}", "直链被拒"))
        except Exception as exc:
            if "23018" in str(exc) or "size limit" in str(exc).lower():
                _note_size_limit("main", int(f.get("size") or 0))
                rows.append(_fmt_check(False, f"转存后下载 · {tag}",
                                       "超过账号下载大小限制"))
            else:
                rows.append(_fmt_check(False, f"转存后下载 · {tag}",
                                       human_error(str(exc))))

    cap = _learned_cap.get("main") or 0
    big = [f for f in files if cap and (f.get("size") or 0) > cap]
    downloadable = len(files) - len(big)
    if not direct_ok and not bridge_ok:
        verdict = "blocked"
    elif downloadable == 0:
        verdict = "blocked"
    elif big:
        verdict = "partial"
    else:
        verdict = "ok"
    return verdict, rows, downloadable, len(big)


async def handle_pikpak_share(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              text: str):
    """PikPak 分享：解析出直链后交给 Gopeed 下载，完成后照常上传 Google Drive。

    未登录时只能取视频地址；非视频（zip 等）需要配 PIKPAK_REFRESH_TOKEN（登录后转存再取直链）。
    """
    msg = await update.message.reply_text("🩺 正在自检（确认能下才开工）…")

    ok_env, env_rows = await preflight_common()
    if not ok_env:
        await msg.edit_text(
            f"🩺 提交前自检\n{SEP}\n\n" + "\n".join(env_rows) +
            "\n\n⛔ 已暂停，没有开始下载\n按上面 ❌ 项处理后，把链接再发一次")
        _save_pending_submit(text)
        try:
            await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔁 重新自检并开始", callback_data="pre:retry")]]))
        except Exception:
            pass
        return

    await msg.edit_text("🔍 正在解析 PikPak 分享…")
    try:
        share_id, pwd = pikpak_parse(text)
    except Exception as exc:
        await msg.edit_text(f"❌ {exc}")
        return

    token = _read_pikpak_token()
    client = PikPakShare(refresh_token=token, share_pwd=pwd)
    logged_in = False
    try:
        logged_in = await asyncio.to_thread(client.ensure_login)
        # refresh_token 会被轮换：把最新值写回文件，否则重启后失效
        if logged_in and client.refresh_token and client.refresh_token != token:
            _save_pikpak_token(client.refresh_token)
    except Exception as exc:
        log.warning("PikPak 登录失败（继续未登录模式）: %s", exc)

    try:
        if pwd:
            await asyncio.to_thread(client.unlock, share_id)
        files = await asyncio.to_thread(client.walk_share, share_id)
    except Exception as exc:
        log.error("PikPak 解析失败: %s", exc)
        await msg.edit_text(
            f"❌ 这个 PikPak 分享没解析成功\n{human_error(str(exc))}\n\n"
            f"如果是链接打不开，让对方重新分享一次")
        return

    if not files:
        await msg.edit_text("⚠️ 这个分享里没有文件")
        return
    if len(files) > MAX_SHARE_FILES:
        await msg.edit_text(
            f"⚠️ 这个分享有 {len(files)} 个文件，超过单次上限 {MAX_SHARE_FILES} 个")
        return

    # 逐个取直链；未登录时只有视频有
    await msg.edit_text(f"🔍 分享里共 {len(files)} 个文件，正在取下载地址…")
    resolved, missing = [], []
    for f in files:
        try:
            info = await asyncio.to_thread(client.file_info, share_id, f["id"])
            url = PikPakShare.pick_url(info)
        except Exception as exc:
            log.warning("PikPak 取直链失败 %s: %s", f["name"][:30], exc)
            url = ""
        if url:
            resolved.append((f, url))
        else:
            missing.append(f)

    # 非视频 + 已登录 → 不立刻全转存，先进队列，由 feed_pikpak 按空间分批转存
    queued = 0
    if missing and logged_in:
        date_str0 = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
        share_name0 = safe_name("PikPak_" + share_id[-6:], fallback="PikPak")
        items = [{"id": f["id"], "name": f["name"], "size": f.get("size") or 0,
                  "path": f["path"], "ancestors": f.get("ancestors") or []}
                 for f in missing]
        queued = pk_queue_put(share_id, share_name0, date_str0, items)

    if not resolved and not queued:
        tip = ("已配置登录，但仍没拿到下载地址（可能今日转存次数用完）"
               if logged_in else
               "未登录访问只给视频的播放地址。zip/压缩包等非视频文件需要登录 PikPak")
        await msg.edit_text(
            f"❌ 这个分享暂时下不了\n{SEP}\n\n{tip}\n\n"
            f"要支持非视频文件：把你的 PikPak refresh_token 配到服务器环境变量 "
            f"PIKPAK_REFRESH_TOKEN（浏览器 F12 里可拿到）")
        _save_pending_submit(text)
        return

    # 建任务
    date_str = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
    share_name = safe_name("PikPak_" + share_id[-6:], fallback="PikPak")
    hdrs = client.download_headers()
    started = 0
    for f, url in resolved:
        try:
            saved_name = gopeed_saved_name("pk" + share_id, f["path"], f["name"])
            tid = await asyncio.to_thread(
                gopeed.add_task, url, saved_name, GOPEED_DLDIR, hdrs,
                gopeed_connections(f.get("size") or 0))
            map_add(tid, {
                "date": date_str, "share": share_name,
                "orig_name": f["name"], "saved_name": saved_name,
                "rel_path": str(safe_rel_path(f["path"], f["name"])).replace(os.sep, "/"),
                "size": f.get("size") or 0, "added_at": int(time.time()),
                "pwd_id": "", "passcode": "", "fid": f["id"],
                "fid_token": "", "retry": 0, "own_fid": "",
            })
            started += 1
            await asyncio.sleep(WAVE_PACE)
        except Exception as exc:
            log.error("PikPak 建任务失败 %s: %s", f["name"][:30], exc)

    # 视频：直链现成，直接建任务；压缩包等已进队列，这里再放行第一波
    if queued and logged_in:
        try:
            await feed_pikpak(force=True)
        except Exception as exc:
            log.error("PikPak 首波放行失败: %s", exc)

    if not started and not queued:
        await msg.edit_text(
            f"❌ 一个下载任务都没建成\n{SEP}\n\n"
            f"拿到了 {len(resolved)} 个地址，但下载器都没接住，"
            f"请稍后重发一次，或到「⚙️ 设置」看看下载器是否正常")
        return

    lines = []
    if started:
        lines.append(f"✅ 已开始下载 {started} 个文件（直链下载，不占 PikPak 空间）")
    if queued:
        lines.append(f"📦 另有 {queued} 个文件需要转存，已排队")
        lines.append("   PikPak 空间小，会「转存一个 → 下完 → 立刻删」地分批处理")
    lines.append(f"📁 保存到：Aria2Files › {date_str} › {share_name}")
    if not logged_in and missing:
        lines.append(f"\n⚠️ 另有 {len(missing)} 个非视频文件（zip/压缩包等）"
                     f"需要登录 PikPak 才能下，配 PIKPAK_REFRESH_TOKEN 即可")
    if not logged_in and any(not f["name"].lower().endswith(
            (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".ts"))
            for f, _ in resolved):
        lines.append("\nℹ️ 未登录模式下只有视频能直接下")
    await msg.edit_text("\n".join(lines))


async def handle_quark_share(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """夸克分享链接：不转存，直接取直链交给 Gopeed 下载，完成后自动上传 Google Drive"""
    msg = await update.message.reply_text("🔍 正在解析夸克分享链接…")

    try:
        pwd_id, passcode = QuarkShare.parse_link(text)
        q, stoken, files = await _open_share(pwd_id, passcode)
    except Exception as exc:
        log.error("夸克解析失败: %s", exc)
        if is_auth_error(str(exc), "quark"):
            await send_auth_alert("quark", str(exc))
        await msg.edit_text(
            f"❌ 这个链接没解析成功\n{human_error(str(exc))}\n\n"
            f"如果是链接打不开，让对方重新生成一个分享发你")
        return

    # 账号模式下先探一下分享直链能不能用；被夸克拒（412）就改走「转存后下载」
    bridge_mode = False
    if isinstance(q, QuarkShare) and files and QUARK_MODE != "guest":
        if not await _share_direct_for(q, files, pwd_id, stoken):
            bridge_mode = True
            await msg.edit_text(
                "🔍 正在解析夸克分享链接…\n"
                "（夸克不让直接下分享文件，已切换为「先转存再下载」）")

    if not files:
        await msg.edit_text("⚠️ 这个分享里是空的，没有能下载的文件")
        return

    if len(files) > MAX_SHARE_FILES:
        await msg.edit_text(
            f"⚠️ 这个分享的文件太多了\n\n"
            f"一共 {max(len(files), MAX_SHARE_FILES + 1)} 个以上，"
            f"超过单次上限 {MAX_SHARE_FILES} 个\n"
            f"（上限可以用 /etc/tgbot.env 里的 MAX_SHARE_FILES 调整）\n\n"
            f"也可以分批发，或者先把东西整理到几个文件夹里再分享")
        return

    # ---- 提交前自检：先确认「真能下、真能存」，再决定要不要开工 ----
    await msg.edit_text("🩺 正在自检（确认能下才开工）…")
    ok_env, env_rows = await preflight_common()
    verdict, share_rows, downloadable, blocked_n = await preflight_share(
        q, stoken, files, pwd_id)

    header = f"🩺 提交前自检\n{SEP}\n\n"
    body = "\n".join(env_rows) + "\n\n" + "\n".join(share_rows)

    if not ok_env or verdict == "blocked":
        reason = ("运行环境有问题" if not ok_env
                  else "这个分享在当前账号下下不了")
        await msg.edit_text(
            header + body + "\n\n" + SEP + "\n"
            f"⛔ 已暂停，没有开始下载（{reason}）\n\n"
            f"按上面的 ❌ 项处理完，再发一次链接；\n"
            f"也可以直接点下面的按钮重新自检")
        try:
            await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔁 重新自检并开始", callback_data="pre:retry")]]))
            _save_pending_submit(text)
        except Exception:
            pass
        return

    note = ""
    if verdict == "partial":
        note = (f"\n\n⚠️ 其中 {blocked_n} 个文件超过账号下载上限，会被跳过；"
                f"其余 {downloadable} 个正常下")
    await msg.edit_text(
        header + body + note + "\n\n" + SEP + "\n✅ 自检通过，开始下载")

    # 串行化：同一 pwd_id 的提交排队处理，杜绝并发重复建任务
    async with _share_lock(pwd_id):
        share_name, rel_paths = _share_layout(files, pwd_id)

        # 去重：同一分享的同一 fid 已在队列中就不重复下载
        active_keys = {(v.get("pwd_id"), v.get("fid"))
                       for v in load_gopeed_map().values()}
        pending, skipped = [], 0
        seen_fids = set()
        for f, rel in zip(files, rel_paths):
            if f["fid"] in seen_fids:
                skipped += 1
                continue
            seen_fids.add(f["fid"])
            if (pwd_id, f["fid"]) in active_keys:
                skipped += 1
            else:
                pending.append((f, rel))
        if not pending:
            await msg.edit_text(
                f"ℹ️ 这 {skipped} 个文件已经在下载队列里了，不用重复发\n"
                f"发「📋 任务列表」可以看进度")
            return

        need = sum(f["size"] for f, _ in pending)
        usage = await asyncio.to_thread(shutil.disk_usage, str(DOWNLOAD_DIR))
        hard = max(0, usage.free - MIN_FREE_BYTES)

        # 单个文件就超过可用余量的，永远下不了，单独挑出来告知
        oversize = [(f, rel) for f, rel in pending
                    if int(f.get("size") or 0) > hard]
        queueable = [(f, rel) for f, rel in pending
                     if int(f.get("size") or 0) <= hard]

        preview = "\n".join(f"· {f['name']}" for f, _ in pending[:8])
        if len(pending) > 8:
            preview += f"\n… 一共 {len(pending)} 个"

        if not queueable:
            biggest = max((int(f.get("size") or 0) for f, _ in pending), default=0)
            await msg.edit_text(
                f"💾 这批文件一个都放不下\n"
                f"最大的单个文件 {human(biggest)}，服务器现在只剩 {human(usage.free)} 可用\n"
                f"需要至少留出 {human(MIN_FREE_BYTES)} 的安全余量，请先清理一下再发")
            return

        await msg.edit_text(
            f"✅ 找到 {len(pending)} 个文件，共 {human(need)}\n{preview}\n\n"
            f"⏳ 正在安排下载…")

        # 归档目录：北京时间日期 / 分享名（所有波次都进同一个文件夹）
        date_str = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))

        # 全部先入队，再放第一波；后面的波次由 watcher 在空间腾出来后自动续上
        queue_put(pwd_id, share_name, date_str, passcode, [
            {"fid": f["fid"], "name": f["name"],
             "size": int(f.get("size") or 0), "rel": rel,
             "pdir": f.get("pdir", ""),          # 所在目录，刷新 token 时按目录重列
             "fid_token": f.get("share_fid_token", "")}
            for f, rel in queueable])

        stats = await admit_wave(force=True)
        left = len((load_queue().get(pwd_id) or {}).get("items") or [])

        if not stats["admitted"] and not left:
            await msg.edit_text(
                f"❌ 下载任务一个都没建成，下载服务可能挂了\n"
                f"请稍后重发，或发「⚙️ 设置」看看服务状态")
            return

        if stats["admitted"]:
            summary = f"✅ 已开始下载\n本次开始下载 {stats['admitted']} 个"
        else:
            summary = "⏳ 已排队"
        if skipped:
            summary += f"，跳过重复 {skipped} 个"
        if bridge_mode:
            summary += (f"\n♻️ 夸克现在不允许直接下分享文件，"
                        f"已改为「先转存到你的网盘再下载」（下完会自动删掉中转文件）")
        summary += (
            f"\n\n📁 保存到：Aria2Files › {date_str} › {share_name}\n"
            f"下载完会自动上传到 Google Drive\n\n")
        if left:
            left_bytes = sum(int(x.get("size") or 0)
                             for x in (load_queue().get(pwd_id) or {}).get("items", []))
            summary += (
                f"💡 这批一共 {len(queueable) + len(oversize)} 个、{human(need)}，"
                f"磁盘一次放不下\n"
                f"剩下 {left} 个（{human(left_bytes)}）会边传边下、自动接着来，"
                f"最后都会放进同一个文件夹，不用再发一次\n\n")
        if oversize:
            biggest = max(int(f.get("size") or 0) for f, _ in oversize)
            summary += (
                f"⚠️ 另有 {len(oversize)} 个单个文件太大（最大 {human(biggest)}），"
                f"超过服务器可用余量，这次先跳过\n\n")
        summary += "发「📋 任务列表」可以看进度"
        await msg.edit_text(summary)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = (update.message.text or "").strip()

    # 底部键盘
    if text == "📥 新建下载":
        await update.message.reply_text(
            "📥 新建下载\n" + SEP + "\n\n"
            "把下面任意一种发给我就行：\n\n"
            "🔗 夸克网盘分享链接\n"
            "   https://pan.quark.cn/s/xxxxx\n"
            "   带提取码也可以，一起粘过来\n\n"
            "📦 PikPak 分享链接\n"
            "   https://mypikpak.com/s/xxxxx\n"
            "   视频直接下；zip 等非视频需要登录\n\n"
            "🧲 磁力链接\n"
            "   magnet:?xt=urn:btih:...\n\n"
            "🌐 直链地址\n"
            "   https://example.com/file.zip\n\n"
            f"📎 或者直接把文件拖进来（{human(TG_DOWNLOAD_LIMIT)} 以内）"
        )
        return
    if text == "📋 任务列表":
        t, kb = await asyncio.to_thread(view_list, 0)
        await update.message.reply_text(t, reply_markup=kb)
        return
    if text == "☁️ 我的网盘":
        t = await asyncio.to_thread(view_cloud)
        await update.message.reply_text(t, reply_markup=kb_back([
            InlineKeyboardButton("🔗 给最新文件生成分享链接", callback_data="link:latest")], src="cloud"))
        return
    if text == "💾 磁盘空间":
        await _quark_quota("main")
        _bridge_refresh = bridge_account_ready()
        if _bridge_refresh:
            await _quark_quota("bridge")
        await update.message.reply_text(view_disk(), reply_markup=kb_back(src="disk"))
        return
    if text == "⚙️ 设置":
        await update.message.reply_text(view_settings(), reply_markup=kb_back(src="settings"))
        return
    if text == "❓ 帮助":
        await update.message.reply_text(view_help(), reply_markup=kb_back(src="help"))
        return

    # 夸克分享链接
    if "pan.quark.cn/s/" in text:
        await handle_quark_share(update, context, text)
        return

    # PikPak 分享链接（mypikpak.com / .net）
    if "mypikpak.com/s/" in text or "mypikpak.net/s/" in text:
        await handle_pikpak_share(update, context, text)
        return

    # 链接提交
    if not (text.startswith("magnet:") or text.startswith(("http://", "https://"))):
        await update.message.reply_text(
            "没看懂 😅\n\n"
            "你可以发：夸克分享链接、磁力链接、直链地址，或者直接把文件发给我。\n"
            "不知道从哪开始的话，点下面的「❓ 帮助」看看例子。"
        )
        return

    if text.startswith("magnet:") and "xt=urn:btih:" not in text:
        await update.message.reply_text(
            "❌ 这个磁力链接不完整，缺少 xt=urn:btih: 这一段\n"
            "从资源页面上完整复制一次再发我")
        return

    # 磁力 / 直链：先进排队表，再由 feed_aria2 按"并发数 + 磁盘余量"逐批放行
    # 磁力 / 直链：先自检，环境有问题就先别开工
    msg0 = await update.message.reply_text("🩺 正在自检…")
    ok_env, env_rows = await preflight_common()
    if not ok_env:
        report = "\n".join(env_rows)
        await msg0.edit_text(
            f"🩺 提交前自检\n{SEP}\n\n{report}\n\n"
            f"⛔ 已暂停，没有开始下载\n按上面 ❌ 项处理后，把链接再发一次")
        _save_pending_submit(text)
        try:
            await msg0.edit_reply_markup(reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔁 重新自检并开始", callback_data="pre:retry")]]))
        except Exception:
            pass
        return
    await msg0.edit_text("✅ 自检通过，开始下载")

    pos = aria2_queue_add(text)
    res = await feed_aria2(force=True)

    if res["started"]:
        gid = res["gids"][0]
        try:
            t = await asyncio.to_thread(_aria2, "aria2.tellStatus", [gid])
        except Exception:
            t = {"gid": gid, "status": "waiting", "totalLength": "0",
                 "completedLength": "0", "files": [{"path": "新任务"}]}
        await update.message.reply_text(card_task(t), reply_markup=kb_task(t))
    else:
        await update.message.reply_text(
            f"⏳ 已排队，等前面的下完自动开始\n"
            f"🔗 {text[:70]}\n\n"
            f"前面还有 {res['left']} 个在等着（同时最多跑 {ARIA2_MAX_ACTIVE} 个，"
            f"避免把磁盘塞满）\n"
            f"📁 下载完会自动上传到 {drive_label(ARIA2_DEST)}")


# ==================== 额外按钮 ====================

async def on_extra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat.id != ALLOWED_CHAT:
        await q.answer("无权限", show_alert=True)
        return
    await q.answer()
    data = q.data or ""
    try:
        if data == "pre:retry":
            saved = _load_pending_submit()
            if not saved:
                await q.edit_message_text(
                    "⌛ 上次那条提交已经过期了，麻烦把链接/文件重新发一次")
                return
            await q.edit_message_text("🩺 重新自检中…")
            # 复用提交入口，等于再发一次同样的链接
            class _FakeMsg:
                async def reply_text(self, *a, **k):
                    return q.message
            class _FakeUpdate:
                message = _FakeMsg()
                effective_chat = update.effective_chat
            await handle_quark_share(_FakeUpdate(), context, saved)
            return
        if data == "cloud":
            t = await asyncio.to_thread(view_cloud)
            await q.edit_message_text(t, reply_markup=kb_back([
                InlineKeyboardButton("🔗 给最新文件生成分享链接", callback_data="link:latest")], src="cloud"))
        elif data == "help":
            await q.edit_message_text(view_help(), reply_markup=kb_back(src="help"))
        elif data == "link:latest":
            rows = await asyncio.to_thread(_cloud_recent, 1)
            if not rows:
                await q.edit_message_text("⚠️ 网盘里还没有文件，没法生成分享链接",
                                          reply_markup=kb_back())
                return
            _, rel_path, _, remote = rows[0]
            rc, out, err = await asyncio.to_thread(
                _rclone_run, ["link", f"{remote}/{rel_path}"], 40)
            if rc == 0 and out:
                await q.edit_message_text(
                    f"🔗 已生成分享链接\n{SEP}\n\n"
                    f"📄 {os.path.basename(rel_path)[:40]}\n\n"
                    f"{out}\n\n"
                    f"⚠️ 拿到链接的人都能打开，别乱发",
                    reply_markup=kb_back(src="cloud"))
            else:
                await q.edit_message_text(
                    f"⚠️ 分享链接没生成出来\n{human_error(err)}",
                    reply_markup=kb_back(src="cloud"))
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            log.warning("extra edit failed: %s", exc)
    except Exception as exc:
        log.exception("extra failed")


_watcher_task = None
_bot_app = None


async def _cleanup_bridge_leftovers():
    """启动后清理中转目录里的残留副本（上次异常退出时没删掉的）。

    两个账号都要清：主号 + 专用转存号。
    """
    await asyncio.sleep(20)
    alive = {v.get("own_fid") for v in load_gopeed_map().values() if v.get("own_fid")}
    for kind in ("main", "bridge"):
        if kind == "bridge" and not bridge_account_ready():
            continue
        try:
            q = _client_for(kind)
            bridge = await asyncio.to_thread(q.ensure_dir, QUARK_BRIDGE_DIR)
            items = await asyncio.to_thread(q.list_dir, bridge, 200)
            dead = [x["fid"] for x in items
                    if not x.get("dir") and x["fid"] not in alive]
            if dead:
                await asyncio.to_thread(q.delete, dead)
                log.info("清理中转目录残留 %d 个文件（%s 账号）", len(dead), kind)
        except Exception as exc:
            log.warning("清理中转目录失败（%s）: %s", kind, exc)


async def post_init(app):
    _learned_cap.update(main=0, bridge=0)
    global _watcher_task, _bot_app
    _bot_app = app
    _watcher_task = asyncio.create_task(gopeed_watcher())
    asyncio.create_task(_cleanup_bridge_leftovers())


async def post_shutdown(app):
    """优雅关闭 watcher，避免 Task was destroyed 警告"""
    global _watcher_task
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass


def main():
    app = (Application.builder().token(BOT_TOKEN)
           .concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("disk", cmd_disk))
    app.add_handler(CommandHandler("pikpak", cmd_pikpak))
    app.add_handler(CallbackQueryHandler(on_extra, pattern=r"^(cloud|help|link:latest)$"))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO | filters.VOICE,
        handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot 启动，开始轮询")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
