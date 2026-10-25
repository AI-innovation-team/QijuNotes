# 第三方组件

- `@deepseek-ai/dsh`：固定 `0.2.1-rc.2`，MIT 许可证；依赖树及下载完整性见 `runtime/npm/package-lock.json`。源码来源：https://github.com/deepseek-ai/deepseek-harness 。安装后的包以其附带许可证为准。
- `cryptography`：用于 AES-GCM 加密备份，许可证见安装分发包；依赖未打包进本仓库。
- Playwright：可选浏览器测试依赖，Apache-2.0 许可证；未打包浏览器二进制。
- ntfy：可选通知服务，版本和校验信息见 `runtime/ntfy-version.json`；未打包服务或安卓安装包，许可证由上游分发提供。

研读目录保存公开资料出处与有限教学摘录，来源内容权利属于相应作者/出版方；数据库中获取的长正文不随源码分发。产品头像是本项目使用的生成图像。

以上说明不替代各依赖许可证，也没有为本项目新增开源许可。
