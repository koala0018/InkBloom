# InkBloom

InkBloom 是本地运行的 Windows 漫画批量上色工具，支持图片、PDF、CBZ、ZIP 和多张彩色样例。

## 上色引擎

### Cobra（默认）

- 面向参考图漫画上色，可同时利用多张人物、服装和场景样例。
- 自动检索与当前漫画页局部最相关的参考区域。
- 更好地保持人物肤色、发色、衣服和场景固有色。
- FP16、单页串行推理，当前便携版适配 RTX 5060 Ti 16GB。
- 输出后恢复原始线稿与纸白，减少颜色过重、偏色和脏灰。

Cobra 独立环境和权重位于 `models/cobra`，不需要 ComfyUI。移动程序时必须移动完整的 `InkBloom` 文件夹。

### Style2Paints V4.5

保留为快速兼容模式，支持原生 Stage I/II、颜色提示点和八种输出层。

## 使用

1. 双击 `InkBloom.exe`。
2. 上传漫画图片、PDF、CBZ 或 ZIP。
3. 上传多张人物和场景彩色样例。
4. 默认选择 Cobra，按需要调整步数、Top-K、色彩强度和线稿保护。
5. 查看逐阶段进度、日志与高清预览，完成后下载 PDF 或 CBZ。

## 便携版

用户无需安装 Python。请保留以下内容：

```text
InkBloom/
  InkBloom.exe
  _internal/
  models/
    cobra/
    style2paints/
```

## 许可证

InkBloom 源码使用 MIT License。Cobra 使用 Apache License 2.0。Style2Paints 官方预训练模型和二进制文件保留其作者声明的权利，详见 `THIRD_PARTY_NOTICES.md`。
