# 配置与部署

以下命令在项目根目录执行。先按仓库 README 安装固定运行时依赖，再配置单独的实例数据目录。产品参数文件描述业务基线，不是可以整份导入 DSH 的原生配置。

## 路径与入口

| 项目 | 默认位置 |
| --- | --- |
| 配置 | `~/.local/share/dsh-personal/config.json` |
| 业务数据库 | `~/.local/share/dsh-personal/personal.sqlite` |
| DSH 原生数据 | `~/.local/share/dsh-personal/dsh/` |
| 本机网页 | `http://127.0.0.1:18780/` |
| 原生宿主 / 模型网关 | 回环端口 18781 / 18782 |
| 账号文件 | 数据根的 `account-auth.json` |
| 凭据目录 | `~/.config/dsh-personal/` |

自定义启动可使用 `python3 -m dsh_personal.server --data /absolute/path/to/data`；`--private-host` 增加指定私网地址监听，`--web-root` 指定静态资产目录。不要把内部宿主和模型网关当作手机入口。

项目用户服务安装器 `python3 scripts/install-service.py` 使用上述默认数据目录，生成并启动应用、原生宿主、备份和独立账本单元。它会写入同名项目服务配置；已有定制实例应先核对配置，不能把重复安装当作无副作用检查。需要用户级 systemd 会话；登录退出后的运行与机器重启行为由部署环境另外验证。

```bash
bash scripts/control.sh status
bash scripts/control.sh logs
```

`start`、`stop`、`restart` 操作本项目单元；重启前等实际模型调用结束。控制脚本不会为未安装的通知服务创建配置。

## 账号

首次在 Linux 主机浏览器打开本机入口创建账号。此后其他设备使用相同账号密码，首次设置不能由转发或手机连接完成。保持登录、改密和本机重设见 [ACCOUNTS.md](ACCOUNTS.md)。

## 模型

将下列片段合并到现有 `config.json`，保留其他有效字段。示例路径里的 `USER` 必须替换为实际系统账号；JSON 不自动展开 `~` 或环境变量。

```json
{
  "model": {
    "base_url": "https://api.deepseek.com",
    "name": "deepseek-flash",
    "api_key_file": "/home/USER/.config/dsh-personal/model.key",
    "max_tokens": 8192,
    "thinking": "disabled"
  }
}
```

这是当前适配的非思考模式示例，模型标识须由供应商实际支持。`base_url` 使用 API 根地址，网关追加 `/chat/completions`；不要重复填后缀。上限取请求、配置和 8192 的最小值。凭据文件只包含密钥，放在源码和数据根以外；父目录 0700、文件 0600。不要把密钥写进命令参数或仓库。

```bash
chmod 700 "$HOME/.config/dsh-personal"
chmod 600 "$HOME/.config/dsh-personal/model.key"
python3 scripts/readiness.py
```

`readiness.py` 为只读检查。`scripts/verify-provider.py --output /absolute/path/to/private-result.json` 会发出一个上限 32 token 的合成请求，产生真实调用费用并绑定当时配置；连通性不代替课程内容质量。更换模型、地址或凭据后重新验证。模型配置热读取，修改原生 provider 适配需重启项目服务。

网关记录结算与预留、供应商数值 usage、实际输出上限和请求时刻，不在 usage 回执中保存正文或密钥。最多两个实际模型请求；默认请求期限 300 秒，重定向被拒绝。系统 DNS 阻塞不能被 socket 计时器直接打断，返回后过期请求不会继续发送。

新实例后台默认限额为每日 60000、每月 1000000 token。受控元信息 `background_limit_enforced` 只有字面值 `false` 才停用本地限额，原配置和用量仍保留；不存在的 `config.json` 预算字段不会生效。变更需有明确依据并通过项目维护流程核对，不直接修改运行中数据库。本地关闭限额不能解除供应商余额、频率或并发限制。

## 通知与私网入口

按 [ANDROID.md](ANDROID.md) 准备 ntfy 后，将输出合并到配置。下列占位符均需替换：

