# scan-ocr-pdf

把扫描版中文书页（夹英文，简繁皆可）PDF 做成可复制、可搜索、可高亮的 PDF：原扫描图不动，叠加逐字对齐的隐形文字层。

- 正文、脚注、图表、书眉、边码、页码分区写入，拖选正文不夹带其它区域
- 横向双页扫描自动拆页；多栏页按栏连续复制；图表、图注单独成组
- 印刷页码页标签、章节书签自动生成
- OCR 后按页人工校对（corrections.py），标点按中英文语境全角/半角
- 可出 Zotero 专用版（处理 Zotero 复制换行插空格的问题）

## 安装

```bash
pip install rapidocr_onnxruntime pymupdf pymupdf-fonts numpy pillow
# 另需 poppler-utils（pdftotext）
```

首次运行会下载 PP-OCRv6 medium 识别模型（约 77MB），补破折号用的 PP-OCRv5 server（约 85MB）按需下载，都放在 `models/`。

## 用法

```bash
mkdir WORK && echo '{"src": "book.pdf", "out": "book_ocr.pdf"}' > WORK/config.json
python3 scanocr.py WORK images
python3 scanocr.py WORK ocr
python3 scanocr.py WORK classify
python3 scanocr.py WORK proof      # 逐页对图校对，写 WORK/corrections.py
python3 scanocr.py WORK build      # 或 build --zotero
python3 scanocr.py WORK check
```

完整流程、配置项和设计说明见 [SKILL.md](SKILL.md)，校对模板见 `corrections.example.py`。
