#!/usr/bin/env python3
"""scanocr — 扫描版中文书页 → 分区隐形文字层 PDF（可复制/搜索/高亮）

用法（在作业目录 WORK 下放 config.json，可选 corrections.py）:
  python3 scanocr.py WORK images     # 抽出/渲染页面图
  python3 scanocr.py WORK ocr        # RapidOCR 行+逐字框 -> words.json（最慢，约1页/秒）
  python3 scanocr.py WORK classify   # 分区 -> lines.json，打印每页统计、疑似漏行、低置信行
  python3 scanocr.py WORK gaps       # 对疑似漏行空隙裁图重识别，打印 ADD 候选
  python3 scanocr.py WORK toc        # 预览自动识别的章节书签和页标签
  python3 scanocr.py WORK proof      # 写 proof/pNNN.txt 供逐页对图校对
  python3 scanocr.py WORK build [--zotero] [--debug] [--kinds body,note] [--out X.pdf]
  python3 scanocr.py WORK check [PDF] # 抽文字检查空格/标点/常见误识/校对规则命中
  python3 scanocr.py WORK overlay 2,13 [PDF]  # 渲染字符高亮框叠加图到 WORK/overlay/

依赖: pip install rapidocr_onnxruntime pymupdf pymupdf-fonts numpy pillow ; poppler-utils
"""
import sys, os, re, json, glob, statistics, difflib, importlib.util, subprocess, tempfile
from collections import Counter

WORK = os.path.abspath(sys.argv[1]); CMD = sys.argv[2]; ARGS = sys.argv[3:]
CFG = {
    "src": None,               # 原PDF路径（必填）
    "out": "out.pdf",          # 输出（相对 WORK）
    "page_offset": None,       # 印刷页码 = PDF页序 + page_offset；None = 由各页页码自动推算（页标签、补缺页码）
    "toc": True,               # 自动识别章节标题写入PDF书签
    "toc_patterns": [          # (正则, 层级)；另外：明显大于正文字号的行视为1级标题
        [r"^第[一二三四五六七八九十百零〇\d]+[部篇编]", 1],
        [r"^第[一二三四五六七八九十百零〇\d]+章", 1],
        [r"^第[一二三四五六七八九十百零〇\d]+节", 2],
        [r"^[一二三四五六七八九十百]+[　 、]\S", 2],
        [r"^\d+(\.\d+)*[　 ]\S", 2],
        [r"^[（(][一二三四五六七八九十]+[）)]\S", 3],
        [r"^\d+[.．、]\s*\S", 3]
    ],
    "header_band": 0.10,       # 页顶此比例内为书眉
    "folio_band": 0.90,        # 页底此比例以下、且≤4字符为页码
    "margin_regex": r"\d{3}",  # 版心外的边码（原书页码）
    "note_rule": True,         # 用脚注分隔短横线判定脚注区
    "auto_folio": True,         # 漏识别页码时按其它页页码的中位位置补一个；原书本无页码的页（如分部扉页）可关掉
    "rule_override": {},       # {"页序": y像素} 未检出分隔线时手工给
    "render_dpi": 300,         # 页面不是单张嵌入图时的渲染分辨率
}
CFG.update(json.load(open(os.path.join(WORK, 'config.json'))))
if CFG['src'] and not os.path.isabs(CFG['src']) and os.path.exists(os.path.join(WORK, CFG['src'])):
    CFG['src'] = os.path.join(WORK, CFG['src'])   # src 相对作业目录也可以
IMG = os.path.join(WORK, 'images')
J = lambda n: os.path.join(WORK, n)

def load_corr():
    p = J('corrections.py')
    if not os.path.exists(p):
        return {}, [], []
    spec = importlib.util.spec_from_file_location('corr', p); m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return getattr(m, 'R', {}), getattr(m, 'ADD', []), getattr(m, 'HEADER', [])

def bbox(b):
    xs = [p[0] for p in b]; ys = [p[1] for p in b]; return [min(xs), min(ys), max(xs), max(ys)]

# ---------------------------------------------------------------- images
def cmd_images():
    import pymupdf
    os.makedirs(IMG, exist_ok=True)
    doc = pymupdf.open(CFG['src'])
    for i, page in enumerate(doc, 1):
        ims = page.get_images()
        out = f'{IMG}/p{i:03d}.png'
        if len(ims) == 1:  # 单张整页扫描图：按原分辨率取出
            pix = pymupdf.Pixmap(doc, ims[0][0])
            if pix.n - pix.alpha >= 4: pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            pix.save(out)
            from PIL import Image
            im = Image.open(out)
            ref = im.height if im.width > im.height * 1.15 else max(im.size)   # 横向双页按高度算
            if ref > 3000:   # 600dpi 级大图缩到单页长边约2800px，OCR 更快、准确率不降
                r = 2800 / ref; im.convert('L').resize((round(im.width * r), round(im.height * r)), Image.LANCZOS).save(out)
        else:
            page.get_pixmap(dpi=CFG['render_dpi']).save(out)
    print('pages', len(doc))

def images():
    return sorted(glob.glob(f'{IMG}/p*.png'))

# ---------------------------------------------------------------- ocr
MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')
V5 = ('https://huggingface.co/bukuroo/PPOCRv5-ONNX/resolve/main/', 'ppocrv5-server-rec.onnx', 'ppocrv5_dict.txt')
V6 = 'https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_rec_onnx/resolve/main/'
DASH = '—―‒–-一'

def fetch_model(v):
    """v5：社区导出的 ONNX + 字典；v6：官方 ONNX，字典从 inference.yml 抽出。返回 (onnx, dict)"""
    os.makedirs(MODELS, exist_ok=True)
    if v == 'v5':
        for f in V5[1:]:
            if not os.path.exists(f'{MODELS}/{f}'): subprocess.run(['curl', '-sL', '-o', f'{MODELS}/{f}', V5[0] + f], check=True)
        return f'{MODELS}/{V5[1]}', f'{MODELS}/{V5[2]}'
    onnx, yml, dic = (f'{MODELS}/ppocrv6-medium-rec.{e}' for e in ('onnx', 'yml', 'txt'))
    for f, u in ((onnx, 'inference.onnx'), (yml, 'inference.yml')):
        if not os.path.exists(f): subprocess.run(['curl', '-sL', '-o', f, V6 + u], check=True)
    if not os.path.exists(dic):
        import yaml
        open(dic, 'w').write('\n'.join(yaml.safe_load(open(yml))['PostProcess']['character_dict']) + '\n')
    return onnx, dic

def make_engine(v=None):
    """识别模型默认 PP-OCRv6 medium（简繁英日一个模型；比 v5 server 少约7%错字，但会漏掉破折号，cmd_ocr 用 v5 补回）。
    config rec:'v5' 用 PP-OCRv5 server；'v4' 用 RapidOCR 自带模型（繁体大量丢字、转简）。检测模型都用 RapidOCR 自带的。"""
    from rapidocr_onnxruntime import RapidOCR
    v = v or CFG.get('rec', 'v6')
    if v not in ('v5', 'v6'): return RapidOCR()
    m, d = fetch_model(v)
    return RapidOCR(rec_model_path=m, rec_keys_path=d)

