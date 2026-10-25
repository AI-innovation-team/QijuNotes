# DSH个人系统 Codex任务包 1.1

日期：2026-10-22。业务时区：Asia/Shanghai。

本包用于在用户选定的Linux主机上建设、验证和部署个人成长与起居注系统。它包含已确认的技术规格和可执行任务指令；系统代码、目标主机部署与真实使用验收由接收本包的Codex继续完成。

## 1 这次已确认的产品规则

- A1学习21:00、A2反思21:30、A3打卡08:30，默认每天触发。无人回复、短答、已读未答、单次跳过不改变明日计划；只有用户明确指令可以调整时间、频率或暂停。
- 每项定时活动都是可以持续深入的聊天。一次简答足以完成基础记录，用户继续聊时应继续承接；摘要完成不会关闭会话。
- 今日入口能补填前几日：已有活动续聊，缺失活动按所选原日期补建，保留实际补填时间；历史补填不替代今天、不补发历史通知。
- 完整保留起居注、三平台导入、三条成长主线、明确委托与用户验收、手机入口、备份迁移等范围。

SPEC_v1.1.md是产品与技术依据。CHANGELOG_v1.1.md明确废止旧自动降频规则。不要混用1.0验收表或配置。

## 2 如何开始

1. 将压缩包放到准备建设本项目的Linux工作目录并解压，保留dsh_codex_taskpack_v1.1目录名。建议使用独立的dsh-personal项目目录；已有对应仓库时就在该仓库工作。
2. 在这个工作目录打开Codex。CLI已安装时可以运行codex；也可用能够访问同一目录的Codex客户端。先确认当前目录确实是本项目，不要在正在执行另一项科研分析的会话中混入这项建设。
3. 发送下面这一段即可，不需要把整份规格粘贴到聊天中。

~~~text
请读取当前工作目录中的 dsh_codex_taskpack_v1.1/START_CODEX.md，并按其要求开始实施。SPEC_v1.1.md是本项目开发基线，已确认默认每日唤醒、只有用户明确要求才调整频率或暂停、支持历史补填、所有例程可持续深入聊天。请先检查实际环境与已有项目，随后按P0–P4持续推进，完成代码、验证、部署与交付；不要停在方案、插件清单或最小原型。维护progress/PROGRESS.md与progress/HANDOFF.md。外部条件未齐时继续不受阻的工作，按验收矩阵如实保留待验证项。
~~~

本包的目录组织是项目约定。Codex能够识别AGENTS.md，但不会仅因一个目录名是tasks或templates就自动执行其中内容；启动指令负责明确入口。官方说明见文末。

## 3 用户需要介入的时刻

Codex先检查可读取的信息并自行处理常规工程决定。部署缺少实际主机、模型凭据、手机通知通道或异机备份目标时，它应集中说明具体缺项、用途及当前可继续部分。秘密填入目标环境的安全配置，不写进任务包或聊天报告。

不需要逐个批准P0、P1等阶段。已有委托范围内的实现、修复、隔离验证和项目自有服务部署连续推进。影响其他正在运行的项目、整机重启或实际不可逆动作，遵守已有授权和维护窗口。

可查看progress/PROGRESS.md了解当前可使用入口、完成了什么、证据在哪里和下一步。进入核心体验时，三项日常活动、历史补填、持续聊天、记录与任务验收应能连贯使用；初期纯探针不要求你逐条体验。

## 4 中断与续做

优先继续原项目会话。CLI可使用codex resume选择原会话；若需新开会话，读取同一目录中的prompts/RESUME_CODEX.md并执行其中续做指令。目标是从progress/HANDOFF.md、实际代码和证据继续，避免重复建服务或重做已完成阶段。

中断前让执行者保存：当前分支/提交、未提交修改、运行中的进程或任务、已通过和失败的场景、下一项可执行动作。会话摘要不替代这些可核查文件。

## 5 文件结构与用途

| 路径 | 用途 |
| --- | --- |
| START_CODEX.md | 一次启动所需的总执行指令 |
| SPEC_v1.1.md | 完整技术开发基线 |
| CHANGELOG_v1.1.md | 本次三项修订及旧规则撤销 |
| tasks/P0_CAPABILITIES.md | 固定版本、验证真实接口并跑通最小链路 |
| tasks/P1_INTERACTION.md | 持久入站、独立活动和跨端持续聊天 |
| tasks/P2_DAILY_AND_DELEGATION.md | 三项每日活动、历史补填和委托验收 |
| tasks/P3_RECORDS_AND_GROWTH.md | 原文、导入、纠错和三主线成长 |
| tasks/P4_DEPLOYMENT_AND_HANDOVER.md | Linux运行、恢复、观察与交付 |
| acceptance/ACCEPTANCE_MATRIX.json | 26项验收及子项的初始清单 |
| acceptance/USER_JOURNEYS.md | 用户实际使用路径和反例 |
| operations/DEPLOYMENT_RUNBOOK.md | 目标环境验证与部署恢复指南 |
| config/product-defaults.json | 本项目默认行为；不是可直接导入DSH的配置 |
| templates/AGENTS.project.md | 精简项目规则，按需合并进既有AGENTS.md |
| templates/PROGRESS.md | 进度摘要模板 |
| templates/HANDOFF.md | 断点与续做模板 |
| templates/ENVIRONMENT.md | 环境与部署事实模板 |
| templates/DECISIONS.md | 重要技术决定与范围变更记录 |
| prompts/RESUME_CODEX.md | 原会话或新会话续做 |
| prompts/REVIEW_CODEX.md | 独立验收与针对性返修 |
| SHA256SUMS | 包内文件的内容校验值 |

开发时生成progress/、evidence/、测试及应用源码；本包中的模板不代表这些工作已经完成。

## 6 何时算完成

核心可用与完整交付分别报告。完整交付必须满足全部约定必需场景及子项，包含三平台导入、三主线完整功能、真机通知、干净实例迁移与实际运行证据。固定样本、模拟时钟、自动化测试用户不能冒充真实设备、真实连续运行或用户本人验收。连续7天观察只能由真实经过的时间证明。

缺少外部条件时可以交付可运行代码和可体验版本，但要留下准确待办；不能把待验证清单写成“全部完成”。用户明确验收绑定实际成果版本。

## 7 Codex使用依据

核查日期2026-10-22。使用时以本机Codex版本和可用命令为准，不绑定模型名称、试验功能或未经验证的参数。

- [Codex CLI与会话恢复](https://developers.openai.com/codex/cli)
- [AGENTS.md项目指令](https://developers.openai.com/codex/guides/agents-md)
- [按阶段组织开发与记录上下文](https://developers.openai.com/cookbook/examples/codex/iterating-development-workflows-with-codex)

产品后台token额度用于部署后的DSH，不等于Codex开发过程的额度；两者分别记录。
