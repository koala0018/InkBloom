# InkBloom

免费、本地运行的 Windows 漫画批量上色工具。支持图片、PDF、CBZ、ZIP，使用 ECCV16 神经网络自动推测颜色，并可从彩色样例校准整部作品的色彩分布。完成后同时导出 PDF 与 CBZ。

## 便携版

解压 `InkBloom-Windows-x64-portable.zip`，双击 `InkBloom.exe`。无需安装 Python，程序数据与输出都保存在自身文件夹。

## 从源码运行

```powershell
python -m pip install -r requirements.txt
python app.py
```

打开 `http://127.0.0.1:17860`。同一局域网设备可以使用页面底部显示的地址访问。

## 构建

```powershell
.\build-portable.ps1
```

## 说明

内置两个引擎：

- **AI 精细**：BSD 许可的 ECCV16 深度学习上色网络，ONNX CPU 推理，不要求独立显卡；彩色样例用于色度校准。
- **极速模式**：参考图调色板与边缘感知传播，适合快速预览或大批量低延迟处理。

AI 能推测合理颜色，但无法从一张样例保证识别每个角色身份或指定部件；复杂角色仍可能需要人工校正。所谓“保证任意漫画准确”在技术上不可验证，本项目不会作虚假承诺。

模型来源与许可证见 `THIRD_PARTY_NOTICES.md`。源码构建前需运行 `tools/export_ai_model.py` 生成 `assets/models/eccv16-colorizer.onnx`；官方便携包已内置模型。

MIT License