def dash_gaps(row, img_w):
    """v6 识别时常把破折号整个丢掉，只在逐字框里留下一段空白。返回可疑空白处的字序号（-1=行首，n-1=行尾）"""
    cb, txt = row['cboxes'], row['text']
    if len(cb) != len(txt) or len(cb) < 3: return []
    ws = sorted(b - a for a, b in cb if b > a); cw = ws[len(ws) // 2] if ws else 0
    if cw <= 0: return []
    b = bbox(row['box']); out = []
    for i in range(len(cb) - 1):
        if cb[i + 1][0] - cb[i][1] > 1.2 * cw and re.match(CJK + '|[，。、；：「」『』（）]', txt[i] + txt[i + 1][:0]): out.append(i)
    if cb[0][0] - b[0] > 1.5 * cw: out.append(-1)
    if b[2] - cb[-1][1] > 1.5 * cw: out.append(len(cb) - 1)
    return out

def patch_dashes(row, gaps, v5txt):
    """把 v5 在同一行同一位置识出的破折号插回 v6 结果；只插破折号，别的字仍以 v6 为准"""
    txt, cb, ch = row['text'], row['cboxes'], row['chars']; ins = {}
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, txt, v5txt, autojunk=False).get_opcodes():
        seg = v5txt[b1:b2]
        if op == 'insert' and seg and all(c in DASH for c in seg) and (a1 - 1) in gaps:
            ins[a1 - 1] = seg
    ws = sorted(b - a for a, b in cb if b > a); cw = ws[len(ws) // 2]
    for i in sorted(ins, reverse=True):
        x0 = cb[i][1] if i >= 0 else bbox(row['box'])[0]; x1 = cb[i + 1][0] if i + 1 < len(cb) else bbox(row['box'])[2]
        ins[i] = '——' if x1 - x0 > 1.5 * cw else '—'   # 中文破折号占两字宽；v5 也常只识出一半或识成"一一"
        n = len(ins[i]); step = (x1 - x0) / n
        txt = txt[:i + 1] + ins[i] + txt[i + 1:]
        cb[i + 1:i + 1] = [[x0 + k * step, x0 + (k + 1) * step] for k in range(n)]
        ch[i + 1:i + 1] = list(ins[i])
    row['text'] = txt
    return bool(ins)

def cmd_ocr():
    from PIL import Image
    import numpy as np
    eng = make_engine(); v6 = CFG.get('rec', 'v6') == 'v6'; e5 = None; out = {}; fixed = 0
    for i, p in enumerate(images(), 1):
        res, _ = eng(p, return_word_box=True)
        rows = []
        for r in res or []:
            boxes = r[3] if len(r) > 3 else []; chars = r[4] if len(r) > 4 else []
            rows.append({'box': [[float(a) for a in pt] for pt in r[0]], 'text': r[1], 'conf': float(r[2]),
                         'cboxes': [[min(q[0] for q in b), max(q[0] for q in b)] for b in boxes], 'chars': list(chars)})
        if v6 and CFG.get('dash_fix', True):
            im = None
            for row in rows:
                g = dash_gaps(row, 0)
                if not g: continue
                if e5 is None: e5 = make_engine('v5')
                if im is None: im = Image.open(p).convert('RGB')
                b = bbox(row['box']); h = b[3] - b[1]
                crop = np.array(im.crop((max(0, b[0] - h * .3), b[1], b[2] + h * .3, b[3])))[:, :, ::-1]
                r5, _ = e5(crop, use_det=False, use_cls=False)
                if r5: fixed += patch_dashes(row, g, r5[0][0])
        out[i] = rows; print(i, len(rows), flush=True)
        json.dump(out, open(J('words.json'), 'w'), ensure_ascii=False)
    if v6: print('v5 补回破折号的行', fixed)

# ---------------------------------------------------------------- classify
def find_rule(im, H, W):
    import numpy as np
    x0, x1 = int(W * .08), int(W * .94)
    for y in range(int(H * .52), int(H * .92)):
        row = im[y, x0:x1].astype(np.int8)
        if row.sum() < W * .08: continue
        d = np.diff(np.concatenate([[0], row, [0]]))
        L = (np.where(d == -1)[0] - np.where(d == 1)[0]).max()
        if W * .10 < L < W * .32 and row.sum() < L + 40 and im[y-6, x0:x1].sum() < 80 and im[y+6, x0:x1].sum() < 80:
            return y
    return None

def split_margin(im, t, b, W, rx):
    """把粘在正文行首/行尾的边码数字拆出来（靠像素空白列找分界）"""
    x0, y0, x1, y1 = b; gapmin = W * .014
    m1 = re.match(rf'^({rx})(\D.*)$', t); m2 = re.match(rf'^(.*\D)({rx})$', t)
    if m1 and x0 < W * .13:
        cols = im[int(y0):int(y1), int(x0):int(x0 + W * .12)].sum(0); st = False
        for i, c in enumerate(cols):
            st = st or c > 0
            if st and c == 0:
                j = i
                while j < len(cols) and cols[j] == 0: j += 1
                if j - i > gapmin: return [(m1.group(1), [x0, y0, x0 + i, y1]), (m1.group(2), [x0 + j, y0, x1, y1])]
    if m2 and x1 > W * .89:
        w = int(W * .12); seg = im[int(y0):int(y1), int(x1) - w:int(x1)].sum(0); st = False
        for i in range(len(seg) - 1, -1, -1):
            st = st or seg[i] > 0
            if st and seg[i] == 0:
                j = i
                while j >= 0 and seg[j] == 0: j -= 1
                if i - j > gapmin: return [(m2.group(1), [x0, y0, x1 - w + j + 1, y1]), (m2.group(2), [x1 - w + i + 1, y0, x1, y1])]
    return [(t, b)]

# ---------------------------------------------------------------- layout：横向双页、分栏、图表
def cover(boxes, x0, x1):
    import numpy as np
    c = np.zeros(max(int(x1 - x0), 1), int)
    for b in boxes:
        a, z = int(max(b[0], x0) - x0), int(min(b[2], x1) - x0)
        if z > a: c[a:z] += 1
    return c

def zero_runs(c):
    runs, i = [], 0
    while i < len(c):
        if c[i] == 0:
            j = i
            while j < len(c) and c[j] == 0: j += 1
            runs.append((i, j)); i = j
        else: i += 1
    return runs

def find_spread(boxes, W, H):
    """横向双页（页宽>1.15倍页高）：在中间 35%–65% 找没有文字行跨过的竖条当中缝。
    用 OCR 行框而不是像素，装订阴影不影响。config spread: "auto"|true|false"""
    sp = CFG.get('spread', 'auto')
    if sp is False or (sp == 'auto' and W < H * 1.15): return [[0, W]]
    lo, hi = int(W * .35), int(W * .65); c = cover(boxes, lo, hi); runs = zero_runs(c)
    if runs:
        a, b = max(runs, key=lambda r: (r[1] - r[0]) - abs((r[0] + r[1]) / 2 + lo - W / 2) * .3)
        g = lo + (a + b) // 2
    else:
        g = lo + int(c.argmin())
    return [[0, g], [g, W]]

def split_row(r, gx):
    """OCR 把跨中缝/栏间空白的两段识成一行时，按逐字框在 gx 处拆开"""
    cb, ch = r.get('cboxes') or [], r.get('chars') or []
    if len(cb) != len(ch) or len(cb) < 2: return [r]
    k = next((i for i in range(1, len(cb)) if cb[i - 1][1] <= gx + 8 and cb[i][0] >= gx - 8 and cb[i][0] - cb[i - 1][1] > 4), None)
    if k is None: return [r]
    t, pos = r['text'], 0
    for it in ch[:k]:
        j = t.find(it, pos); pos = (j if j >= 0 else pos) + len(it)
    b = bbox(r['box'])
    mk = lambda txt, c, h, x0, x1: dict(r, box=[[x0, b[1]], [x1, b[1]], [x1, b[3]], [x0, b[3]]], text=txt.strip(), cboxes=c, chars=h)
    return [mk(t[:pos], cb[:k], ch[:k], b[0], cb[k - 1][1]), mk(t[pos:], cb[k:], ch[k:], cb[k][0], b[2])]

def find_gutters(boxes, rx0, rx1, H, lh):
    """分栏：去掉通栏行后，正文行横向投影里的空白竖条；两侧都要有足够多、竖向铺开的行。config columns: "auto"|1"""
    if CFG.get('columns', 'auto') in (1, False): return []
    rw = rx1 - rx0; cand = [b for b in boxes if b[2] - b[0] < .55 * rw]
    if len(cand) < 8 or len(cand) < .5 * len(boxes): return []
    lo = min(b[0] for b in cand); hi = max(b[2] for b in cand); c = cover(cand, lo, hi); gs = []
    thr = max(1, int(.08 * len(cand)))   # 允许少数居中的短标题、识别时粘连的行跨过栏缝
    for a, b in zero_runs((c > thr).astype(int)):
        if b - a < .4 * lh or a == 0 or b >= len(c): continue
        g = lo + (a + b) / 2
        L = [x for x in cand if x[2] <= g]; R = [x for x in cand if x[0] >= g]
        span = lambda S: max(x[3] for x in S) - min(x[1] for x in S) if S else 0
        if len(L) >= .2 * len(cand) and len(R) >= .2 * len(cand) and span(L) > .15 * H and span(R) > .15 * H:
            gs.append(round(g))
    return gs

def find_figures(im, boxes, rx0, rx1, H, lh):
    """图表：把文字行框涂白后剩下的大块墨迹（框线、箭头、图片）。有贯通横线且框内有≥2行字=表格，否则=插图。
    另外：≥2条等宽长横线夹着多格文字行=三线表。返回 [{'box':[x0,y0,x1,y1],'type':'table'|'figure'}]"""
    import numpy as np
    m = im[:, rx0:rx1].copy(); hh, ww = m.shape
    e = int(1.5 * lh)   # 横向多涂一点：OCR 行框常比实际字迹短（斜体、行尾标点），残字不能当成图
    for b in boxes:
        m[max(0, int(b[1]) - 6):int(b[3]) + 6, max(0, int(b[0]) - rx0 - e):max(0, int(b[2]) - rx0 + e)] = False
    f = 8; h, w = hh // f, ww // f
    g = m[:h * f, :w * f].reshape(h, f, w, f).sum(axis=(1, 3)) > 2
    g2 = g.copy(); g2[1:] |= g[:-1]; g2[:-1] |= g[1:]; g2[:, 1:] |= g[:, :-1]; g2[:, :-1] |= g[:, 1:]
    seen = np.zeros_like(g2); comps = []
    for y in range(h):
        for x in range(w):
            if g2[y, x] and not seen[y, x]:
                st = [(y, x)]; seen[y, x] = True; ys, xs = [], []
                while st:
                    a, b = st.pop(); ys.append(a); xs.append(b)
                    for c, d in ((a + 1, b), (a - 1, b), (a, b + 1), (a, b - 1)):
                        if 0 <= c < h and 0 <= d < w and g2[c, d] and not seen[c, d]: seen[c, d] = True; st.append((c, d))
                comps.append([min(xs) * f + rx0, min(ys) * f, (max(xs) + 1) * f + rx0, (max(ys) + 1) * f])
    rw = rx1 - rx0; figs, rules = [], []
    inside = lambda bx, b: bx[0] - 5 <= (b[0] + b[2]) / 2 <= bx[2] + 5 and bx[1] - 5 <= (b[1] + b[3]) / 2 <= bx[3] + 5
    for c in comps:
        bw, bh = c[2] - c[0], c[3] - c[1]
        if c[3] < H * CFG['header_band'] or c[1] > H * CFG['folio_band']: continue
        if bh > .85 * H or c[0] - rx0 < .01 * rw or rx1 - c[2] < .01 * rw: continue   # 扫描黑边、装订阴影
        if bw > .25 * rw and bh <= 4 * f: rules.append(c); continue
        if bw < .12 * rw or bh < 2.5 * lh: continue
        def runs(a):   # 每行最长连续墨迹
            out = []
            for row in a:
                d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
                s, e = np.where(d == 1)[0], np.where(d == -1)[0]
                out.append((e - s).max() if len(s) else 0)
            return np.array(out)
        sub = im[c[1]:c[3], c[0]:c[2]]; msub = m[c[1]:c[3], c[0] - rx0:c[2] - rx0]
        hr = runs(sub); longest = hr.max()
        resid = msub[hr < .6 * bw].sum() / max(bw * bh, 1); vlong = runs(msub.T).max() if msub.size else 0
        if vlong < .4 * bh and longest < .5 * bw and bh < 3.5 * lh: continue   # 一两行没识别出来的字，不是图（gaps 会报漏行）
        if vlong < .4 * bh and resid < .03:   # 只有长横线+几点残字（如通栏脚注线）：不是图表
            if longest > .25 * rw: rules.append(c)
            continue
        ntext = sum(1 for b in boxes if inside(c, b))
        figs.append({'box': c, 'type': 'table' if longest > .8 * bw and ntext >= 2 else 'figure'})
    rules.sort(key=lambda c: c[1]); i = 0   # 三线表
    while i < len(rules):
        j = i
        while j + 1 < len(rules) and rules[j + 1][1] - rules[j][3] < 12 * lh and \
                min(rules[j + 1][2], rules[i][2]) - max(rules[j + 1][0], rules[i][0]) > .8 * (rules[i][2] - rules[i][0]): j += 1
        if j > i:
            box = [min(r[0] for r in rules[i:j + 1]), rules[i][1], max(r[2] for r in rules[i:j + 1]), rules[j][3]]
            tb = [b for b in boxes if inside(box, b)]
            rowsn = Counter(round((b[1] + b[3]) / 2 / lh) for b in tb)
            if tb and max(rowsn.values()) >= 2 and not any(inside(fg['box'], [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2] * 2) for fg in figs):
                figs.append({'box': box, 'type': 'table'})
        i = j + 1
    return figs

CAPTION = r'^\s*(圖|图|表|Fig\.?|Figure|Table|Chart|插圖|插图)\s*[\d一二三四五六七八九十〇]'

def layout_lines(p):
    """给每行标上 ri(哪一页)、ci(哪一栏，-1=通栏)、seg(被通栏行分开的段)、fi(哪个图表)、容器左右边界 bx"""
    regs = p.get('regions') or [[0, p['w']]]; cols = p.get('cols') or [[r] for r in regs]; figs = p.get('figs') or []
    for l in p['lines']:
        if l['kind'] in ('table', 'figure', 'caption'): l['kind'] = 'body'   # 图表归属每次重算
        b = l['box']; cx = (b[0] + b[2]) / 2; cy = (b[1] + b[3]) / 2
        ri = next((i for i, r in enumerate(regs) if r[0] <= cx < r[1]), len(regs) - 1); cs = cols[ri]
        lh = b[3] - b[1]   # 归入重叠最多的一栏；跨进别栏不到一个半字（OCR 框外扩）也算本栏，否则是通栏
        ov = [min(b[2], c[1]) - max(b[0], c[0]) for c in cs]; k = max(range(len(cs)), key=lambda i: ov[i])
        ci = k if len(cs) == 1 or ov[k] >= (b[2] - b[0]) - 1.5 * lh else None
        l['ri'] = ri; l['ci'] = -1 if ci is None else ci
        l['bx'] = regs[ri] if ci is None else cs[ci]
        fi = next((i for i, fg in enumerate(figs) if fg['box'][0] - 8 <= cx <= fg['box'][2] + 8 and fg['box'][1] - 8 <= cy <= fg['box'][3] + 8), None)
        if fi is not None and l['kind'] in ('body', 'note'): l['kind'] = figs[fi]['type']
        l['fi'] = fi if fi is not None else -1
    lh = statistics.median([l['box'][3] - l['box'][1] for l in p['lines'] if l['kind'] == 'body'] or [40])
    for fi, fg in enumerate(figs):   # 紧贴图下/图上、落在图宽之内的短行（坐标轴说明、图例）算图的一部分；图注除外
        fb = list(fg['box']); changed = True
        while changed:
            changed = False
            for l in p['lines']:
                b = l['box']
                if fg['type'] != 'figure' or l['kind'] != 'body' or re.match(CAPTION, l.get('text', l['ocr'])): continue
                if abs((b[0] + b[2]) / 2 - (fb[0] + fb[2]) / 2) < .2 * (fb[2] - fb[0]) and b[2] - b[0] < .6 * (fb[2] - fb[0]) and \
                        (0 <= b[1] - fb[3] < 1.6 * lh or 0 <= fb[1] - b[3] < 1.6 * lh):
                    l['kind'] = fg['type']; l['fi'] = fi; fb = [fb[0], min(fb[1], b[1]), fb[2], max(fb[3], b[3])]; changed = True
        fg['ext'] = fb
    for l in p['lines']:   # 图表上下紧挨着、以"圖/表/Figure"开头的短行=图注
        if l['kind'] == 'body' and figs and re.match(CAPTION, l.get('text', l['ocr'])):
            near = [i for i, fg in enumerate(figs) if min(abs(l['box'][1] - fg.get('ext', fg['box'])[3]), abs(fg.get('ext', fg['box'])[1] - l['box'][3])) < 2.5 * lh]
            if near: l['kind'] = 'caption'; l['fi'] = near[0]
    for kind in ('body', 'note'):   # 通栏行把分栏切成上下段
        for ri in range(len(regs)):
            fw = sorted((l['box'][1] + l['box'][3]) / 2 for l in p['lines'] if l['kind'] == kind and l['ri'] == ri and l['ci'] == -1)
            for l in p['lines']:
                if l['kind'] == kind and l['ri'] == ri:
                    yc = (l['box'][1] + l['box'][3]) / 2
                    l['seg'] = sum(1 for y in fw if (y <= yc if l['ci'] == -1 else y < yc))
    return p

def reading_order(ls):
    """阅读顺序：先按 (页, 段, 栏 / 图表) 分组，组内按行聚类（同一行的碎片按 x），行间自上而下"""
    def gkey(l):
        if l['kind'] in ('table', 'figure', 'caption'): return (l.get('ri', 0), l.get('fi', -1), 0, 0)
        return (l.get('ri', 0), l.get('seg', 0), l.get('ci', 0), 0)
    out = []; groups = {}
    for l in ls: groups.setdefault(gkey(l), []).append(l)
    for k in sorted(groups):
        g = sorted(groups[k], key=lambda l: (l['box'][1] + l['box'][3]) / 2); rows = []
        for l in g:
            yc = (l['box'][1] + l['box'][3]) / 2; h = l['box'][3] - l['box'][1]
            if rows and abs(rows[-1][0] - yc) < .45 * min(h, rows[-1][1]): rows[-1][2].append(l)
            else: rows.append([yc, h, [l]])
        for r in rows: out += sorted(r[2], key=lambda l: l['box'][0])
    return out

def cmd_classify():
    import numpy as np
    from PIL import Image
    Wd = json.load(open(J('words.json'))); pages = {}
    for pg, path in enumerate(images(), 1):
        im = np.array(Image.open(path).convert('L')) < 128; H, W = im.shape
        rows = Wd[str(pg)]
        regs = find_spread([bbox(r['box']) for r in rows], W, H)
        if len(regs) > 1:
            rows = [s for r in rows for s in split_row(r, regs[0][1])]
        lh = statistics.median([bbox(r['box'])[3] - bbox(r['box'])[1] for r in rows] or [40])
        lines, cols, figs, rules = [], [], [], []
        for ri, (rx0, rx1) in enumerate(regs):
            rw = rx1 - rx0
            rrows = [r for r in rows if rx0 <= (bbox(r['box'])[0] + bbox(r['box'])[2]) / 2 < rx1]
            wide = [bbox(r['box']) for r in rrows if bbox(r['box'])[2] - bbox(r['box'])[0] > rw * .6]
            LL = float(np.median([b[0] for b in wide])) if wide else rx0 + rw * .15
            RR = float(np.median([b[2] for b in wide])) if wide else rx0 + rw * .85
            inner = [bbox(r['box']) for r in rrows if H * CFG['header_band'] < bbox(r['box'])[1] and bbox(r['box'])[3] < H * CFG['folio_band']]
            rfigs = find_figures(im, [bbox(r['box']) for r in rrows], rx0, rx1, H, lh) if CFG.get('figures', True) else []
            infig = lambda b: any(f['box'][0] - 8 <= (b[0] + b[2]) / 2 <= f['box'][2] + 8 and f['box'][1] - 8 <= (b[1] + b[3]) / 2 <= f['box'][3] + 8 for f in rfigs)
            def tight(r):   # 用逐字框收紧行框（OCR 行框常向外多出半个字）
                b = bbox(r['box']); cb = r.get('cboxes') or []
                return [min(c[0] for c in cb), b[1], max(c[1] for c in cb), b[3]] if cb else b
            tb = [tight(r) for r in rrows if H * CFG['header_band'] < bbox(r['box'])[1] and bbox(r['box'])[3] < H * CFG['folio_band']]
            gs = find_gutters([b for b in tb if not infig(b)], rx0, rx1, H, lh)
            for g in gs: rrows = [s for r in rrows for s in split_row(r, g)]
            edges = [rx0] + gs + [rx1]; cols.append([[a, b] for a, b in zip(edges, edges[1:])]); figs += rfigs
            nf = im[:, rx0:rx1].copy()
            for f in rfigs: nf[f['box'][1] - 10:f['box'][3] + 10, max(0, f['box'][0] - rx0 - 10):f['box'][2] - rx0 + 10] = False   # 表格横线不是脚注线
            rule = CFG['rule_override'].get(str(pg) if ri == 0 else f'{pg}.{ri}') or \
                (find_rule(nf, H, rw) if CFG['note_rule'] else None)
            rules.append(rule)
            rl = []
            for r in rrows:
                for t, b in split_margin(im[:, rx0:rx1], r['text'], [v - (rx0 if i % 2 == 0 else 0) for i, v in enumerate(bbox(r['box']))], rw, CFG['margin_regex']):
                    b = [b[0] + rx0, b[1], b[2] + rx0, b[3]]
                    if b[3] < H * CFG['header_band']: k = 'header'
                    elif b[1] > H * CFG['folio_band'] and len(t) <= 4:
                        k = 'folio'
                        if CFG['page_offset'] is not None: t = str(pg + CFG['page_offset'])
                    elif re.fullmatch(CFG['margin_regex'], t) and (b[0] > RR - 40 or b[2] < LL + 20): k = 'margin'
                    elif rule and b[1] > rule: k = 'note'
                    else: k = 'body'
                    rl.append({'kind': k, 'box': [round(v) for v in b], 'ocr': t, 'conf': round(r.get('conf', 1), 3)})
            if not rule and CFG['note_rule']:   # 没找到分隔线：每栏页底一段以"数字+空格"开头、上方有空行的行块当脚注
                for c0, c1 in cols[-1]:
                    body = sorted([l for l in rl if l['kind'] == 'body' and c0 - 10 <= l['box'][0] and l['box'][2] <= c1 + 10 and not infig(l['box'])], key=lambda l: l['box'][1])
                    pitch = [b['box'][1] - a['box'][1] for a, b in zip(body, body[1:])]
                    mp = statistics.median(pitch) if pitch else 0
                    for k in range(len(body) - 1, 0, -1):
                        l = body[k]
                        if l['box'][1] < H * .6: break
                        if re.match(r'^\d{1,3}(?![.．、\d])\s*\S', l['ocr']) and l['box'][1] - body[k - 1]['box'][1] > 1.6 * mp:
                            rules[-1] = (body[k - 1]['box'][3] + l['box'][1]) // 2
                            for m in body[k:]: m['kind'] = 'note'
                            break
            lines += rl
        p = layout_lines({'w': W, 'h': H, 'rule_y': rules[0], 'rules': rules, 'regions': regs, 'cols': cols, 'figs': figs, 'lines': lines})
        pages[pg] = p
        # 疑似漏行：同一栏里正文相邻行距明显大于中位行距（图表处除外）
        gaps = []
        for ri, cs in enumerate(cols):
            for ci in range(len(cs)):
                body = sorted([l for l in lines if l['kind'] == 'body' and l['ri'] == ri and l['ci'] == ci], key=lambda l: l['box'][1])
                pitch = [b['box'][1] - a['box'][1] for a, b in zip(body, body[1:])]
                mp = statistics.median(pitch) if pitch else 0
                gaps += [(a['box'][3], b['box'][1]) for a, b in zip(body, body[1:]) if mp and b['box'][1] - a['box'][1] > 1.45 * mp
                         and not any(f['box'][1] < b['box'][1] and f['box'][3] > a['box'][3] for f in figs)]
        low = [l['ocr'][:12] for l in lines if l['conf'] < .9]
        lay = ('双页 ' if len(regs) > 1 else '') + ('/'.join(f'{len(c)}栏' for c in cols) if any(len(c) > 1 for c in cols) else '') + \
              ''.join(f" {f['type']}{f['box']}" for f in figs)
        for l in lines: [l.pop(k, None) for k in ('bx',)]
        print(pg, 'rule', rules if len(rules) > 1 else rules[0], lay, dict(Counter(l['kind'] for l in lines)), 'GAPS' if gaps else '', gaps or '', 'LOW' if low else '', low or '')
    json.dump(pages, open(J('lines.json'), 'w'), ensure_ascii=False, indent=1)
    print('注意：GAPS 可能是漏识别的行（也可能是标题/段间距），对图核实后写入 corrections.ADD；无 rule 但有脚注的页写 rule_override（双页右页用 "页.1"）')

def cmd_gaps():
    """对正文行间的大空隙裁图重识别，打印候选行（格式即 corrections.ADD 条目），人工确认后再加"""
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR
    eng = make_engine(); P = json.load(open(J('lines.json')))
    for pg_s, p in P.items():
        im = Image.open(images()[int(pg_s) - 1]).convert('L'); figs = p.get('figs') or []
        for key in sorted({(l.get('ri', 0), l.get('ci', 0)) for l in p['lines'] if l['kind'] == 'body'}):   # 每栏分别找
          b = sorted([l for l in p['lines'] if l['kind'] == 'body' and (l.get('ri', 0), l.get('ci', 0)) == key], key=lambda l: l['box'][1])
          if len(b) < 3: continue
          LL = min(l['box'][0] for l in b) - 30; RR = max(l['box'][2] for l in b) + 20
          rules = p.get('rules') or [p['rule_y']]; ry = rules[key[0]] if key[0] < len(rules) else None
          pairs = list(zip(b, b[1:])) + [(b[-1], {'box': [0, ry or int(p['h'] * CFG['folio_band']), 0, 0]})]
          for a, c in pairs:
            y0 = a['box'][3] + 8; y1 = c['box'][1] - 8
            if any(f['box'][1] < y1 and f['box'][3] > y0 and f['box'][0] < RR and f['box'][2] > LL for f in figs): continue
            if y1 - y0 < 30 or (np.array(im.crop((LL, y0, RR, y1))) < 128).sum() < 300: continue
            crop = im.crop((LL - 20, y0 - 6, RR + 20, y1 + 6))
            can = Image.new('L', (crop.width + 80, crop.height + 80), 255); can.paste(crop, (40, 40)); can.save(os.path.join(tempfile.gettempdir(), '_gap.png'))
            for r in eng(os.path.join(tempfile.gettempdir(), '_gap.png'))[0] or []:
                xs = [q[0] for q in r[0]]; ys = [q[1] for q in r[0]]
                bb = [round(min(xs) + LL - 60), round(min(ys) + y0 - 46), round(max(xs) + LL - 60), round(max(ys) + y0 - 46)]
                if bb[3] - bb[1] >= 25: print(f"({pg_s}, 'body', {bb}, '{r[1]}'),")

# ---------------------------------------------------------------- 校对与标点
CJK = r'[\u3000-\u303f\u3400-\u9fff\uff00-\uffef“”‘’《》—…]'
H2F = {',': '，', ';': '；', ':': '：', '?': '？', '!': '！', '(': '（', ')': '）'}
F2H = {'，': ',', '；': ';', '：': ':', '．': '.', '？': '?', '！': '!'}

def normalize(t):
    """中文语境用全角标点、英文语境用半角；去掉中文字之间的空格"""
    t = re.sub(rf'(?<={CJK})[ \t]+(?={CJK})', '', t)
    out = []
    for i, ch in enumerate(t):
        prev = t[i-1] if i else ' '; nxt = t[i+1] if i + 1 < len(t) else ' '
        if ch in H2F and (re.match(CJK, prev) or re.match(CJK, nxt)): ch = H2F[ch]
        elif ch in F2H and re.match(r'[A-Za-z0-9]', prev) and re.match(r'[A-Za-z0-9]', nxt): ch = F2H[ch] + ' '
        out.append(ch)
    t = ''.join(out)
    # 括号配对：开括号是全角，闭括号也用全角（"（太5.3)"），反之亦然
    st, o = [], list(t)
    for i, ch in enumerate(o):
        if ch in '(（': st.append(ch)
        elif ch in ')）' and st: o[i] = '）' if st.pop() == '（' else ')'
    t = ''.join(o)
    return re.sub(r'(?<=[A-Za-z]),(?=[A-Za-z])', ', ', t)

def apply_rules(rules, t):
    for a, b in rules:
        t = re.sub(a[3:], b, t) if a.startswith('re:') else t.replace(a, b)
    return t

def final_pages():
    P = json.load(open(J('lines.json'))); R, ADD, HEADER = load_corr(); pages = {}
    for pg_s, p in P.items():
        pg = int(pg_s); lines = [dict(l) for l in p['lines']]
        for (apg, kind, box, text) in ADD:
            if apg != pg: continue
            # 紧贴在某行左/右侧的补字（如首字下沉）并进那一行，避免阅读器在中间插空格/换行
            near = [l for l in lines if l['kind'] == kind and min(box[3], l['box'][3]) - max(box[1], l['box'][1]) > 0
                    and (0 <= l['box'][0] - box[2] < 40 or 0 <= box[0] - l['box'][2] < 40)]
            if near:
                l = near[0]; left = box[2] <= l['box'][0]
                l['ocr'] = text + l['ocr'] if left else l['ocr'] + text
                l['box'] = [min(box[0], l['box'][0]), l['box'][1], max(box[2], l['box'][2]), l['box'][3]]
            else:
                lines.append({'kind': kind, 'box': box, 'ocr': text, 'conf': 1})
        for l in lines:
            t = l['ocr']
            if l['kind'] == 'header':
                for pat, rep in HEADER:
                    if re.search(pat, t): t = rep; break
            elif l['kind'] in ('body', 'note', 'table', 'figure', 'caption'):
                t = apply_rules(R.get(0, []), t)   # R[0]=全书通用规则，先做；页规则在其后，可以把个别处改回
                t = apply_rules(R.get(pg, []), t)
                t = normalize(t)
                if l['kind'] == 'note': t = re.sub(r'^(\d{1,3})(?![\d])[\s　]*(?=\S)', r'\1　', t)   # 脚注号后全角空格；(?![\d])防回溯把独立成行的两位数号拆成“2　3”
            l['text'] = t
        pages[pg] = layout_lines({'w': p['w'], 'h': p['h'], 'lines': lines, 'rule_y': p.get('rule_y'),
                                  'regions': p.get('regions'), 'cols': p.get('cols'), 'figs': p.get('figs', [])})
        for key in {(l['ri'], l['ci']) for l in lines if l['kind'] in ('body', 'note')}:   # 每栏右边界（Zotero 行尾连字符用）
            xs = sorted(l['box'][2] for l in lines if l['kind'] in ('body', 'note') and (l['ri'], l['ci']) == key)
            for l in lines:
                if (l['ri'], l['ci']) == key: l['colR'] = xs[int(len(xs) * .9)] if len(xs) >= 4 else None
        alltxt = '\n'.join(apply_rules(R.get(0, []), l['ocr']) for l in lines)
        for a, b in R.get(pg, []):
            hit = re.search(a[3:], alltxt, re.M) if a.startswith('re:') else a in alltxt
            if not hit: print('MISS 校对规则未命中', pg, a)
    lab, _ = page_labels(pages)   # 页码文字统一成推算出的印刷页码（修正误识）
    single = [p for p in pages.values() if len(p.get('regions') or [0]) == 1]
    fb = [l['box'] for p in single for l in p['lines'] if l['kind'] == 'folio']
    for pg, p in pages.items():   # 漏识别页码的单页：按其它页页码的中位位置补一个（双页不补）；auto_folio=false 时不补（原书本无页码的扉页/分部页）
        if CFG.get('auto_folio', True) and fb and pg in lab and p in single and not any(l['kind'] == 'folio' for l in p['lines']):
            box = [int(statistics.median(b[i] for b in fb)) for i in range(4)]
            p['lines'].append({'kind': 'folio', 'box': box, 'ocr': '', 'text': '', 'conf': 1, 'auto': True, 'ri': 0, 'ci': 0, 'seg': 0, 'fi': -1,
                               'bx': (p.get('regions') or [[0, p['w']]])[0]})
    for pg, p in pages.items():
        if pg in lab:
            st, n0, _ = lab[pg]
            for l in p['lines']:
                n = n0 + l.get('ri', 0)
                if l['kind'] == 'folio' and n >= 1:
                    l['text'] = str(n) if st == 'D' else to_roman(n)
    return pages

WRITE_ORDER = ['body', 'note', 'figs', 'header', 'margin', 'folio']   # 写入顺序=选择顺序；figs=每个图表连同图注一组
KINDS = {'figs': ('caption', 'table', 'figure')}
of_kind = lambda ls, kind: [l for l in ls if l['kind'] in KINDS.get(kind, (kind,))]

def cmd_proof():
    os.makedirs(J('proof'), exist_ok=True)
    for pg, p in final_pages().items():
        with open(J(f'proof/p{pg:03d}.txt'), 'w') as f:
            for kind in WRITE_ORDER:
                for l in reading_order(of_kind(p['lines'], kind)):
                    tag = f"{l['kind']:7}" + (f"p{l['ri'] + 1}" if len(p.get('regions') or [0]) > 1 else '') + (f"c{l['ci'] + 1}" if l['ci'] >= 0 and len((p.get('cols') or [[0]])[l['ri']]) > 1 else '')
                    f.write(f"{tag:12} {l['conf']:.2f} {l['box']} {l['text']}\n")
    print('写入', J('proof'))

# ---------------------------------------------------------------- build
ROMAN = {'i': 1, 'v': 5, 'x': 10, 'l': 50, 'c': 100}
def roman(t):
    t = t.lower(); n = 0
    for i, ch in enumerate(t):
        v = ROMAN[ch]; n += -v if i + 1 < len(t) and ROMAN[t[i + 1]] > v else v
    return n

def to_roman(n):
    out = ''
    for v, r in [(100, 'c'), (90, 'xc'), (50, 'l'), (40, 'xl'), (10, 'x'), (9, 'ix'), (5, 'v'), (4, 'iv'), (1, 'i')]:
        while n >= v: out += r; n -= v
    return out

def page_labels(pages):
    """各页印刷页码 → 页标签。横向双页标成"12–13"。按"已过的书页数"推算偏移，误识/缺页按前后页纠正"""
    cum, k, c = {}, {}, 0
    for pg in sorted(pages):
        cum[pg] = c; k[pg] = len(pages[pg].get('regions') or [0]); c += k[pg]
    seen = {}
    for pg, p in pages.items():
        for ri in range(k[pg]):
            inr = lambda l: l.get('ri', 0) == ri
            cands = [l for l in p['lines'] if l['kind'] == 'folio' and inr(l)] + \
                    [l for l in p['lines'] if l['kind'] == 'header' and inr(l) and re.fullmatch(r'\d{1,4}|[ivxlcIVXLC]{1,7}', l['ocr'].strip())]
            got = None
            for l in cands:
                t = l['ocr'].strip()
                if re.fullmatch(r'\d{1,4}', t): got = ('D', int(t) - ri - cum[pg]); break
                if re.fullmatch(r'[ivxlcIVXLC]{1,7}', t): got = ('r', roman(t) - ri - cum[pg]); break
            if got: seen[pg] = got; break
    if CFG['page_offset'] is not None:
        seen = {pg: ('D', CFG['page_offset'] + 1) for pg in pages}
    if not seen: return {}, []
    ks = sorted(seen)
    for i in range(1, len(ks) - 1):   # 某页与前后都不同且前后一致 → 误识
        a, b, cc = seen[ks[i - 1]], seen[ks[i]], seen[ks[i + 1]]
        if a == cc != b: seen[ks[i]] = a
    lab = {}; cur = seen[ks[0]]
    for pg in sorted(pages):
        cur = seen.get(pg, cur); st, o = cur; lab[pg] = (st, cum[pg] + o, k[pg])   # (样式, 本页第一个印刷页码, 本页含几页)
    ranges = []; fmt = lambda st, n: str(n) if st == 'D' else to_roman(n)
    for pg in sorted(pages):
        st, n, kk = lab[pg]
        if n < 1: continue
        if kk > 1: ranges.append({'startpage': pg - 1, 'prefix': fmt(st, n) + '–', 'style': st, 'firstpagenum': n + kk - 1}); continue
        prev = ranges[-1] if ranges else None
        if prev and not prev['prefix'] and prev['style'] == st and prev['firstpagenum'] + (pg - 1 - prev['startpage']) == n and \
                all(lab[q][2] == 1 for q in range(prev['startpage'] + 1, pg)):
            continue
        ranges.append({'startpage': pg - 1, 'prefix': '', 'style': st, 'firstpagenum': n})
    return lab, ranges

def detect_toc(pages):
    """章节标题：匹配 toc_patterns，或字号明显大于正文的正文行；同一行的碎片先合并"""
    hs = [l['box'][3] - l['box'][1] for p in pages.values() for l in p['lines'] if l['kind'] == 'body']
    H = statistics.median(hs) if hs else 40
    toc = []
    for pg in sorted(pages):
        p = pages[pg]; body = [l for l in p['lines'] if l['kind'] == 'body']
        rows = []   # 按阅读顺序把同一栏同一行的碎片并成一行
        for l in reading_order(body):
            yc = (l['box'][1] + l['box'][3]) / 2; key = (l.get('ri', 0), l.get('seg', 0), l.get('ci', 0))
            if rows and rows[-1]['key'] == key and abs(rows[-1]['yc'] - yc) < .5 * H: rows[-1]['ls'].append(l)
            else: rows.append({'yc': yc, 'key': key, 'ls': [l]})
        ycs = [r['yc'] for r in rows]; d = [b - a for a, b in zip(ycs, ycs[1:]) if b > a]
        pitch = statistics.median(d) if d else 2 * H
        for ri, r in enumerate(rows):
            bx = r['ls'][0].get('bx') or [0, p['w']]; BW = bx[1] - bx[0]
            ls = sorted(r['ls'], key=lambda l: l['box'][0])
            gap_up = ycs[ri] - ycs[ri - 1] if ri and rows[ri - 1]['key'] == r['key'] else 9e9
            gap_dn = ycs[ri + 1] - ycs[ri] if ri + 1 < len(rows) and rows[ri + 1]['key'] == r['key'] else 9e9
            isolated = gap_up > 1.5 * pitch and gap_dn > 1.4 * pitch   # 标题上下都有空行
            text = re.sub(r'\s+', '　', '　'.join(l['text'].strip() for l in ls)).strip('　')
            text = re.sub(r'(?<=[\u4e00-\u9fff])　(?=[\u4e00-\u9fff]$)', '', text)  # "章　名"末字被拆开
            x0 = min(l['box'][0] for l in ls); x1 = max(l['box'][2] for l in ls)
            h = max(l['box'][3] - l['box'][1] for l in ls)
            if len(text) > 30 or re.search(r'[。；，：]$', text): continue
            centered = abs((x0 + x1) / 2 - (bx[0] + bx[1]) / 2) < BW * .08 and (x1 - x0) < BW * .6
            lvl = None
            for pat, lv in CFG['toc_patterns']:
                if re.search(pat, text) and (centered or h > 1.3 * H or (isolated and x1 - x0 < BW * .6)): lvl = lv; break
            if lvl is None and h > 1.5 * H and centered: lvl = 1
            if lvl:
                toc.append([lvl, text, pg, {'kind': 1, 'to': None, 'top': min(l['box'][1] for l in ls) / p['h']}])
    for lvl, title, pg in reversed(CFG.get('toc_extra', [])):   # 手工补的书签（如图片式章名页），同页被它包含的自动书签去掉
        toc = [t for t in toc if not (t[2] == pg and title.replace('　', '').startswith(t[1].replace('　', '')))]
        toc.insert(0, [lvl, title, pg, {'kind': 1, 'to': None, 'top': 0}])
    toc.sort(key=lambda t: (t[2], t[3]['top']))
    # 层级不能跳级（首项必须为1）
    if toc:
        m = min(t[0] for t in toc)
        for t in toc: t[0] -= m - 1
        for i in range(1, len(toc)): toc[i][0] = min(toc[i][0], toc[i - 1][0] + 1)
    return toc

def cmd_toc():
    pages = final_pages()
    for t in detect_toc(pages): print('  ' * (t[0] - 1) + t[1], 'p', t[2])
    lab, ranges = page_labels(pages); print('page labels', ranges)

def cmd_build():
    import pymupdf
    zot = '--zotero' in ARGS; dbg = '--debug' in ARGS
    out = J(ARGS[ARGS.index('--out') + 1]) if '--out' in ARGS else J(CFG['out'])
    pages = final_pages(); Wd = json.load(open(J('words.json')))
    cjk = pymupdf.Font('cjk'); lat = pymupdf.Font('notos')
    font_for = lambda ch: cjk if cjk.has_glyph(ord(ch)) else lat   # 尽量单一字体 → 高亮高度一致
    KIND_H = {}
    for k in ['body', 'note', 'caption', 'table', 'figure', 'header', 'margin', 'folio']:
        hs = [l['box'][3] - l['box'][1] for p in pages.values() for l in p['lines'] if l['kind'] == k]
        KIND_H[k] = statistics.median(hs) if hs else 40
    allx = sorted(l['box'][2] for p in pages.values() for l in p['lines'] if l['kind'] == 'body')
    RIGHT = allx[int(len(allx) * .9)] if allx else 1e9

    def cells_for(pg, box):
        x0, y0, x1, y1 = box; cells = []
        for row in Wd[str(pg)]:
            ry0 = min(p[1] for p in row['box']); ry1 = max(p[1] for p in row['box'])
            if min(y1, ry1) - max(y0, ry0) < .5 * min(y1 - y0, ry1 - ry0): continue
            for item, (cx0, cx1) in zip(row['chars'], row['cboxes']):
                if cx1 < x0 - 8 or cx0 > x1 + 8: continue
                n = len(item)
                for k, ch in enumerate(item):
                    cells.append((ch, cx0 + (cx1 - cx0) * k / n, cx0 + (cx1 - cx0) * (k + 1) / n))
        return sorted(cells, key=lambda c: c[1])

    def positions(pg, l):
        """校对后文本与OCR逐字框对齐（difflib），插入/替换的字均分相邻空间"""
        text = l['text']; x0, _, x1, _ = l['box']; cells = cells_for(pg, l['box']); n = len(text)
        if not cells or not n:
            w = (x1 - x0) / max(n, 1); return [(x0 + i * w, x0 + (i + 1) * w) for i in range(n)]
        pos = [None] * n
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ''.join(c[0] for c in cells), text, autojunk=False).get_opcodes():
            if tag == 'equal':
                for k in range(i2 - i1): pos[j1 + k] = cells[i1 + k][1:]
            elif tag == 'replace':
                a, b = cells[i1][1], cells[i2 - 1][2]; m = j2 - j1
                for k in range(m): pos[j1 + k] = (a + (b - a) * k / m, a + (b - a) * (k + 1) / m)
        i = 0
        while i < n:
            if pos[i] is not None: i += 1; continue
            j = i
            while j < n and pos[j] is None: j += 1
            lo = i - 1 if i > 0 else i; hi = j + 1 if j < n else j
            A = pos[lo][0] if lo < i else x0; B = pos[hi - 1][1] if hi > j else x1
            if B - A < 4: A, B = A - 2, B + 2
            m = hi - lo
            for k in range(m): pos[lo + k] = (A + (B - A) * k / m, A + (B - A) * (k + 1) / m)
            i = j
        pos = [(a, max(b, a + 1)) for a, b in pos]
        for i in range(n - 1):  # 首尾相接，避免阅读器插空格
            pos[i] = (pos[i][0], max(pos[i + 1][0], pos[i][0] + 1))
        return pos

    doc = pymupdf.open(CFG['src'])
    for page in doc:   # 去掉原有的文字层（如 macOS 实况文本留下的乱码），只留扫描图
        if page.get_text().strip():
            page.add_redact_annot(page.rect)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    for pg, p in pages.items():
        page = doc[pg - 1]; sx = page.rect.width / p['w']; sy = page.rect.height / p['h']; PH = page.rect.height
        kinds = ['body', 'note', 'figs'] if zot else WRITE_ORDER
        if '--kinds' in ARGS: kinds = ARGS[ARGS.index('--kinds') + 1].split(',')
        # 每行一个 BT…ET；行内每字只写 "Tz <gid>Tj"：Tz=字框宽/字形宽，字框首尾相接，所以顺序前进正好落在原图位置。
        # 不要改成整段一个 Tz：中文全角、英文比例字宽和扫描字形宽度不成比例，整段缩放会偏移到一两个字宽，高亮对不上原图；
        # 阅读器（pdf.js/Zotero）不会因 Tz 变化拆行。英文词间高亮断开是 Zotero 丢弃 U+0020 所致，Zotero 版已改写成 U+00A0。
        # 整页一个字体资源。先用 TextWriter 写同样的字让 PyMuPDF 登记字体和 ToUnicode，再把它生成的流换成我们的。
        ops, alltext, alt_text = [], [], []
        for kind in kinds:   # 写入顺序=选择顺序；正文先写，其它区排在后面
            ls = reading_order(of_kind(p['lines'], kind))
            grp = lambda l: (l.get('ri'), l.get('seg'), l.get('ci'))
            for idx, l in enumerate(ls):
                nb = [m for m in ls[max(idx - 1, 0):idx + 2] if grp(m) == grp(l)]
                nxt = idx + 1 < len(ls) and grp(ls[idx + 1]) == grp(l)
                localR = max(m['box'][2] for m in nb) if nxt else 9e9   # 引文等缩排段：和上下行右端对齐也算没断段
                h = l['box'][3] - l['box'][1]; hh = h if h > 1.4 * KIND_H[l['kind']] else KIND_H[l['kind']]
                fs = hh * sy * .80
                yc = (l['box'][1] + l['box'][3]) / 2 * sy
                base = yc + (cjk.ascender + cjk.descender) / 2 * fs
                text, cp = l['text'], positions(pg, l)
                # Zotero 丢掉 U+0020 字形，只按字距推断空格，留下的空隙会让高亮/选择在英文词间断开；
                # 改写成 U+00A0（Zotero NFKD 后仍是普通空格，字框连续，复制只出一个空格）
                if zot: text = text.replace(' ', '\u00a0')
                colR = l.get('colR') or RIGHT; colW = l['bx'][1] - l['bx'][0]
                if zot and kind in ('body', 'note') and cp and l['box'][2] > min(colR, localR) - 1.2 * KIND_H[kind] and l['box'][2] - l['box'][0] > .6 * colW:
                    text += '\u2010'; cp = cp + [(cp[-1][1], cp[-1][1] + 2)]
                seq = []
                for ch, (cx0, cx1) in zip(text, cp):
                    f = cjk if cjk.has_glyph(ord(ch)) else lat   # Droid 缺的（⁴⁵⁶…）用 Noto 补
                    g = f.has_glyph(ord(ch)); adv = f.glyph_advance(ord(ch)) * fs
                    if not g or adv <= 0: continue
                    seq.append((ch, g, cx0 * sx, max((cx1 - cx0) * sx, .3) / adv * 100, f is lat))
                if not seq: continue
                for c in seq: (alt_text if c[4] else alltext).append(c[0])
                o = [f'BT /FONT {fs:.3f} Tf 1 0 0 1 {seq[0][2]:.2f} {PH - base:.2f} Tm']; cur = False
                for ch, g, x, tz, isl in seq:
                    if isl != cur: o.append(f'/{"FONT2" if isl else "FONT"} {fs:.3f} Tf'); cur = isl
                    o.append(f'{tz:.2f} Tz <{g:04x}>Tj')
                ops.append('\n'.join(o) + '\nET')
        if not ops: continue
        old = set(page.get_contents())
        tw = pymupdf.TextWriter(page.rect); tw.append((0, 50), ''.join(alltext) or '　', font=cjk, fontsize=8)
        if alt_text: tw.append((0, 70), ''.join(alt_text), font=lat, fontsize=8)
        tw.write_text(page, render_mode=3)
        xs = [x for x in page.get_contents() if x not in old]
        names = re.findall(rb'/(F\d+) [\d.]+ Tf', doc.xref_stream(xs[0]))
        stream = '\n'.join(ops).replace('/FONT2', '/' + (names[-1].decode() if alt_text else 'F0')).replace('/FONT', '/' + names[0].decode())
        head = '1 0 0 1 0 0 cm\n' + ('0 Tr 1 0 0 rg\n' if dbg else '3 Tr\n')
        doc.update_stream(xs[0], ('q\n' + head + stream + '\nQ\n').encode())
    lab, ranges = page_labels(pages)   # 页标签=印刷页码，Zotero/阅读器引用时用
    if ranges: doc.set_page_labels(ranges); print('page labels', ranges)
    if CFG['toc']:
        toc = detect_toc(pages); items = []
        for lvl, title, pg, d in toc:
            pr = doc[pg - 1].rect
            items.append([lvl, title, pg, {'kind': pymupdf.LINK_GOTO, 'page': pg - 1, 'to': pymupdf.Point(0, d['top'] * pr.height - 6), 'zoom': 0}])
        if items: doc.set_toc(items); print('toc', [(t[0], t[1], t[2]) for t in toc])
    doc.subset_fonts(); os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    doc.save(out, garbage=4, deflate=True); print('saved', out)

