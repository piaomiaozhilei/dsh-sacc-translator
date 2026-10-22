# SACC Translator for DeepSeek Harness

Windows x64 翻译插件，复用 DSH 模型选择、语音输入和文档预览，支持文本及尽量保留原格式的文档翻译。

## 状态

当前源码对应 0.1.21，适配 DSH 0.2.1-rc.2。支持 CPU 版与自动绑定 GPU 组件的 CUDA 版。仓库只包含翻译所需源码，不包含 SACC 主应用、用户数据、密钥、日志或运行时二进制。

支持扩展名：doc/docx、xls/xlsx、ppt/pptx、pdf、png/jpg/jpeg/bmp/gif/webp、txt/md/csv/html/htm。复杂排版与 OCR 译文仍需人工复核。

## npm 安装（0.1.21）

官方“添加插件”输入 `dsh-sacc-translator-cpu` 或 `dsh-sacc-translator-cuda`。也可使用 CLI：

```powershell
dsh plugin --profile desktop add dsh-sacc-translator-cpu
# 或
dsh plugin --profile desktop add dsh-sacc-translator-cuda
```

npm 入口包约 26 KB。包管理器自动下载固定版本 GitHub Release 运行时；CUDA 额外自动下载 GPU 组件，无安装脚本。首次总下载量仍约 CPU 364 MB / CUDA 2.04 GB。不要同时启用两个版本。

基础运行时作为内部依赖 `dsh-sacc-translator-runtime` 引用已验证的 CPU 0.1.17 Release 包，用户无需单独安装或启用。npm 包不包含用户文档、设置、密钥或历史。0.1.17 完整 Release 安装方法仍可使用。

当前入口包构建：`npm pack ./packages/cpu` 和 `npm pack ./packages/cuda`。下面的完整包构建配方须先检出 `v0.1.17` 标签，避免混用版本。

## 0.1.21 更新

修复模型返回 JSON 后附加说明或输出多个 JSON 对象时导致整批翻译失败的问题。解析保留有效翻译，冲突条目交由定向重试；不接受截断或结构损坏的 JSON。

翻译聊天消息下方新增复制与时间操作，复用宿主剪贴板接口、图标和提示组件，提供复制成功/失败反馈。新任务记录结束时间；旧记录回退显示创建时间。保留 0.1.19 的并发和预览配置，以及固定的基础运行时/GPU 依赖。

验证：9 项插件回归测试通过，本机离线 CUDA 版安装后已由用户确认界面正常。本次未重新执行整篇 PDF 的端到端翻译。

## 0.1.21 更新

修复模型返回 JSON 后附加说明或输出多个 JSON 对象时导致整批翻译失败的问题。解析保留有效翻译，冲突条目交由定向重试；不接受截断或结构损坏的 JSON。

翻译聊天消息下方新增复制与时间操作，复用宿主剪贴板接口、图标和提示组件，提供复制成功/失败反馈。新任务记录结束时间；旧记录回退显示创建时间。保留 0.1.19 的并发和预览配置，以及固定的基础运行时/GPU 依赖。

验证：9 项插件回归测试通过，本机离线 CUDA 版安装后已由用户确认界面正常。本次未重新执行整篇 PDF 的端到端翻译。

## 0.1.19 更新

默认翻译并发为 5，已有设置保持不变。CPU/CUDA Bundle 均附带宿主预览配置：PDF/普通文件读取 1 GiB；Excel 256 MiB、100 万单元格、2 分钟；Office 输入 512 MiB、输出 PDF 1 GiB、转换 5 分钟。预览仍为整文件加载，大文件需要足够内存。宿主个人配置覆盖项优先。启用后重新打开 DSH，以便前后端加载配置。没有提高长期缓存上限。

这些配置作用于宿主共享预览服务，不仅是翻译面板。运行库继续使用固定的 0.1.17 / GPU 0.1.16 附件，格式支持及二进制未变更。

## 构建

0.1.17 发行包复用经过 SHA-256 校验的 0.1.16 运行时，保留已验证的格式支持。以下命令下载固定版本公开发行附件并构建三个包：

```powershell
python release/build_market.py --inputs .build/inputs --output plugins/dsh-translator/dist
```

