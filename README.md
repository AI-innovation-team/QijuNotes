# 起居 · QijuNotes

自托管的个人起居与成长系统：保存日常对话，持续学习、反思与睡眠交流，整理可追溯的个人史，管理日程和委托成果。

<img src="web/qiju-avatar.png" alt="起居的蓝发鱼系史官头像" width="180">

项目采用 Python / SQLite、原生网页界面和固定版本的 DeepSeek DSH。产品基线为 [SPEC 1.1](plans/dsh_codex_taskpack_v1.1/SPEC_v1.1.md)，当前处于持续开发的预览阶段。

## 功能

- **持续对话**：每日一学、反思和睡眠打卡各自按日期归档，可以持续追问、历史补填、修改记录；支持 Markdown、未发送草稿恢复和跨设备同步。
- **学习与研读**：可追溯的来源材料、课程提前准备、史实与分析区分、逐段研读笔记。夜间研读策略须在实例中明确启用。
- **个人史与资料**：批量导入 ChatGPT / Kimi 等整理文件，保留原日期与来源，生成第三人称叙事，支持自定义称谓、更正与删除。
- **日程与成长**：月历、日列表、24小时周视图，三条成长主线与版本化委托成果；成果验收由用户提交。
- **长期要求**：聊天中保存回复风格、例程交流要求和已有委托需求，并给出程序保存回执。
- **设备与维护**：账号密码、蓝白/夜间主题、手机布局、私网访问、ntfy 通知、加密备份及较新修订账本恢复。

每日活动默认每天触发，只有用户明确要求才改变频率或暂停。历史补填保留原业务日期和实际提交时间。

## 当前边界

聊天反馈可以登记与追踪，尚未自动连接代码修改和部署。指定学习主题能保存并影响讲解，但主题驱动的检索、选材与排课尚未打通。视频目录与已取得正文分别记账，不能把登记来源当作已研读。新实例不继承任何部署的手机、模型、七日观察或用户验收结果。

## 安装

适用环境：Linux / systemd 用户服务，Python 3.12，Node `^22.19` 或 `>=24`，以及 npm。开发测试可以在独立临时目录运行。

```bash
git clone https://github.com/AI-innovation-team/QijuNotes.git
cd QijuNotes
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

# 先保证 PATH 中的 node 满足版本要求；也可显式设置 DSH_NODE。
bash runtime/install.sh
python scripts/install-service.py
bash scripts/control.sh status
```

安装器创建本项目的用户服务及数据目录。首次打开 [本机入口](http://127.0.0.1:18780/)，创建账号后登录。模型和手机通知按 [配置说明](docs/CONFIGURATION.md) 接入；密钥只保存在本机私有配置目录。没有模型时仍保存原话，但不能生成模型回复。

默认数据目录为 `~/.local/share/dsh-personal/`。模型密钥、账号文件、原生会话、附件、数据库和恢复密钥均不属于源码仓库。安装器不会自动安装或启动 ntfy；该服务有独立准备步骤。

```bash
bash scripts/control.sh stop
bash scripts/control.sh start
bash scripts/control.sh restart
```

DSH 原生宿主、应用与网关各有固定职责；不要在同一数据目录启动第二个宿主。换机时先阅读 [恢复说明](docs/RECOVERY.md)，不要把源码上传当成数据备份。

## 文档

| 主题 | 文档 |
| --- | --- |
| 日常使用 | [使用指南](docs/USER_GUIDE.md)、[聊天界面](docs/CHAT_INTERFACE.md)、[账号](docs/ACCOUNTS.md) |
| 记录与活动 | [个人史](docs/CHRONICLE.md)、[日程](docs/CALENDAR.md)、[材料准备](docs/MATERIAL_PREPARATION.md) |
| 学习 | [来源登记](docs/STUDY_SOURCES.md)、[教学设计参考](docs/LEARNING_RESEARCH.md) |
| 部署维护 | [配置](docs/CONFIGURATION.md)、[安卓](docs/ANDROID.md)、[HarmonyOS](docs/HARMONYOS.md)、[升级](docs/UPGRADE.md) |
| 数据保护 | [删除](docs/DELETION.md)、[备份恢复](docs/RECOVERY.md) |
| 开发 | [测试](tests/README.md)、[API](docs/API_UI.md)、[范围与限制](docs/SCOPE.md)、[公开源码维护](docs/PUBLIC_SOURCE.md) |

## 目录

```text
dsh_personal/    事务、会话、学习、记录、任务、日程与服务
web/             原生 HTML / CSS / JavaScript 界面
runtime/         固定 DSH 版本、适配器、来源目录及安装器
scripts/         部署、账号、备份验证与维护入口
tests/           隔离单元、集成及浏览器回归
docs/            使用文档与资料模板
plans/           产品基线与验收计划
progress/        公开空白账本；不包含某位用户的真实验收记录
```

## 验证

```bash
python -m unittest discover -s tests -v
python scripts/validate-acceptance.py
```

默认 Python 测试使用合成夹具和临时数据，不调用外部模型。缺少本机 ntfy 测试归档时，相应集成项会跳过。浏览器回归与原生宿主探针的独立环境要求见 [测试说明](tests/README.md)。通过自动测试不代表模型内容、真机通知或长期运行已完成验收。

依赖许可证由各自项目提供，见 [第三方组件](THIRD_PARTY.md)。本项目尚未指定开源许可证。
