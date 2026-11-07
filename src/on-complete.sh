#!/bin/sh
# aria2 on-download-complete 钩子：下载完成后自动上传 Google Drive
# 参数：$1=GID  $2=文件数  $3=第一个文件的完整路径
# 由 aria2 以 PUID:PGID(1000:1000) 身份调用，容器内路径 /scripts = 宿主 /opt/cloud-disk-transfer
#
# v3：
#   - 单文件任务保留相对目录结构，如 /downloads/2026-11-01/4-工具类软件/a.apk
#     会上传到 gdrive:Aria2Files/2026-11-01/4-工具类软件/a.apk
#   - 多文件任务（BT 等）仍上传顶层目录
#   - 夸克临时文件改由 cleanup_uploaded.py 定时清理（按文件名+大小匹配，更可靠）

set -u

GID="${1:-}"
NUM="${2:-0}"
FIRST="${3:-}"

RCLONE="/scripts/bin/rclone"
CONF="/scripts/rclone.conf"
DEST="gdrive:Aria2Files"
LOG="/scripts/logs/rclone.log"
FAILED="/scripts/logs/failed.log"
BASE="/downloads"

mkdir -p /scripts/logs 2>/dev/null

log()  { printf '%s [%s] %s\n' "$(date '+%F %T')" "$GID" "$*" >> "$LOG"; }
fail() { printf '%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$GID" "$1" "$2" >> "$FAILED"; log "$3"; }

[ -z "$FIRST" ] && { log "跳过：未取到文件路径"; exit 0; }
[ -x "$RCLONE" ] || { fail "$FIRST" "rclone 不存在或不可执行" "错误：rclone 不存在或不可执行 ($RCLONE)"; exit 1; }
[ -r "$CONF" ]   || { fail "$FIRST" "rclone.conf 不可读" "错误：rclone.conf 不可读 ($CONF)"; exit 1; }

# 计算上传目标：单文件保留目录结构，多文件保留顶层目录
REL="${FIRST#"$BASE"/}"
TOP="${REL%%/*}"

if [ "$NUM" -gt 1 ]; then
    if [ "$TOP" != "$REL" ]; then
        ITEM="$BASE/$TOP"
        # rclone move 只搬目录内容，不会创建顶层目录本身，必须显式补上
        REL_DIR="$TOP"
    else
        # 修复：多文件平铺在 /downloads 根目录时，通过 aria2 RPC 查出全部文件逐个上传
        SECRET="${RPC_SECRET:-$(grep -m1 '^rpc-secret=' /config/aria2.conf 2>/dev/null | cut -d= -f2-)}"
        RPC_FILES=""
        if [ -n "$SECRET" ] && [ -x /usr/bin/curl ] && [ -x /usr/bin/jq ]; then
            RPC_FILES=$(/usr/bin/curl -s http://127.0.0.1:6800/jsonrpc -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"aria2.getFiles\",\"params\":[\"token:$SECRET\",\"$GID\"]}" 2>/dev/null | /usr/bin/jq -r '.result[].path' 2>/dev/null)
        fi

        if [ -n "$RPC_FILES" ]; then
            log "平铺多文件任务（$NUM 个），通过 RPC 枚举全部文件逐个上传 -> $DEST"
            FAIL_ANY=0
            # 用 here-doc 而不是管道：管道里的 while 跑在子 shell 里，
            # FAIL_ANY 的改动带不回父 shell，会导致失败也退出 0。
            while IFS= read -r f; do
                [ -f "$f" ] || continue
                log "上传平铺文件: $f -> $DEST"
                if ! "$RCLONE" --config "$CONF" move "$f" "$DEST" \
                        --transfers 4 --retries 3 --exclude "*.aria2" \
                        --log-file "$LOG" --log-level INFO; then
                    fail "$f" "平铺文件上传失败" "上传失败: $f"
                    FAIL_ANY=1
                fi
            done <<EOF
$RPC_FILES
EOF
            [ "$FAIL_ANY" -eq 0 ] && exit 0
            exit 1
        fi

        ITEM="$FIRST"
        REL_DIR="."
    fi
else
    ITEM="$FIRST"
    REL_DIR=$(dirname "$REL")
fi

if [ "$REL_DIR" != "." ] && [ -n "$REL_DIR" ]; then
    DEST="$DEST/$REL_DIR"
fi

log "开始上传: $ITEM -> $DEST"

# 捕获 rclone 输出，失败时取最后 3 行作为原因
OUT=$("$RCLONE" --config "$CONF" move "$ITEM" "$DEST" \
        --transfers 4 \
        --checkers 8 \
        --buffer-size 16M \
        --retries 3 \
        --low-level-retries 10 \
        --exclude "*.aria2" \
        --log-file "$LOG" \
        --log-level INFO 2>&1)
RC=$?

if [ "$RC" -eq 0 ]; then
    log "上传成功: $ITEM -> $DEST"
else
    REASON=$(printf '%s\n' "$OUT" | grep -v '^$' | tail -n 3 | tr '\n' ' ' | cut -c1-200)
    [ -z "$REASON" ] && REASON="rclone 退出码 $RC，无输出"
    fail "$ITEM" "$REASON" "上传失败: $ITEM（本地保留）"
    exit 1
fi