输出 CPU、CUDA 与 GPU 附属包以及 SHA256SUMS.txt。构建只接受固定哈希的输入；源代码位于 plugins/dsh-translator，发行前端位于 packages/cpu 与 packages/cuda。GPU 二进制不存入 Git。

若修改 Python 文档处理逻辑，则需在 Windows x64 安装 requirements-build.txt，并在 .tools/libreoffice/runtime 准备官方 LibreOffice，使用 runtime/worker.spec 重新构建并完成文档回归验证，再更新发行输入哈希。Office 与 NVIDIA 等第三方组件保留其各自许可。

## 安装 0.1.17（Windows x64）

公共市场收录审核通过后，搜索“翻译 CPU”或“翻译 CUDA”。CPU 无 NVIDIA 依赖；CUDA 自动安装固定版本 GPU 附属组件，不需要手工下载 ZIP 或执行脚本。两个版本只启用一个，共用设置与翻译历史。

也可以直接使用 DSH 官方 CLI：

```powershell
# CPU（约 364 MB）
dsh plugin --profile desktop add https://github.com/piaomiaozhilei/dsh-sacc-translator/releases/download/v0.1.17/dsh-sacc-translator-cpu-0.1.17.tgz
# 或 CUDA（基础包约 364 MB，自动下载 GPU 组件约 1.68 GB）
dsh plugin --profile desktop add https://github.com/piaomiaozhilei/dsh-sacc-translator/releases/download/v0.1.17/dsh-sacc-translator-cuda-0.1.17.tgz
```

首次安装 CUDA 需要下载较大的组件，等待宿主安装完成；安装失败时在市场重试。组件不是独立 DSH 插件，无需启用，不单独列入市场。插件启用时校验 15 个 DLL 并自动挂载。运行时存放在 `%LOCALAPPDATA%/dsh-translator/runtimes` 的版本目录，避免 pnpm 深路径导致 Windows DLL 加载失败；同盘优先使用硬链接避免重复占用，跨盘才复制。该目录属于安装组件，不计入翻译文档缓存。首次校验和 CUDA 初始化可能需要一分钟。无兼容 NVIDIA GPU 时工作进程明确显示原因并回退 CPU。

已有用户请先结束翻译任务，通过宿主禁用旧版、安装新版，再启用所需版本；重启 DSH 后生效。更新不会删除翻译设置或记录。不要同时启用 CPU 和 CUDA。GPU 依赖下载失败会使 CUDA 安装失败，不会声称已完成 GPU 安装。GPU 组件校验失败会阻止文件翻译并提示重装，不会静默使用损坏文件。

0.1.17 复用已经验证的 0.1.16 文档运行时，仅更改安装依赖和组件挂载；没有模型、OCR 或文档格式逻辑变更。旧 0.1.16 ZIP/PowerShell 手动安装方法仅适用于旧版。

## 设置与隐私

模型调用由 DSH 完成，工作进程不持有模型凭据。CPU/CUDA 版本共用 DSH 数据目录下的 sacc-translator 设置和翻译记录。清理缓存会删除专属翻译产物及记录，不删除宿主共享附件。

请勿向仓库上传个人文档、翻译记录、环境配置、API 密钥或故障日志。问题报告优先使用自行构造的脱敏样例。

## 0.1.16 验证

18 种扩展名已通过本地固定模型响应的解析、回填及输出读取测试；旧版 Office 输出再转换后可读取，Excel 公式保留。GPU 可选组件安装后，三个 OCR 模型实际使用 CUDA。测试不代表对所有复杂文档或模型翻译质量的保证。

基础安装包约 364 MB；可选 GPU 组件约 1.68 GB，完整 CUDA 下载约 2.04 GB。首次 CUDA 初始化仍可能约一分钟；尚未实现跨任务引擎复用。

公共目录条目位于社区 awesome-dsh-plugin，审核通过后才能在 dshmarket 搜索。仓库 packages/cpu 与 packages/cuda 分别对应两个可安装条目，包内二进制仅通过 Release 分发。release/build_market.py 从校验过的 0.1.16 包可复现构建 0.1.17。
