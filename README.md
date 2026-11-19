# ZonedOCR（分区 OCR）

**正文、脚注、书眉、原书页码，各归其位。**

中文 · [English](README.en.md)


普通 OCR 只是把扫描书变成"能搜到字"。ZonedOCR 认得书的版式：把扫描版中文书（夹英文，简繁皆可）或英文书 PDF 做成可复制、可搜索、可高亮的 PDF，原扫描图不动，叠加逐字对齐的隐形文字层，并且：

- **智能分区**：正文、脚注、图表、书眉、边码、页码分区写入；拖选正文不会夹带书眉、页码或脚注
- **原书页码**：自动推算印刷页码写成页标签（含罗马数字前言），Zotero/阅读器引用时直接显示原书页码；误识别的页码按前后页纠正
- **自动书签**：识别章、节标题生成目录书签
- **方向纠正**：自动识别倒置、横躺（90°/180°/270°）的页面，并扶正几度以内的歪斜，输出 PDF 换成扶正后的页面。每页约多 20 秒（双核 CPU 实测），开头询问时会按页数报出预计多花的时间
- **复杂版式**：横向双页扫描自动拆页；多栏页按栏连续复制；图表、图注单独成组
- **逐字对齐**：高亮框贴合原图文字，浏览器、Zotero 里高亮连续不碎
- **人工校对**：OCR 后按页对图校对（`corrections.py`），标点按中英文语境设全角/半角
- **英文书**：自动识别英文书，脚注号转上标、"Chapter 9 + 章名"合成一个书签、Zotero 版英文行尾不粘词
- **Zotero 专用版**：解决 Zotero 复制时换行插空格、英文词间高亮断开等问题

## 安装

```bash
pip install rapidocr_onnxruntime pymupdf pymupdf-fonts numpy pillow
# 另需 poppler-utils（pdftotext）
```

首次运行会下载 PP-OCRv6 medium 识别模型（约 77MB），补破折号用的 PP-OCRv5 server（约 85MB）按需下载，都放在 `models/`。

## 用法

一条命令跑完，开头只问三件事：要不要 Zotero 优化版、要不要识别目录大纲、要不要扶正方向歪斜的页面，之后不用再管：

```bash
python3 zonedocr.py run book.pdf
# 要 Zotero 优化版吗？（在 Zotero 里阅读、高亮、复制时用） [y/N] y
# 要识别目录大纲（章节书签）吗？ [Y/n]
# 要自动识别页面方向、扶正歪斜的页面吗？（每页约多 20 秒，本书 300 页，约多 100 分钟；扫描端正的书可以不选） [Y/n]
# … 完成：book_work/book_OCR_Zotero.pdf
```

也可以直接带参数跳过提问：`--zotero` / `--no-zotero`、`--toc` / `--no-toc`、`--deskew` / `--no-deskew`。

想更准确：对照 `book_work/proof/` 里的逐页文本写 `corrections.py`，再运行 `python3 zonedocr.py book_work build`。分步命令（images / ocr / classify / proof / build / check）见 SKILL.md。

完整流程、配置项和设计说明见 [SKILL.md](SKILL.md)，校对模板见 `corrections.example.py`。也可以把本目录作为 AI 助手的技能（skill）使用，由 AI 跑完识别、分区、校对全流程。

## 许可

MIT
