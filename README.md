# InkBloom

InkBloom 是一个本地运行的 Windows 漫画批量上色工具。主引擎使用 **Style2Paints V4.5**，支持图片、PDF、CBZ、ZIP、多张彩色样例、逐页样例匹配、八种原生输出层、阶段进度和实时日志。

## Style2Paints 工作流

1. 文档拆页。
2. 启动并加载 Style2Paints V4.5。
3. 从多张样例中为每一页选择最接近的参考图。
4. Style2Paints 线稿清理与区域分割。
5. Stage I 生成固有色和平涂层。
6. Stage II 生成精细渲染结果。
7. 下载并保存 Flat、Smoothed、Blended 等全部八个原生层。
8. 导出彩色 PDF、CBZ 和分层 ZIP。

## 安装官方 Style2Paints 程序

Style2Paints GitHub 仓库只包含 Apache-2.0 源码，官方预训练模型和二进制保留全部权利，因此模型不会提交到本仓库。请从官方 README 提供的地址下载 `style2paints45beta1214B.zip`，然后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-style2paints.ps1
```

脚本会校验官方文件大小与 MD5，再解压到 `models/style2paints`。官方压缩包信息：

- 大小：2,604,850,176 bytes
- MD5：`6dbccce33c5ac9ea3bdae3eabe93c94d`

## 从源码运行

```powershell
python -m pip install -r requirements.txt
python app.py
```

打开 `http://127.0.0.1:17860`。

## 构建便携版

```powershell
.\build-portable.ps1
```

如果 `models/style2paints/READY` 存在，构建脚本会把官方程序一并复制到便携目录。最终用户解压后双击 `InkBloom.exe` 即可使用。

## 说明

- Style2Paints V5 截至目前仍只有预览，没有公开模型，InkBloom 使用可获得的 V4.5。
- Style2Paints 的参考图是全局色彩风格条件，不保证自动识别角色身份；多张样例由 InkBloom 按页面结构自动选择。
- 颜色提示点沿用 Style2Paints 原生格式 `[x, y, R, G, B, 半径]`，最多 500 个。
- 输出图像可用于商业或非商业用途；官方模型和二进制的再分发权利仍属于 Style2Paints 作者。
