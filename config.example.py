# -*- coding: utf-8 -*-
"""配置参考：所有可配置项都在这里。

实际部署时有两种方式：
  1) 环境变量（推荐，systemd 用 EnvironmentFile 读取 .env.example）
  2) 把本文件复制成 config.py，由 systemd 的 EnvironmentFile 或 shell 导出

任何一项都不要硬编码进源码，也不要把填好的文件提交到仓库。
"""

CONFIG = {
    # ===== 必填 =====
    "BOT_TOKEN": "123456:ABC-DEF_your_telegram_bot_token",   # @BotFather 申请
    "ALLOWED_CHAT": "123456789",      # 只服务这一个会话（你的 Telegram chat id）
    "ARIA2_SECRET": "your_aria2_rpc_secret",                # aria2.conf 里的 rpc-secret

    # ===== 路径（按发行版自行调整） =====
    "DOWNLOAD_DIR": "/data/tg_downloads",       # Telegram 文件落地
    "ARIA2_DLDIR": "/data/aria2/downloads",     # aria2 视角的下载目录
    "GOPEED_DLDIR": "/data/gopeed/downloads",   # Gopeed 下载目录
    "GOPEED_API": "http://127.0.0.1:9999",
    "ARIA2_RPC": "http://127.0.0.1:6800/jsonrpc",
    "RCLONE_BIN": "/usr/bin/rclone",
    "RCLONE_CONF": "/opt/cloud-disk-transfer/rclone.conf",
    "FAILED_LOG": "/opt/cloud-disk-transfer/logs/failed.log",
    "GOPEED_MAP": "/opt/cloud-disk-transfer/logs/gopeed_tasks.json",

    # ===== 网盘目标 =====
    "RCLONE_DEST": "gdrive:TelegramFiles",      # Telegram 文件上传到哪
    "ARIA2_DEST": "gdrive:Aria2Files",          # 链接/分享上传到哪

    # ===== 夸克（可选：不填则只用公开分享会话） =====
    # Cookie 从 OpenList 的 sqlite 里读；也可改成你自己的读取方式
    "QUARK_DB": "/data/openlist/data/data.db",
    "QUARK_MODE": "auto",               # auto / guest / account / bridge
    "QUARK_BRIDGE_COOKIE_FILE": "/opt/cloud-disk-transfer/quark_bridge.cookie",
    "QUARK_BRIDGE_DIR": "tg_transfer_bridge",

    # ===== PikPak（可选：不填则只能下视频） =====
    "PIKPAK_REFRESH_TOKEN": "",         # 或用 pikpak_setup.py 写入 token 文件
    "PIKPAK_TOKEN_FILE": "/opt/cloud-disk-transfer/logs/pikpak_token.json",

    # ===== 调优（不填用默认值） =====
    "MAX_SHARE_FILES": "20000",         # 单分享最多文件数
    "MAX_WAVE_FILES": "40",             # 每波最多建几个任务
    "WAVE_PACE": "0.2",                 # 建任务间隔（秒）
    "FEED_MIN_INTERVAL": "10",          # 两波最小间隔（秒）
    "UPLOAD_CONCURRENCY": "3",          # 同时上传几个
    "ARIA2_MAX_ACTIVE": "2",            # 磁力/直链并发
    "LEFTOVER_KEEP_DAYS": "7",          # 失败残留保留天数
    "QUARK_MIN_FREE_MB": "300",         # 夸克剩余低于此值不转存
    "GUEST_MAX_MB": "45",               # 公开通道单文件上限
    "BRIDGE_ACC_MAX_MB": "45",          # 转存号（非会员）单文件上限
}
