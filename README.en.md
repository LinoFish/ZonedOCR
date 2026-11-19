# ZonedOCR

**Body, footnotes, headers and printed page numbers — each in its own zone.**

[中文](README.md) · English


Ordinary OCR only makes a scanned book searchable. ZonedOCR understands the page layout. It turns scanned Chinese books (mixed with English, Simplified or Traditional) or English books into a PDF you can copy, search and highlight: the original scan stays untouched, with an invisible, character-aligned text layer on top, plus:

- **Smart zoning**: body text, footnotes, figures, running headers, margin numbers and folios are written as separate zones, so selecting body text never drags in headers, page numbers or notes
- **Printed page numbers**: detects the book's own page numbers and writes them as PDF page labels (Roman-numeral front matter included), so Zotero and other readers cite the original pages; misread folios are corrected from neighbouring pages
- **Automatic bookmarks**: chapter and section headings become an outline
- **Complex layouts**: two-page landscape scans are split; multi-column pages copy column by column; figures and captions are grouped
- **Character-level alignment**: highlights sit on the scanned glyphs and stay continuous in browsers and Zotero
- **Proofreading pass**: page-by-page corrections against the image (`corrections.py`); punctuation set to full- or half-width by Chinese/English context
- **English books**: detected automatically; footnote markers become superscripts, "Chapter 9" plus its title becomes one bookmark, and the Zotero edition never glues words across line ends
- **Zotero edition**: fixes Zotero's extra spaces at line breaks and broken highlights between English words

## Install

```bash
pip install rapidocr_onnxruntime pymupdf pymupdf-fonts numpy pillow
# plus poppler-utils (pdftotext)
```

The first run downloads the PP-OCRv6 medium recognition model (~77 MB); PP-OCRv5 server (~85 MB, used to restore em dashes) is fetched on demand. Both go to `models/`.

## Usage

One command does everything. It asks two questions up front — whether you want the Zotero edition and whether to detect the table of contents — and then runs unattended:

```bash
python3 zonedocr.py run book.pdf
# 要 Zotero 优化版吗？... [y/N] y        (Zotero edition?)
# 要识别目录大纲（章节书签）吗？ [Y/n]    (Detect TOC / bookmarks?)
# … 完成：book_work/book_OCR_Zotero.pdf
```

Skip the questions with flags: `--zotero` / `--no-zotero`, `--toc` / `--no-toc`.

For higher accuracy, proofread against the per-page text in `book_work/proof/`, write `corrections.py`, then run `python3 zonedocr.py book_work build`. The individual steps (images / ocr / classify / proof / build / check) are described in SKILL.md.

See [SKILL.md](SKILL.md) for the full workflow, config options and design notes (in Chinese), and `corrections.example.py` for the corrections template. The folder also works as a skill for an AI agent, which can run the whole OCR → zoning → proofreading pipeline.

## License

MIT
