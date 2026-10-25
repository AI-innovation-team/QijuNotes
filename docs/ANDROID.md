# 安卓接入与私网通知

起居提供浏览器界面，并支持 Tailscale 私网内自托管 ntfy 通知。网页账号用于登录起居，ntfy 的手机只读账号用于订阅通知，两者分别管理。本文中的 `<TAILSCALE_IP>` 必须替换为部署主机实际地址，不是可直接打开的入口。

## 手机网页登录

1. 在 Linux 主机的 [本机入口](http://127.0.0.1:18780/) 创建账号。
2. 手机与主机接入同一获授权的 Tailscale 网络。
3. 打开 `http://<TAILSCALE_IP>:18780/`，使用同一账号密码登录，可选择保持登录 30 天。

只有配置了 `--private-host` 的应用才会提供上述私网监听。HTTP 流量在 Tailscale 隧道内传送，浏览器仍显示 HTTP；该配置不能直接照搬到公网。若自行配置 HTTPS 入口，核对实际地址和认证后再更新 `public_url`，参见 [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)。

## 部署通知服务

固定 ntfy 版本和校验值在 [runtime/ntfy-version.json](../runtime/ntfy-version.json)。在项目根目录执行，将参数换为本机实际 Tailscale IP：

```bash
QIJU_PRIVATE_IP='填写主机的实际 Tailscale IP'
python3 scripts/prepare_ntfy.py --listen-host "$QIJU_PRIVATE_IP"
```

准备脚本创建项目独立目录、认证和用户服务文件；不启动服务，不改应用配置，拒绝覆盖非空目标或既有凭据。可通过 `--archive /absolute/path/to/archive.tar.gz` 复用下载包，仍会核对固定校验值。

| 内容 | 默认位置 |
| --- | --- |
| ntfy 二进制与通知缓存 | `~/.local/share/dsh-personal-ntfy/` |
| 配置与认证数据库 | `~/.config/dsh-personal/ntfy/` |
| 服务端发布令牌 | `~/.config/dsh-personal/ntfy/publisher.token` |
| 手机只读账号 | `~/.config/dsh-personal/ntfy/phone.json` |
| 用户服务 | `~/.config/systemd/user/dsh-personal-notify.service` |

凭据目录为 0700，文件为 0600。只在自己的终端或安全编辑器中查看手机凭据，不把它们贴到聊天、仓库或截图。手机不能使用服务端发布令牌。

核对生成文件后启动：

```bash
systemctl --user daemon-reload
systemctl --user enable --now dsh-personal-notify.service
systemctl --user status dsh-personal-notify.service --no-pager
```

将脚本输出的 `notification` 对象合并到应用 `config.json`，保留其他字段，并把 `public_url` 设置为手机实际可达的起居入口。配置字段说明见 [CONFIGURATION.md](CONFIGURATION.md)。服务默认拒绝匿名访问，发布账号只能写指定主题，手机账号只能读指定主题。

## 手机订阅

安装 [ntfy 安卓应用](https://f-droid.org/packages/io.heckel.ntfy/)，添加自托管服务器 `http://<TAILSCALE_IP>:18783` 和准备脚本配置的主题（默认 `dsh-activities`），使用 `phone.json` 中的只读账号登录。主题拼写必须完全匹配；“无权访问”也可能来自密码或访问控制，不能只据提示断定原因。

允许 ntfy 显示通知，并允许 ntfy 与 Tailscale 后台运行。私服即时订阅依赖应用及 VPN 持续运行，被系统强制停止后无法保证及时送达。不同厂商的省电设置需要分别检查，见 [HarmonyOS](HARMONYOS.md) 和 [ntfy 手机订阅说明](https://docs.ntfy.sh/subscribe/phone/)。

若改用其他来源的 APK，先核对发布方、固定版本和校验值；不同商店可能使用不同签名，不要为覆盖安装而盲目卸载现有客户端。仓库不包含私人下载链接或用户设备的安装包。

## 验证与回执

先确认主题可读取，再在手机允许通知、回主屏并锁屏后发送明确标注的测试。分别记录锁屏是否显示、点击是否打开正确活动，以及断网后恢复情况。主机 HTTP 成功和 ntfy 接受请求不能证明设备已送达。

活动通知只包含活动类型、就绪提示和链接，不包含睡眠、反思或情绪正文。效果 ID 映射为 ntfy `sequence_id`，服务端消息 ID 和状态被保存；未知响应不盲目重发。每日首次通知、零次自动追催、23:00–08:00 免打扰、前台明确展示抑制和历史补记静默仍由既有 worker 管理。

通知缓存保留最少信息 24 小时。日程提醒和活动通知应分别验证；一次锁屏成功不代表长期后台连接已经可靠。`tests/test_notifications.py` 的实际 ntfy 集成测试需要固定包，缺包跳过不能记作通过。
