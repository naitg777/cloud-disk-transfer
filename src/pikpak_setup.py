#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置 PikPak 登录（为了能下压缩包）。

用法（root）：
    python3 /opt/cloud-disk-transfer/pikpak_setup.py '<refresh_token>'
    python3 /opt/cloud-disk-transfer/pikpak_setup.py --status

它会把 token 存到 /opt/cloud-disk-transfer/logs/pikpak_token.json（0600，归 tgbot），
并重启机器人。PikPak 的 refresh_token 会轮换，所以后续以这个文件为准。
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pikpak_share import PikPakError, PikPakShare  # noqa: E402

TOKEN_FILE = "/opt/cloud-disk-transfer/logs/pikpak_token.json"


def read_token() -> str:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            return (json.load(fh).get("refresh_token") or "").strip()
    except Exception:
        return ""


def save_token(tok: str) -> None:
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        json.dump({"refresh_token": tok, "ts": int(time.time())}, fh)
    os.chmod(TOKEN_FILE, 0o600)
    subprocess.run(["chown", "tgbot:tgbot", TOKEN_FILE], check=False)


def status() -> int:
    tok = read_token()
    if not tok:
        print("❌ 还没配置 PikPak 登录（当前只能下视频，压缩包不行）")
        return 1
    try:
        cli = PikPakShare(refresh_token=tok)
        if not cli.ensure_login():
            raise PikPakError("token 为空")
        print("✅ 已登录  user_id =", cli.user_id or "(未知)")
        if cli.refresh_token and cli.refresh_token != tok:
            save_token(cli.refresh_token)
            print("   （refresh_token 已轮换，最新值已保存）")
        return 0
    except Exception as exc:
        print("❌ 登录失效：", str(exc)[:160])
        print("   重新拿一个 refresh_token 再执行本脚本")
        return 1


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("--status", "-s"):
        return status()
    tok = args[0].strip().strip('"').strip("'")
    if len(tok) < 20:
        print("❌ 这个 token 看着不对（太短）")
        return 1
    print("正在验证 token …")
    try:
        cli = PikPakShare(refresh_token=tok)
        if not cli.ensure_login():
            raise PikPakError("token 无效")
    except Exception as exc:
        print("❌ 验证失败：", str(exc)[:200])
        return 1
    save_token(cli.refresh_token or tok)
    print("✅ 验证通过  user_id =", cli.user_id or "(未知)")
    print("   已保存到", TOKEN_FILE)
    print("正在重启机器人 …")
    subprocess.run(["systemctl", "restart", "tgbot"], check=False)
    print("✅ 完成。之后发「/pikpak」可以查看状态；压缩包会自动转存后下载。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
