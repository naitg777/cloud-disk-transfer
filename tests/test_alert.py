# -*- coding: utf-8 -*-
"""验证：夸克报错不会被误判成「授权掉了」。"""
import os
import sys
from pathlib import Path

os.environ.update({
    "BOT_TOKEN": "T", "ALLOWED_CHAT": "42", "ARIA2_SECRET": "T",
    "DOWNLOAD_DIR": "/tmp/x/tgdl", "GOPEED_DLDIR": "/tmp/x/gdl",
    "GOPEED_MAP": "/tmp/x/map.json", "GOPEED_QUEUE": "/tmp/x/q.json",
    "ALERT_STATE": "/tmp/x/a.json", "RCLONE_CONF": "/tmp/x/r.json",
    "QUARK_BRIDGE_COOKIE_FILE": "/tmp/x/none.cookie",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import tgbot  # noqa: E402

FAILS, N = [], [0]


def check(cond, label):
    N[0] += 1
    print((" ✓ " if cond else " ✗ ") + label)
    if not cond:
        FAILS.append(label)


# 不该报警的（今天的误报源：十六进制 token 里含 401 / 文案含 “登录”）
check(not tgbot.is_auth_error(
    "转存文件token校验异常[9540109f99c74e38ba6539421c3155f6 token校验异常]（code=41020）", "quark"),
    "41020 + 含401的token 不报授权")
check(not tgbot.is_auth_error("token 401abc def", "quark"), "裸 401 片段不报授权")
check(not tgbot.is_auth_error("download file size limit（code=23018）", "quark"),
    "23018 不报授权")
check(not tgbot.is_auth_error("file not found（code=21001）", "quark"),
    "21001 不报授权")

# 该报警的
check(tgbot.is_auth_error("require login please", "quark"), "require login 要报")
check(tgbot.is_auth_error("HTTP 401: code=401", "quark"), "code=401 要报")
check(tgbot.is_auth_error("用户未登录", "quark"), "未登录 要报")
check(tgbot.is_auth_error("31001 token 无效", "quark"), "31001 要报")
check(tgbot.is_auth_error("invalid_grant", "gdrive"), "gdrive invalid_grant 要报")
check(not tgbot.is_auth_error("connection reset by peer", "gdrive"),
    "gdrive 网络错误不报")

print(f"checks={N[0]} failed={len(FAILS)}")
sys.exit(1 if FAILS else 0)
