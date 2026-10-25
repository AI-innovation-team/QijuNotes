# 导出、备份、恢复与迁移

默认数据目录为 `~/.local/share/dsh-personal`。以下命令在项目根目录执行，使用已安装依赖的 Python 环境；自定义目录需相应替换。

备份包含业务数据库、DSH 原生会话、附件、工作目录、脱敏配置、可读 Markdown、标准 JSON 和哈希清单。账号会话与模型、通知、网关凭据不迁移；新实例需重新配置，见 [账号说明](ACCOUNTS.md)。

数据库采用 SQLite backup API。全量导出持有应用检查点锁，并核实 DSH 单写宿主已停止；生产配置 `backup.host_unit` 为 `dsh-personal-host.service` 时，导出会短时停止并重启该项目宿主。不能取得原生写锁时明确失败，不复制正在写入的原生日志。应用写操作、worker 周期和网关记账必须采用 `operations.maintenance_lock(data)` 的共享锁；备份采用独占锁。只读 Markdown 导出不复制原生文件，无需停止 DSH。

## 配置与手动备份

首次生成独立 AES-256-GCM 密钥：

```bash
python3 -m dsh_personal.operations init-key --path "$HOME/.config/dsh-personal/backup.key"
```

命令不会输出密钥，已存在时拒绝覆盖。密钥文件须为 `0600` 或更严格，不能放在数据目录或备份目录内。用户须在独立安全位置保管密钥副本，不能只保留在唯一主机上。

```bash
python3 -m dsh_personal.operations backup \
  --data "$HOME/.local/share/dsh-personal" \
  --key "$HOME/.config/dsh-personal/backup.key"
```

默认输出到数据目录的 `backups`，保留最近 7 个日、4 个周、6 个月恢复点的并集。同一恢复点可同时承担多个周期，因此总数通常少于 17。只删除本工具 `index.json` 记录且已超期的 `.dshbackup` 文件，不清理原文或附件。

`--output` 可指定备份目录，`--offsite` 可指定已挂载且预先存在的异机目录；不会为其自动创建目录。私有 `backup.offsite_mount` 须包含预期挂载根 `mountpoint`、挂载来源 `source` 和文件系统 `fstype`。复制前后核对最深覆盖挂载和设备，缺失/替换会停止远端复制，不更新远端成功回执；本机快照与最新账本先持久化保留，CLI退出1使定时器能显示远端失败，修复挂载后重试。密钥须在数据、本地备份、账本与异机输出根之外，异机目录不能与本地数据/输出目录互为父子。路径检查仍不能完全消除并发卸载竞态，不保证物理机器独立性。省略命令行参数时读取 `config.json` 的 `backup.directory`、`backup.offsite_dir`；独立账本还支持 `backup.ledger_directory`，因此定时器会采用已配置目标。实际创建了另一条本机路径只能证明复制成功，不能证明异机故障恢复；需另留异机、密钥恢复和最新账本可用的证据。

生产定时器应在 Asia/Shanghai 每日 03:30 运行上述命令。备份不是模型任务，不消耗模型额度；不要在持有共享检查点锁的 worker 周期内部直接调用备份，以免锁升级等待。

## 独立修订和动作账本

周期快照不能知道快照后发生的删除、人工更正和外部动作。应在相应写操作结束后，或以独立短周期维护定时器运行：

```bash
python3 -m dsh_personal.operations ledger \
  --data "$HOME/.local/share/dsh-personal" \
  --key "$HOME/.config/dsh-personal/backup.key"
```

默认独立保存到 `recovery-ledger/latest.ledger.enc`。账本包含删除标识（不含被删除正文）、有效画像/成长修订、摘要与个人史人工更正、个人史称谓、任务版本与用户验收输入、日程记录/提交编号/版本动作与提醒回执、例程通知回执、用量预留；它本身也经过认证加密。`--offsite` 同时复制最新账本，`ledger_protected_at` 与 `ledger_offsite_at` 保留实际保护时间。定时保护间隔内的状态仍可能丢失，恢复报告须保留这段未知范围；不能宣称完全无丢失。

## 标准导出与校验

