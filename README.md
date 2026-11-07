> [!WARNING]
> ## ⚠️ 本项目无官方维护，请先读这段
>
> - **本项目为个人一次性开源 Demo**，作者**不提供长期维护、不跟进功能迭代、不修复运行故障**。
> - 项目依赖第三方云盘的**非官方接口**，云厂商随时可能修改接口、调整鉴权规则；由此导致的功能失效，**作者不会跟进适配**。
> - 本项目**仅用于技术学习交流，不建议直接用于生产环境**；自行部署使用的所有风险由使用者自行承担。
> - 遇到 bug 或 API 失效问题，**请自行 Fork 仓库修改适配**，作者不处理相关求助、不回复 Issue、不合并 PR。

# cloud-disk-transfer

一个跑在服务器上的中转下载工具：把网盘分享链接解析成直链、交给下载器下载，再自动上传到 Google Drive。通过 Telegram Bot 操作。

## 功能

- **多来源**：网盘分享链接（夸克 / PikPak）、磁力链接、HTTP 直链、Telegram 文件
- **形态简单**：解析出下载地址后交给下载器，落盘即上传，不长期占用本机磁盘
- **分批 + 空间感知**：按磁盘/网盘剩余空间分批放行，装不下就等；文件级冷却而不是丢弃
- **提交前自检**：开工前先验证下载器、磁盘、网盘授权、账号状态，有问题直接拦住并给修复建议
- **失败兜底**：熔断（连续失败暂停）、自动重试、残留清理

## 环境要求

- Linux（Debian 12 上开发测试），Python 3.11+
- [Gopeed](https://gopeed.com) 1.9.3+（HTTP/磁力下载器，监听 `127.0.0.1:9999`）
- [aria2](https://aria2.github.io/) 1.36+（磁力/直链链路，RPC `127.0.0.1:6800`）
- [rclone](https://rclone.org/) v1.60+（上传到 Google Drive）
- 一个 Telegram Bot Token（找 @BotFather），以及你自己的 chat id

## 部署（极简）

```bash
# 1. 放代码
sudo mkdir -p /opt/cloud-disk-transfer
sudo cp -r src/* /opt/cloud-disk-transfer/
sudo python3 -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

# 2. 配置（把所有占位符换成你自己的）
sudo cp config.example.py /opt/cloud-disk-transfer/config.py
sudo cp .env.example /etc/cloud-disk-transfer.env        # 填 BOT_TOKEN 等
sudo chmod 600 /etc/cloud-disk-transfer.env

# 3. rclone 授权（会给 Google Drive）
rclone config      # 建一个名为 gdrive 的 remote
sudo cp rclone.conf /opt/cloud-disk-transfer/rclone.conf

# 4. 服务
sudo cp deploy/systemd/tgbot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tgbot
```

跑起来后，在 Telegram 里给 Bot 发 `/start`，按菜单操作。

> 详细变量说明见 `config.example.py`；架构见 `docs/ARCHITECTURE.md`；报错处理见 `docs/TROUBLESHOOTING.md`。

## 测试

```bash
cd tests
python -m pytest -q          # 或逐个 python test_xxx.py
```

测试全部使用桩对象，**不联网、不碰真实账号**（需要真实网络的脚本未包含在本仓库）。

## 已知限制

| 限制 | 说明 |
|---|---|
| 第三方接口随时失效 | 夸克/PikPak 的接口、鉴权、限速规则都非公开，厂商一改就失效 |
| 账号限制 | 未付费网盘账号对"自己网盘直链"有单文件大小上限，超出无法下载 |
| 机房 IP | 部分服务会屏蔽 IDC 网段，需要住宅出口，代码层无解 |
| 不做网页 UI | 只有 Telegram Bot 交互 |

## 社区接手

欢迎任何人 Fork 本仓库自行维护、修改、二次发布，**无需征得作者同意**。
作者不审核、不合并任何外部 Pull Request；有优化请在自己的 Fork 里发布。

## 许可证

本项目基于 **MIT** 协议开源，详见 [LICENSE](LICENSE)。
