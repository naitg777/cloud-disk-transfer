#!/bin/bash
set -u

FAILED="/opt/cloud-disk-transfer/logs/failed.log"
ENVF="/etc/tgbot.env"
LOCK="/tmp/aria2-alert.lock"

[ -s "$FAILED" ] || exit 0

exec 9>"$LOCK"
flock -n 9 || exit 0

TOKEN=$(grep -m1 '^BOT_TOKEN=' "$ENVF" | cut -d= -f2-)
CHAT=$(grep -m1 '^ALLOWED_CHAT=' "$ENVF" | cut -d= -f2-)
if [ -z "${TOKEN:-}" ] || [ -z "${CHAT:-}" ]; then
    logger -t aria2-alert "缺少 BOT_TOKEN 或 ALLOWED_CHAT，跳过告警"
    exit 1
fi

COUNT=$(wc -l < "$FAILED")
DETAIL=$(head -n 5 "$FAILED" | awk -F'\t' '{printf "📄 文件：%s\n   时间：%s\n   原因：%s\n\n", $3, $1, $4}')
[ "$COUNT" -gt 5 ] && DETAIL="${DETAIL}…还有 $((COUNT-5)) 个文件同样失败\n"

FREE=$(df -h /data | awk 'NR==2{print $4}')

BODY="⚠️ 有 $COUNT 个文件没传上 Google Drive

${DETAIL}文件都还在服务器上，没有丢。想手动补传的话，在服务器执行：
docker exec -u 1000:1000 aria2-pro /scripts/bin/rclone --config /scripts/rclone.conf move <文件路径> gdrive:Aria2Files

服务器剩余空间：${FREE}"

RESP=$(curl -s -m 20 -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT}" \
    --data-urlencode "text=${BODY}" -w '\n%{http_code}')

CODE=$(printf '%s' "$RESP" | tail -n1)
if [ "$CODE" = "200" ]; then
    : > "$FAILED"
    logger -t aria2-alert "已推送 ${COUNT} 条失败记录"
else
    logger -t aria2-alert "推送失败，HTTP ${CODE}，保留记录待下次重试"
    exit 1
fi