应用“导出”提供 Markdown 和 ZIP；命令行提供相同 ZIP：

```bash
python3 -m dsh_personal.operations export \
  --data "$HOME/.local/share/dsh-personal" \
  --output /tmp/dsh-personal-export.zip
python3 -m dsh_personal.operations verify --input /tmp/dsh-personal-export.zip
```

ZIP 内 `diary.md` 是完整对话原文，可脱离 DSH 阅读，按消息实际接收日期分日并建立周索引；活动原业务日期另列，补填不伪装成当日提交。`data.json` 保留全部权威表及关系；`records.json` 保留导入、画像与成长版本；附件保留相对路径及哈希。跨实例使用时，原生会话里的历史绝对路径仍作为原始记录存在；有效工作目录与原生晚答恢复须针对固定 DSH 版本独立验证，不以文件复制代替。

需要单独保留“已记载”的个人史时，已登录浏览器可请求 `/api/export?format=chronicle`，下载 `personal-history.md`；它包含来源引用并排除待整理条目，不代替完整备份。三个项目文件夹只是既有活动按业务日期的视图，不产生另一份原话副本。

加密备份校验：

```bash
python3 -m dsh_personal.operations verify \
  --input /path/to/snapshot.dshbackup \
  --key "$HOME/.config/dsh-personal/backup.key"
```

认证失败、哈希不符、路径穿越、重复路径或不支持的格式版本均拒绝处理。

## 恢复到干净目录

必须使用空目录；工具拒绝覆盖现有实例。示例中的备份路径需替换为 `backups/index.json` 记载的实际文件：

```bash
python3 -m dsh_personal.operations restore \
  --input /path/to/snapshot.dshbackup \
  --key "$HOME/.config/dsh-personal/backup.key" \
  --latest-ledger "$HOME/.local/share/dsh-personal/recovery-ledger/latest.ledger.enc" \
  --destination "$HOME/.local/share/dsh-personal-restored"
```

恢复总是先写 `isolated=true`，暂停日常调度、委托、自动续轮、通知和待执行效果。旧快照中的 pending/running 工作进入恢复核对状态，已发生但无回执的动作不会重放。较新账本中的删除和更正先合并，再重建可读导出；导入资料与画像上下文保持隔离。模型和通知配置置空，防止旧端点误发。日程合并优先保留较新版本及同版本已发生的提醒回执；原 pending/sending 提醒转为 `recovery_unknown`，不随解除隔离自动发送。核对未来安排后需本人明确编辑保存，才恢复所需提醒。个人史须先执行来源删除传播，不能用旧稿复活已删除原话的派生叙述。

`recovery-report.json` 记录实际恢复耗时、版本、附件校验与剩余条件。即使传了 `--latest-ledger`，也不会自动解除隔离：需核对账本覆盖时间、外部动作回执、DSH 版本及工作目录、任务成果和附件。

只有核对已经完成时才使用解除命令，`--user-input` 写入真实核对结论，不能填开发者代替用户验收的声明：

```bash
python3 -m dsh_personal.operations release \
  --data "$HOME/.local/share/dsh-personal-restored" \
  --ledger-coverage-confirmed --effects-confirmed \
  --user-input '填写实际核对结论及已确认的覆盖范围'
```

解除隔离不会自动把历史 `recovery_hold` 任务或未知通知重新排队；分别核对后通过明确委托或原活动继续。源数据删除涉及 DSH 原生副本时，恢复在暂存目录按最新账本清理旧命名空间，校验失败则拒绝发布恢复实例。在线删除未清理完的活动保留 `deletion_pending`；不能因解除隔离而绕过。成功后使用新原生身份，删除后的新记录按精确身份保留，详见 [删除说明](DELETION.md)。

## 验证边界

`tests/test_operations.py` 在隔离目录验证 SQLite 快照、认证加密、恢复、来源删除传播、修订/通知账本、锁、篡改和保留周期。夹具不能证明实际机器故障后的异机恢复、真实 DSH 晚答或手机通知。部署者应在实际目标核对密钥、最新账本、附件和原生版本，并保存自己的恢复结果。
