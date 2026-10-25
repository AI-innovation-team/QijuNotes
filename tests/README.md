# 测试

在仓库根目录运行。测试使用临时目录和明确的合成样本；不要把生产数据库复制到测试目录。

## Python 回归

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/validate-acceptance.py
```

无需供应商密钥。通知服务的二进制集成场景只有在项目本地已有固定版本 ntfy 测试归档时才运行，否则显示跳过。Node 选择与安装器测试使用模拟可执行文件，不改全局 Node。

公开 `progress/ACCEPTANCE.json` 是78项均未实测的空账本。校验脚本检查结构和证据关系，不会把工程测试或空账本判成真实验收通过。

## 当前隔离浏览器回归

```bash
npm install --prefix .runtime/browser --save-exact playwright@1.58.2
.runtime/browser/node_modules/.bin/playwright install chromium
mkdir -p evidence/P4
node tests/mobile_interaction_browser.cjs
node tests/composer_autosize_browser.cjs
node tests/directive_receipt_browser.cjs
node tests/ui_night_mode.cjs
node tests/test_feedback_ui.cjs
```

这些入口自行启动临时服务及合成账号，结束后停止服务。默认使用 Playwright 对应的 Chromium；已有兼容浏览器时可设置 `QIJU_CHROMIUM_PATH=/absolute/path/to/chromium`。需要系统浏览器依赖时，按 Playwright 安装提示在你选定的环境处理。

每次运行会写入 `evidence/P4/`；报告和截图可能含测试内容或本机信息，不自动提交到公共仓库。

其余 `ui_*.cjs` 包含历史回归入口，部分针对旧版登录/界面，并依赖事先建立的 `.runtime/qa` 服务。它们作为开发参考保留，不属于上述当前无外部依赖的运行清单。

## 原生与真实模型验证

`runtime/probe.py` 等探针需要先安装固定 DSH 依赖，使用隔离宿主和本地模拟模型。`runtime/native_version_probe.py` 还需要显式提供独立旧版 npm 安装目录，不能默认借用其他项目。

真实供应商验证由 `scripts/verify-live-model.py` 单独执行，默认只展示计划；加 `--run` 会产生实际模型调用及费用。详见 [真实模型验证](../docs/LIVE_MODEL_VALIDATION.md)。自动回归不运行它，也不替代用户本人、真实手机或经过时间的证据。
