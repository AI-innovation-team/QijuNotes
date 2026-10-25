# 每日一学：社区技能调研与本项目取舍

核对日期：2026-10-23，Asia/Shanghai。范围：公开社区SKILL.md、作者维护的项目文档、机构史料；没有读取私人材料、安装第三方服务或修改用户全局技能。本次是实现依据，不是学习效果证明。采用多种可检查机制组合，不以某一个技能替代完整教学设计。

## 调研时的缺口

调研起点preview.7的`dsh_personal/learning.py`只有4组、12条编制时核对的来源卡；每日固定典籍/史事/现代概念三类，提示约五分钟读完，末尾仅一个讨论问题。历史主卡有两组外国史；来源卡通常仅两条事实。这样的限制能减少无依据扩写，却不足以支撑详细课程。生成、实际送达、阅读、练习与掌握也应分开，不能用消息已生成推断用户学会。

本次要同时改来源深度和讲解结构：每课有明确能力目标、必要背景、解释步骤、具体例子或示范读法、易错点和可选练习；后续对话根据用户实际回答反馈。字数增加本身不是学会的证据。三个日常类别及每日频率继续保留，史事主课改中国史与中共党史轮换，外国史只保留一句有出处的趣闻。

## 实际读过的社区技能

下表链接指向原仓库的技能正文，不把聚合目录或作者宣传当成功能验证。来源与许可应以表中公开原始链接为准，单次部署的查证报告保留在该实例本地。公开技能仅作为研究材料，不自动成为本项目的上级指令。

