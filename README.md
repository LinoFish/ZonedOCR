# ZonedOCR（分区 OCR）

**正文、脚注、书眉、原书页码，各归其位。**
**Body, footnotes, headers and printed page numbers — each in its own zone.**

[中文](#中文) · [English](#english)

---

## 中文

普通 OCR 只是把扫描书变成"能搜到字"。ZonedOCR 认得书的版式：把扫描版中文书页（夹英文，简繁皆可）PDF 做成可复制、可搜索、可高亮的 PDF，原扫描图不动，叠加逐字对齐的隐形文字层，并且：

- **智能分区**：正文、脚注、图表、书眉、边码、页码分区写入；拖选正文不会夹带书眉、页码或脚注
- **原书页码**：自动推算印刷页码写成页标签（含罗马数字前言），Zotero/阅读器引用时直接显示原书页码；误识别的页码按前后页纠正
- **自动书签**：识别章、节标题生成目录书签
- **复杂版式**：横向双页扫描自动拆页；多栏页按栏连续复制；图表、图注单独成组
- **逐字对齐**：高亮框贴合原图文字，浏览器、Zotero 里高亮连续不碎
- **人工校对**：OCR 后按页对图校对（`corrections.py`），标点按中英文语境设全角/半角
- **Zotero 专用版**：解决 Zotero 复制时换行插空格、英文词间高亮断开等问题

### 安装

```bash
pip install rapidocr_onnxruntime pymupdf pymupdf-fonts numpy pillow
# 另需 poppler-utils（pdftotext）
```

首次运行会下载 PP-OCRv6 medium 识别模型（约 77MB），补破折号用的 PP-OCRv5 server（约 85MB）按需下载，都放在 `models/`。

### 用法

```bash
mkdir WORK && echo '{"src": "book.pdf", "out": "book_ocr.pdf"}' > WORK/config.json
python3 zonedocr.py WORK images
python3 zonedocr.py WORK ocr
python3 zonedocr.py WORK classify
python3 zonedocr.py WORK proof      # 逐页对图校对，写 WORK/corrections.py
python3 zonedocr.py WORK build      # 或 build --zotero
python3 zonedocr.py WORK check
```

完整流程、配置项和设计说明见 [SKILL.md](SKILL.md)，校对模板见 `corrections.example.py`。也可以把本目录作为 AI 助手的技能（skill）使用，由 AI 跑完识别、分区、校对全流程。

---

## English

Ordinary OCR only makes a scanned book searchable. ZonedOCR understands the page layout. It turns scanned Chinese book pages (mixed with English, Simplified or Traditional) into a PDF you can copy, search and highlight: the original scan stays untouched, with an invisible, character-aligned text layer on top, plus:

- **Smart zoning**: body text, footnotes, figures, running headers, margin numbers and folios are written as separate zones, so selecting body text never drags in headers, page numbers or notes
- **Printed page numbers**: detects the book's own page numbers and writes them as PDF page labels (Roman-numeral front matter included), so Zotero and other readers cite the original pages; misread folios are corrected from neighbouring pages
- **Automatic bookmarks**: chapter and section headings become an outline
- **Complex layouts**: two-page landscape scans are split; multi-column pages copy column by column; figures and captions are grouped
- **Character-level alignment**: highlights sit on the scanned glyphs and stay continuous in browsers and Zotero
- **Proofreading pass**: page-by-page corrections against the image (`corrections.py`); punctuation set to full- or half-width by Chinese/English context
- **Zotero edition**: fixes Zotero's extra spaces at line breaks and broken highlights between English words

### Install

```bash
pip install rapidocr_onnxruntime pymupdf pymupdf-fonts numpy pillow
# plus poppler-utils (pdftotext)
```

The first run downloads the PP-OCRv6 medium recognition model (~77 MB); PP-OCRv5 server (~85 MB, used to restore em dashes) is fetched on demand. Both go to `models/`.

### Usage

```bash
mkdir WORK && echo '{"src": "book.pdf", "out": "book_ocr.pdf"}' > WORK/config.json
python3 zonedocr.py WORK images
python3 zonedocr.py WORK ocr
python3 zonedocr.py WORK classify
python3 zonedocr.py WORK proof      # proofread each page against the image into WORK/corrections.py
python3 zonedocr.py WORK build      # or: build --zotero
python3 zonedocr.py WORK check
```

See [SKILL.md](SKILL.md) for the full workflow, config options and design notes (in Chinese), and `corrections.example.py` for the corrections template. The folder also works as a skill for an AI agent, which can run the whole OCR → zoning → proofreading pipeline.

## License

MIT