# ---------------------------------------------------------------- check / overlay
SUSPECT = r'进人|导人|卷人|引人|纳人|带人|闯人|陷人|融人|深人|暖昧|自已|一一|—一|一—|[A-Za-z]\d{2,}[A-Za-z]'

def cmd_check():
    pdf = ARGS[0] if ARGS else J(CFG['out'])
    final_pages()
    txt = subprocess.run(['pdftotext', '-raw', pdf, '-'], capture_output=True, text=True).stdout
    for i, line in enumerate(txt.splitlines(), 1):
        probs = []
        if re.search(r'[\u3400-\u9fff，。；：“”《》] [\u3400-\u9fff，。；：“”《》]', line) and not re.match(r'^(第.{1,4}章|[一二三四五六七八九十百]+) ', line): probs.append('中文间空格')
        if re.search(rf'{CJK}[,;:?!()]|[,;:?!()]{CJK}', line): probs.append('中文旁半角标点')
        if re.search(SUSPECT, line): probs.append('疑似误识')
        if probs: print(i, probs, line)
    print('lines', len(txt.splitlines()))

def cmd_overlay():
    import pymupdf
    pdf = ARGS[1] if len(ARGS) > 1 else J(CFG['out']); d = pymupdf.open(pdf); os.makedirs(J('overlay'), exist_ok=True)
    for pn in [int(x) for x in ARGS[0].split(',')]:
        p = d[pn - 1]
        for b in p.get_text('rawdict')['blocks']:
            for l in b.get('lines', []):
                for s in l['spans']:
                    for c in s['chars']:
                        p.draw_rect(c['bbox'], color=None, fill=(.2, .5, 1), fill_opacity=.35, overlay=True)
        p.get_pixmap(dpi=90).save(J(f'overlay/p{pn:03d}.png')); print(J(f'overlay/p{pn:03d}.png'))

{'toc': cmd_toc, 'gaps': cmd_gaps, 'images': cmd_images, 'ocr': cmd_ocr, 'classify': cmd_classify, 'proof': cmd_proof,
 'build': cmd_build, 'check': cmd_check, 'overlay': cmd_overlay}[CMD]()