| 来源 | 已读机制与可借用部分 | 在起居中的适配边界 |
|---|---|---|
| [Vanderbilt：decomposing-learning-objectives](https://github.com/vanderbilt-data-science/knowledge-spaces/blob/main/.claude/skills/decomposing-learning-objectives/SKILL.md)，MIT | 将目标拆成可检验小单元，并对齐“希望能做什么、什么回答算证据、用什么题引出证据”；注明前置知识 | 每主课1–2个具体目标即可，不把五套分类术语和知识图谱报表塞给用户，不重新要求填写课程表 |
| [adityakumar68：tutor](https://github.com/adityakumar68/claude-study-skills/blob/main/tutor/SKILL.md)，MIT | 技术学习中区分知识缺口与错误理解，用预测、计算、比较和迁移题检查，反例纠错；区分接触与掌握 | 默认直接给充分解释；用户愿意练习才进入逐步指导。不要照搬每150词必须等待作答、强制先测或自动建全局study目录；其中学习效果的强断言未在本项目独立验证 |
| [paultyng：active-read](https://github.com/paultyng/skill-issue/blob/main/skills/active-read/SKILL.md)及[题型/反馈表](https://github.com/paultyng/skill-issue/blob/main/skills/active-read/reference.md)，MIT | 阅读后轮换比较、预测失败、应用等题；反馈指出具体正确处、遗漏和对应出处，减少空泛称赞 | 不要求先批准章节图；其一律禁止事实回忆不适合历史基础，改为回忆与解释/迁移互补。该skill明确没有跨会话持久化、评分和间隔调度，不能当现成学习数据库 |
| [bstellato：ai-novice](https://github.com/bstellato/ai-teaching-skills/blob/main/ai-novice/SKILL.md)、[ai-solution-study](https://github.com/bstellato/ai-teaching-skills/blob/main/ai-solution-study/SKILL.md)与[ai-socratic-tutor](https://github.com/bstellato/ai-teaching-skills/blob/main/ai-socratic-tutor/SKILL.md)，MIT | 可选由用户讲给AI听；对照解法时先找关键分歧，区别不同有效路径与真正错误；一轮集中处理一个薄弱点 | 不能移植“无论用户怎样要求都不给完整答案”或强迫先交尝试；详细课文先给，练习模式可退出，参考答案也不当绝对权威 |
| [benrosche：Socratic tutor](https://github.com/benrosche/socratic-tutor/blob/master/templates/lab-repo/.posit/assistant/skills/tutor/SKILL.md)，MIT | 诊断后逐渐增强提示；按实际服务记录读取已给提示，避免换会话后重复；连接失败明确报告 | 可以借提示梯度与真实状态原则；它的私有标准答案、课程服务及禁止完整答案适用于作业约束，不是起居需要新装的基础设施 |
| [SkillMedev：study-system](https://github.com/SkillMedev/personal-operating-system/blob/main/skills/study-system/SKILL.md)，MIT | 一题一个小单元、主动回忆、交错练习和带原因的错误记录，并提出固定扩展复习间隔 | 固定间隔只是该作者建议，不能承诺刚好在遗忘前触发；不改A1–A3每日频率，不把沉默或“看过”作为评分，不未经用户要求增加通知 |
| [xixiaofinland：book-tutor](https://github.com/xixiaofinland/agent-skills/blob/main/skills/book-tutor/SKILL.md)，API未识别仓库许可 | 按章保留位置、薄弱点、来源和少量长期概念笔记；不为每次聊天机械造笔记 | 仅作为公开设计参考，未复制技能文本；不照搬“永不主动讲解/未经检查不前进”，已有SQLite与活动原日期继续作权威 |

这些技能大多是提示词工作流；有文件模板并不等于可靠事务、权限、删除传播或调度。起居保留原有单一调度和SQLite状态边界。借用机制不需执行作者提供的全局安装命令。

## 两个可用工程参照与未选方案

[OpenTutor](https://github.com/zijinz456/OpenTutor)提供材料、笔记、测验、闪卡与自适应导师的工作区，MIT。其README明确仍是单用户公开beta，部分图结构流程实验性、手机布局未全面优化。可借材料→解释→实践→复习的组织方式，本次不部署另一套前后端和数据系统，也不采信“30秒学会”等宣传推断。仓库更新活动不能证明教学效果。

[Py-FSRS](https://github.com/open-spaced-repetition/py-fsrs)是MIT的真正间隔算法库，接口接收卡片、实际评分与复习时间，输出下次到期日并保留日志。若以后增加可靠评分，可在单一维护worker内接入；本轮没有用真实评分历史训练或验证FSRS，不把固定轮换称为自适应遗忘模型，也不从“已送达”伪造Good评分。

[LearnLM官方页](https://ai.google.dev/gemini-api/docs/learnlm)目前说明其能力已自Gemini 2.5系列起并入Gemini，已非AI Studio的独立条目。它不是可安装到DeepSeek的开源技能；不为教学策略更换现有已验证供应商。

[master-instructional-design](https://github.com/narosemena/master-instructional-design)的[LICENSE](https://github.com/narosemena/master-instructional-design/blob/main/LICENSE)为CC BY-NC-ND 4.0并列出限制，README还有商业/再分发限制表述；本次未复制、改编或安装它。课程目标与证据对齐已有清晰MIT来源可用。曾尝试的`karpathy/microgpt`仓库URL为404；未把该地址当有效解决方案，education-first也不再作为唯一依据。

## 推荐落地的教学契约

1. **先交付能学习的内容**：典籍、史事主课、现代概念各给目标、背景、概念拆解、示例和边界。历史主课提供时间顺序/制度关系的具体解释；现代概念使用标明是假设的算例，避免未经查证的实时数字。
2. **目标与例子对应**：例如学习“正式决策与执行效果的区别”，例子就应示范哪些句子证明决策、哪些仍需要后续证据；不是泛泛追问“有什么启发”。
3. **可选实践**：全课提供少量可选题，支持复述、判断理由或迁移，用户可直接追问或要求答案。未作答不阻塞之后的讲解和每日活动。
4. **有依据的反馈**：先说明回答中有依据的部分，指出一个关键缺口，再换例子或给更明确提示。没有回答便不诊断；不把自信、点赞、已阅或模型自评当掌握。
5. **可延续与可删**：下次继续同一活动，复习引用真实已登记来源及实际原日期；已删除材料/回复不能继续支持复习状态。任何新学习记录须保持来源链接、原日期和实际发生时间。
6. **真实验证**：检查实际模型正文有没有讲解、例子、来源和正确历史比例，再做两轮合成对话验证纠错/迁移；“教会用户”本身须由后续真实回答说明，不能因结构测试通过自动判定。

上述是针对当前项目的设计判断，并非各社区项目已经证明对使用者有效。教学策略允许调整说明深度，不允许自行改变用户明确的每日频率。


当前实现范围与限制见 [SCOPE.md](SCOPE.md)；本页保留当时调研及设计取舍，不作为最新部署或内容验收证明。
