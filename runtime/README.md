# 固定 DSH 运行时

DSH 0.2.1-rc.2；官方源码锁定 `687f282e8625c6b8d11ff58211a90524be2e4317`。依赖树和完整性校验由 `npm/package-lock.json` 锁定。需要稳定版 Node ^22.19 或 >=24（不支持 Node 23）。安装器和 launcher 依次选择 `DSH_NODE`、项目私有的 `.runtime/node-path`、`PATH` 中的 Node，并检查实际版本；明确选择的 Node 失效时会报错，避免服务悄然切换到不同版本。

```bash
bash runtime/install.sh
python3 runtime/launch_dsh.py --home ~/.local/share/dsh-personal/dsh --port 18781
python3 runtime/probe.py
```

首次安装可用 `DSH_NODE=/absolute/path/to/node bash runtime/install.sh` 指定已有 Node。安装成功后，选择保存到已被 Git 忽略的 `.runtime/node-path`，服务无需继承交互式 shell 的 PATH 就能复用该运行时。该文件只含可执行文件路径，创建时请求权限 600；不支持 POSIX 权限的文件系统可能显示挂载设置决定的权限。安装器只安装本项目锁定依赖，不修改用户全局 Node/npm 配置；所选 Node 需配套可用的 npm。

跨版本兼容性探针必须显式指定旧版本的 npm 安装根目录，例如 `python3 runtime/native_version_probe.py --old-runtime /absolute/path/to/old/npm-root`，也可设置 `DSH_OLD_RUNTIME`。缺少旧版本安装时会在启动任何探针服务前退出，不会自动选择其他项目的安装目录。

生产进程由项目部署单元管理；不要另开第二个writer。launcher持flock保护数据根、仅绑定127.0.0.1，记录launch token到600权限文件且日志脱敏。内部网关key默认读取 `DSH_HOME/../gateway-key`，或继承 `DSH_GATEWAY_KEY`；不是模型供应商密钥。模型设置由个人系统网关掌管。

`personal.patch.json`是该固定版本已验证的原生patch，不是产品默认JSON。专属personal preset只含persona/time-context/timed提问；单次timed等待1秒结束，用户续答没有截止。provider固定经本地18782预算网关，background/foreground/delegated三个模型ID分别分账。关闭自动标题模型与provider内部重试。

Python调用只通过 `DSHBridge`，不写DSH底层日志。`events(session_id,after_seq=...)` 支持增量回读。`submit`在投递前查询持久rpcId，不依赖发布包在快速重试窗口中的去重。查存在失败不创建新会话；固定UUID在会话创建后关联写回前崩溃时可恢复。

原生公开RPC没有“失败计算重试”。`personal-control.mjs`作为同一宿主的有限插件提供认证的 `/api/personal.retry`：只接受已经存在的原始请求定位、独立稳定重试ID，拒绝无失败的会话或正在计算的会话；通过producer source `dsh-personal-retry`记一条明确系统控制事件，不复制原始用户消息，不冒充用户发言，不扩大工具权限。`resume_processing(session_id,retry_id,source_request_id)` 调用它；重试ID在原生日志中持久去重。API不暴露为模型工具。

`pending_questions(session_id)`返回原生问题批次；`answer_pending(session_id,call_id,answers)`只答明确批次，answers必须覆盖全部questionId。多题可先由业务持久缓冲，不能自动跳过未答项。晚答切foreground-model，原生答复按callId持久去重。若晚答后模型失败，恢复来源为`question:<callId>`；普通prompt来源仍为submission_id。

固定版本的assistant/message正文位于`event.data.message.content`，turn/end位于`event.data.reason.kind`。tool/call.arguments是JSON字符串，tool/result正文位于`event.data.message.content`。真实同步探针覆盖这些字段，手工构造flat data不能替代原生验证。

公开session API无持久历史消息局部删除接口，updateQueue.remove只移除尚在inbox中的一次排队项。不能用它宣称底层原文已擦除；业务删除后隔离旧会话并保持native_cleanup_pending。禁止直接编辑DSH存储文件。

探针在 `/tmp/dsh-personal-probe-*` 独立目录运行真实DSH进程，以固定本地SSE模型模拟工具/失败。结果不是供应商、真机或用户接受证据。探针输出写入 `evidence/P0/probe.*`；失败报告留在实际运行实例，不随公开源码分发。

迁移探针为原宿主停止后把DSH home复制到同机独立目录，冷启动后读出相同pending问题并晚答，检查原目录文件哈希不变。这证明该固定版本的目录迁移路径；不是全新机器、跨操作系统或真实用户数据迁移验收。
