# 常见问题与排查

> 本项目无官方维护。以下内容是作者部署时的实际经验，供自行排查参考。

## 先做这三件事

```bash
systemctl status tgbot                  # 服务在不在
journalctl -u tgbot -n 100 --no-pager   # 最近的日志
python3 healthcheck.py                  # 体检：磁盘/下载器/账号/授权
```

## 错误码对照

| 现象 | 常见原因 | 处理方向 |
|---|---|---|
| `HTTP 412` | 分享直链被 CDN 拒绝 | 改用"先转存后下载"，或隔一段时间再试 |
| `41020 token 校验异常` | 分享的 `fid_token` 过期 | 重扫分享换新 token（代码会自动做） |
| `23018 download file size limit` | 账号对单文件下载有大小上限 | 升级会员，或让对方切成小文件 |
| `21001 file not found` | 中转副本已被删/被清 | 检查是否有并发清理；重新转存 |
| `401 / require login` | Cookie 或 token 失效 | 更新 Cookie / refresh_token |
| 上传卡住、CPU 0% | Google 把连接挂住 | 确认 rclone 带 `--timeout 120s` |

## 常见场景

**任务列表里一直有"下不完"的任务**
aria2 会保留失败/完成记录。本仓库的 `_aria2_janitor()` 会定期清：
完成/移除的立即清，失败的保留 1 小时，零进度 30 分钟强制移除。

**分享解析很慢**
大分享（1000+ 文件）扫描一次要几分钟。系统会缓存解析结果，
所以别频繁重启，重启一次要重扫一遍。

**提示"空间不足"但仍能下**
那可能是网盘的**配额**问题，不是磁盘。看 `healthcheck.py` 输出的两个账号容量。

**PikPak 压缩包下不了**
未登录时只返回视频地址；其它文件需要账号（`pikpak_setup.py --status` 查看状态）。

**PikPak 报 PROHIBITED**
机房 IP 段被屏蔽，需要住宅/代理出口，代码层无解。

## 想自己改

| 想改什么 | 改哪里 |
|---|---|
| 换成别的下载器 | `gopeed_client.py`（实现 `add_task/list_tasks/remove_task`） |
| 加新的网盘来源 | 仿 `quark_share.py`：实现 `get_stoken/walk_share/get_direct_url/download_headers` |
| 换上传目标 | 改 `RCLONE_DEST` / `ARIA2_DEST`，rclone 支持几十种后端 |
| 调分批节奏 | `MAX_WAVE_FILES` / `WAVE_PACE` / `FEED_MIN_INTERVAL` |
