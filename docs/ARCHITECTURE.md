# 架构说明

## 一句话

`Telegram 输入 → 解析直链 → 下载器落盘 → rclone 上传到 Google Drive → 删本地`。

```
        ┌─────────────┐
用户 ──▶│  Telegram   │
        │    Bot      │
        └──────┬──────┘
               │ 链接 / 文件
        ┌──────▼──────────────────────────────┐
        │  提交前自检 preflight                │
        │  磁盘 · 下载器 · 网盘授权 · 账号     │
        └──────┬──────────────────────────────┘
               │ 通过才继续
     ┌─────────▼──────────┐   ┌──────────────────┐
     │ 分享解析            │   │ 磁力 / 直链       │
     │ quark_share.py     │   │ aria2 队列        │
     │ pikpak_share.py    │   └─────────┬────────┘
     └─────────┬──────────┘             │
               │ 直链 + 请求头            │
     ┌─────────▼──────────────────────────▼──────┐
     │ 下载器（Gopeed / aria2）→ 本地临时目录      │
     └─────────┬──────────────────────────────────┘
               │ 完成后
     ┌─────────▼──────────┐
     │ 归档 + rclone move │──▶ Google Drive
     └─────────┬──────────┘
               │
        删本地 / 删中转副本
```

## 目录

| 文件 | 作用 |
|---|---|
| `src/tgbot.py` | 主程序：Bot 交互、任务队列、watcher、上传管线、自检 |
| `src/quark_share.py` | 夸克分享解析（两种会话：账号 / 公开分享） |
| `src/pikpak_share.py` | PikPak 解析（含 captcha 签名、转存、删副本） |
| `src/gopeed_client.py` | Gopeed HTTP API 封装 |
| `src/on-complete.sh` | aria2 下载完成钩子（rclone 上传） |
| `src/send-alert.sh` | aria2 失败汇总推送 |
| `src/healthcheck.py` | 每日体检 |
| `src/cleanup_leftovers.py` | 残留清理 |
| `src/pikpak_setup.py` | PikPak 登录配置工具 |
| `deploy/systemd/` | 服务与定时器示例 |
| `tests/` | 16 个套件，全部离线桩测试 |

## 关键设计（也是最值得复用的部分）

### 1. 分批放行（wave）

大分享动辄几千个文件，不可能一次全建任务。做法：

- 按「磁盘剩余 × 60%」和「≤N 个/波」切波；
- 每波只建 N 个任务，下载完 → 上传完 → 本地删除，再放下一波；
- 队列落盘（`gopeed_queue.json`），重启不丢。

### 2. 空间感知降级

网盘空间不够时代码不允许"硬塞"：

- 剩余空间 < 阈值 → 不转存，改用不需要空间的通道（公开分享直链）；
- 单个文件大于可用空间 → 留在队列并提示，不失败；
- 中转副本**下载完成就删**，而不是等上传完。

### 3. 熔断（circuit breaker）

120 秒内连续 3 个任务失败 → 判定为"被对方限流"，整体暂停 10 分钟，
期间**不消耗**每个文件的重试次数。避免"7 分钟把 300 个文件的重试额度烧光"。

### 4. 文件级冷却而不是丢弃

某个文件暂时下不了（链接过期/被限制）→ 打 `skip_until` 时间戳跳过，
到点自动重试。**永不从队列删除条目**（删了就是用户数据丢失）。

### 5. 提交前自检（preflight）

见 `src/tgbot.py` 的 `preflight_common()` / `preflight_share()`：
环境层检查 + 抽 1 小 1 大文件实测，全绿才开工。

## 数据文件

| 文件 | 内容 | 备注 |
|---|---|---|
| `logs/gopeed_tasks.json` | 在途任务映射 | 每 5 秒读写，原子替换 |
| `logs/gopeed_queue.json` | 待放行队列 | 全部放行后自动删除 |
| `logs/aria2_queue.json` | 磁力/直链排队 | — |
| `logs/pikpak_queue.json` | PikPak 待转存 | 空间小，转一个删一个 |
| `logs/pikpak_token.json` | PikPak refresh_token | 0600；token 会轮换，自动回写 |
| `logs/alert_state.json` | 告警冷却 | — |

## 为什么很多地方写得"啰嗦"

因为踩过坑，每条注释背后基本是一次线上故障：

- 上传必须带 `--timeout/--contimeout`，否则 Google 会挂住连接，任务永远"进行中"；
- 解析分享时 `fid_token` 会过期，重试必须先重扫分享换新 token；
- PikPak 的 CDN 会回 `Content-Disposition: filename`，下载器按它改名，
  所以"找文件"要按原名或大小匹配，不能只认自己生成的文件名。