```json
{
  "public_url": "http://<TAILSCALE_IP>:18780",
  "notification": {
    "backend": "ntfy",
    "url": "http://<TAILSCALE_IP>:18783",
    "topic": "dsh-activities",
    "token_file": "/home/USER/.config/dsh-personal/ntfy/publisher.token"
  }
}
```

`public_url` 是手机实际可达的网页地址，填写 URL 不会创建网络入口。ntfy 地址为服务根，主题单独设置。发布凭据为服务账户持有的绝对路径普通文件，权限 0600，不使用符号链接。手机只读凭据与发布凭据分离。适配器只允许受支持的 HTTPS 或私网/回环 HTTP 地址，并拒绝重定向。

服务端接受通知不代表手机收到。默认每日首次通知、零次自动追催、23:00–08:00 免打扰和前台明确展示抑制；历史补记静默。通用 `webhook` 后端兼容发送 `title`、`message`、`url`、`idempotency_key`，不支持任意认证头或设备回执。

## 日常安排、偏好与研读

A1 每日一学默认 21:00，A2 每日反思 21:30，A3 睡眠打卡 08:30，业务时区为 Asia/Shanghai。只由明确用户操作调整时间、频率或暂停；服务故障和未回复不改变日程。不要另注册第二份 A1–A3 cron 或 DSH routines。

长期偏好可在聊天中保存到 `global`、`routine:A1`、`routine:A2`、`routine:A3`，以实际设置回执为准；已有任务可修订目标、输入、交付内容和验收条件。设置不会自行修改源码、权限或凭据。分类学习主题、授课要求和反思方面有独立版本化设置；选题影响已取得资料池的实际选材，未匹配原文会显示缺口。任意专题联网检索仍未贯通。三类打卡时间可在侧栏独立设置，默认保持每日。用法见 [USER_GUIDE.md](USER_GUIDE.md)。

夜间研读来源登记于 `runtime/study_sources.json`。新实例策略默认关闭；维护者根据明确要求启用持久策略后，在 01:00–07:00 上海时间、每 30 分钟检查，前台任务优先。只能对取得的正文形成笔记，目录和视频元数据不算已读。它是来源与笔记积累，不更新供应商模型权重。策略和来源由受控维护流程管理，当前没有通用聊天开启任意抓取的能力。

## 备份

安装器建立每日 03:30（Asia/Shanghai）加密备份和每分钟独立账本定时器。备份密钥默认 `~/.config/dsh-personal/backup.key`，与数据目录分离。密钥副本应保存在独立安全位置；账号与模型/通知凭据不进入备份。

`backup.directory`、`backup.ledger_directory` 可指定本地目标；`backup.offsite_dir` 需指向已挂载的异机目录，并配置 `backup.offsite_mount` 的 `mountpoint`、`source`、`fstype` 以核对挂载。第二条本机路径不是异机保护。实际配置方法、恢复隔离和核对步骤见 [RECOVERY.md](RECOVERY.md)。

## 静态网页发布

`scripts/publish-web.py` 从 Git 提交读取 `web/`，生成按完整提交命名的静态快照，已有目录须逐文件一致。省略 `--activate` 只生成或核对资产，不改变服务。

```bash
QIJU_RELEASE_REF='填写已审查的提交或标签'
python3 scripts/publish-web.py --commit "$QIJU_RELEASE_REF"
```

`--activate` 要求提交解析到当前 HEAD，且已存在 `~/.config/systemd/user/dsh-personal.service.d/20-web-release.conf`。它不初始化缺失的 drop-in，只替换其中的 `--web-root` 并重启应用服务。首次部署必须先根据实际 `ExecStart` 建立这一覆盖配置；普通 `control.sh restart` 不会切换静态快照。

该流程只固定网页资产，后端仍从服务工作目录加载。发布需检查工作区、保护数据、等待运行中调用结束，并分别核对实际进程、认证入口和资产哈希。完整应用版本与迁移工具见 [UPGRADE.md](UPGRADE.md)。
