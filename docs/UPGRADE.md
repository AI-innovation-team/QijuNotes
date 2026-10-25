# 固定版本、升级与回退

DSH 固定版本由运行时清单和依赖锁文件约束。应用 schema 为 1；同 schema 新增功能不意味着任意旧版程序都能理解新状态。升级与回退须保护原话、修订、账号、任务版本和外部动作账本，不用旧备份直接覆盖当前实例。

## 安装当前固定应用

以下命令在项目根目录执行：

```bash
python3 -m dsh_personal.release install \
  --source "$PWD" \
  --destination "$HOME/.local/share/dsh-personal-releases"
```

目录名为 `应用版本-源码指纹`。文件清单记录实际 Git 提交（尚未提交时为 null）、源码指纹、数据库兼容版本及原生运行时依赖。重复安装相同源内容会复用同一目录并重新核对清单；不同内容产生新目录。复制期间文件发生变化时校验失败，候选不会被激活。

应用版本共享明确版本的现有依赖，不复制成多套独立运行时。清单记录实际依赖路径与版本；这适用于本机源码版本管理。干净机器必须在候选目录执行 `runtime/install.sh`，从 `runtime/npm/package-lock.json` 重新安装固定依赖，并提供符合该 DSH 版本要求的 Node；不能依靠旧机器路径完成迁移。

```bash
python3 -m dsh_personal.release verify --release /实际安装的应用版本目录
```

版本化安装本身不停止服务、不修改当前运行目录。

## 升级检查顺序

1. 先完成 `operations backup` 加密全量恢复点和最新独立账本保护，并核对密钥可用。
2. 在干净隔离目录恢复副本，安装候选应用，验证消息、待答问题、明确委托、摘要更正、个人史来源、自动编修与人工修订、称谓、日程/提醒账本、选择性导入、成长关系和附件。
3. 在需要迁移的副本上执行 `release migrate`，查看原始与候选 schema、迁移数量、日期、原文和失败项。
4. 只有候选验证通过后，再在维护窗口停止本项目的两个服务，执行生产数据迁移/应用激活。不要覆盖其他服务。

```bash
systemctl --user stop dsh-personal.service dsh-personal-host.service
python3 -m dsh_personal.release migrate \
  --data "$HOME/.local/share/dsh-personal" \
  --target-schema 1 \
  --candidate-release /实际安装的候选版本目录
```

已是 schema 1 时，该命令返回 `changed=false`，不会冒充执行了一次迁移。应用本身兼容 schema 1、无需数据迁移时，单独激活：

```bash
python3 -m dsh_personal.release activate \
  --data "$HOME/.local/share/dsh-personal" \
  --release /实际安装的候选版本目录
```

激活更新数据目录的 `current-release` 符号链接与 `active-release.json`。服务只有把工作目录/入口明确配置到这个版本路径时，才会使用该版本；现有直接指向源码仓库的服务不会被此工具暗中修改。启动前须核对 systemd 实际 `WorkingDirectory` 和 `ExecStart`。不能仅见激活成功就宣布运行版本已经切换。

升级/回退必须同时取得应用锁、DSH 宿主锁和独占检查点锁；任一服务仍在运行时会拒绝。迁移前通过 SQLite backup API 创建 `upgrade-checkpoints/<时间>/before.sqlite`，记录哈希和源/目标应用兼容信息；在 `candidate.sqlite` 中迁移并校验后才替换生产数据库。迁移失败保留原库和检查点。数据库切换会先完成 WAL 检查点，避免旧 WAL 被误应用到新库。发布数据库与应用指针期间保留 `upgrade-in-progress.json`；中断时保留检查点定位并禁止应用自动启动，成功或完整回退后才清除。应用入口同时拒绝不兼容 schema，不能在回退后的旧库上隐式创建新表。

开发期 schema 0 的精确格式由 `dsh_personal.release.SCHEMA0_DDL` 定义：只有 `meta` 与 `legacy_entries`。未知源 schema、含糊时间戳或其他旧表布局会拒绝，不猜测迁移方法。迁移结果始终 `isolated=true`，不补发历史通知、不执行导入历史委托；核对后使用已建立的恢复核对流程或专项检查点确认再开放服务。

## 数据与应用一起核对回退

```bash
python3 -m dsh_personal.release rollback \
  --data "$HOME/.local/share/dsh-personal" \
  --checkpoint /实际upgrade-checkpoints目录
```

工具首先核对检查点哈希、历史应用是否仍在以及是否兼容旧 schema。回退前另保存 `before-rollback.sqlite`，避免覆盖升级后新增内容而无恢复机会；新旧资料的合并需独立核对，不能因回退就悄悄丢掉新内容。

若检查点记录了可验证且兼容的历史应用，回退数据库并恢复其版本指针。若没有历史应用（如 schema 0 开发夹具），工具只回退数据库、保持隔离，并撤去不兼容的当前版本指针，明确报告 `application_rolled_back=false` 和剩余条件。恢复旧应用程序但保留不兼容新数据库不是有效回滚。

本机私有 `upgrade-checkpoints` 是离线操作的短期回退材料，不代替加密异机备份，也不能把其明文数据库复制到异机目标。长期保留与跨机器灾难恢复使用 `docs/RECOVERY.md` 的加密工具。

## 验证边界

`tests/test_release.py` 验证明确开发夹具中的迁移、身份/日期保留、失败不改源、运行锁拒绝、回退保护较新状态、未知 schema 拒绝和文件清单。schema 0 是 `SCHEMA0_DDL` 定义的开发输入，不表示存在一个可任意升级的历史产品。

固定原生版本之间的日志代际、待答工具契约和回滚可用性需在停机副本中独立检查。`runtime/native_version_probe.py` 提供隔离探针入口；旧版本缺少的契约不能因普通聊天可读而记为兼容。公开源码不附带私人主机的部署或验收历史。

网页快照发布只处理静态资产，与完整应用版本安装不同；参见 [CONFIGURATION.md](CONFIGURATION.md)。
