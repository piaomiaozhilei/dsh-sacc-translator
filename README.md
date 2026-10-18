# SACC Translator for DeepSeek Harness

Windows x64 翻译插件，复用 DSH 模型选择、语音输入和文档预览，支持文本及尽量保留原格式的文档翻译。

## 状态

当前源码对应 0.1.16，适配 DSH 0.2.1-rc.2。支持 CPU 基础版、CUDA 基础版及独立可选 GPU 组件。仓库只包含翻译所需源码，不包含 SACC 主应用、用户数据、密钥、日志或运行时二进制。

支持扩展名：doc/docx、xls/xlsx、ppt/pptx、pdf、png/jpg/jpeg/bmp/gif/webp、txt/md/csv/html/htm。复杂排版与 OCR 译文仍需人工复核。

## 构建

在 Windows x64 上准备 Python 环境，安装 `requirements-build.txt`。将官方 LibreOffice Windows 运行时放在 `.tools/libreoffice/runtime`（应包含 `program/soffice.com`）；构建脚本只打包转换所需的保留资源。Office 及 NVIDIA 等第三方组件另受各自许可约束。

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --distpath plugins/dsh-translator/.build/worker-lean --workpath plugins/dsh-translator/.build/pyinstaller-lean plugins/dsh-translator/runtime/worker.spec
python plugins/dsh-translator/tools/package.py
```

输出位于 `plugins/dsh-translator/dist`。打包要求新的版本暂存目录，以免旧文件混入新包。

## 本机安装

通过 DSH 自带 CLI 将生成的 CPU 或 CUDA tgz 安装到 desktop 配置；只启用一个版本：

```powershell
dsh plugin --profile desktop add ./plugins/dsh-translator/dist/dsh-sacc-translator-cuda-0.1.16.tgz
```

GPU ZIP 与对应的 `Install-GpuRuntime-0.1.16.ps1` 放在同一目录。等待翻译任务结束后，执行以下命令，将路径替换为实际安装的 CUDA 插件目录（包含 package.json 和 runtime）：

```powershell
./Install-GpuRuntime-0.1.16.ps1 -PluginPath '实际 CUDA 插件目录'
```

脚本校验插件版本和 GPU ZIP 的 SHA-256。未安装组件时 CUDA 基础版明确提示并使用 CPU；已安装组件后，三个 OCR 模型实际执行后才报告 CUDA 状态。重启 DSH 后使用插件。

## 设置与隐私

模型调用由 DSH 完成，工作进程不持有模型凭据。CPU/CUDA 版本共用 DSH 数据目录下的 sacc-translator 设置和翻译记录。清理缓存会删除专属翻译产物及记录，不删除宿主共享附件。

请勿向仓库上传个人文档、翻译记录、环境配置、API 密钥或故障日志。问题报告优先使用自行构造的脱敏样例。

## 0.1.16 验证

18 种扩展名已通过本地固定模型响应的解析、回填及输出读取测试；旧版 Office 输出再转换后可读取，Excel 公式保留。GPU 可选组件安装后，三个 OCR 模型实际使用 CUDA。测试不代表对所有复杂文档或模型翻译质量的保证。

基础安装包约 364 MB；可选 GPU 组件约 1.68 GB，完整 CUDA 下载约 2.04 GB。首次 CUDA 初始化仍可能约一分钟；尚未实现跨任务引擎复用。

当前市场脚本仅提供本机 127.0.0.1 索引，上传此仓库不会自动加入公共插件市场。发行附件与公共索引需单独发布。
