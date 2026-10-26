#!/usr/bin/env python3
"""画面质检 v2 (用户确认规则):
  - 四角叠加台标/logo (静止持续出现) -> 拒, 换源
  - 底部一排字 / 字幕条 / 新闻包装 (lower third) -> 允许, 只记录不判废
台标判定: 角落高边缘密度 + 帧间形状固定(IoU) + 叠加物为"小块"(非扁横条/非满角大块)
"""
import os
import subprocess

from PIL import Image, ImageFilter

SAMPLES = [(0.20, 8), (0.65, 8)]   # 取两个位置各 8 秒

TH_EDGE = 0.15        # 角区域边缘密度阈值
TH_IOU = 0.45         # 命中帧两两边缘形状重合度, 高于此视为同一叠加物
TH_RUN = 0.35         # 底部带连续边缘列段 ≥35% 宽且跨过角区边界 => 横贯字幕/包装(放行)
HIT_MIN_RATIO = 0.6   # 至少 60% 帧命中才算台标
SIG = (64, 32)        # 帧间比较用缩略边缘掩码(保留文字细节, 字幕间 IoU 更低)
BAND_COLS = 100       # 底部带列二值化精度
CORNERS = ("tl", "tr", "bl", "br")


def _run_across(cols, anchor):
    """包含 anchor 列的连续 True 段长度"""
    if anchor >= len(cols) or not cols[anchor]:
        return 0
    lo = anchor
    while lo > 0 and cols[lo - 1]:
        lo -= 1
    hi = anchor
    while hi < len(cols) - 1 and cols[hi + 1]:
        hi += 1
    return hi - lo + 1


def _sh(cmd, timeout=180):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def probe_dur(path):
    r = _sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], 60)
    try:
        return float((r.stdout or "0").strip())
    except Exception:
        return 0.0


def grab_samples(url, ytdlp_base, workdir, key, dur, proxy=None):
    """下最低画质的整片(体积很小)用于抽帧质检; 返回 (文件列表, 错误信息)"""
    out = os.path.join(workdir, f"{key}_low.mp4")
    cmd = list(ytdlp_base) + (["--proxy", proxy] if proxy else []) + [
        "-f", "bv*[height<=360]+ba/b[height<=360]/b/worst",
        "--merge-output-format", "mp4",
        "-o", out, url]
    r = _sh(cmd, 900)
    if r and os.path.exists(out) and os.path.getsize(out) > 50000:
        return [out], ""
    err = (r.stderr or "")[-260:] if r else "timeout"
    return [], err.replace("\n", " ")


def extract_frames(video, outdir, prefix, n=4, width=360):
    dur = probe_dur(video)
    files = []
    step = dur / (n + 1) if dur else 1.5
    for i in range(n):
        t = step * (i + 1)
        f = os.path.join(outdir, f"{prefix}_{i}.jpg")
        r = _sh(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", video,
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-y", f], 60)
        if r and os.path.exists(f) and os.path.getsize(f) > 2000:
            files.append(f)
    return files


def ocr_words(path):
    try:
        r = _sh(["tesseract", path, "-", "--psm", "6", "-l", "eng"], 60)
    except FileNotFoundError:
        return 0
    if not r:
        return 0
    words = [w for w in (r.stdout or "").split()
             if sum(c.isalpha() for c in w) >= 3 and len(w) >= 3]
    return len(words)


def _mask_of(path, box):
    """角区域边缘掩码(SIG 尺寸) + 边缘密度"""
    im = Image.open(path).convert("L")
    e = im.crop(box).filter(ImageFilter.FIND_EDGES)
    m = [1 if p > 55 else 0 for p in e.resize(SIG).getdata()]
    return m, sum(m) / len(m)


def _band_runs(path):
    """底部带(85%~100%高)列二值化后, 跨下两角锚点的连续 run 占全宽比例"""
    im = Image.open(path).convert("L")
    w, h = im.size
    band = im.crop((0, int(h * .85), w, h)).filter(ImageFilter.FIND_EDGES)
    bw, bh = band.size
    px = band.load()
    raw = []
    for x in range(bw):
        mx = 0
        for y in range(bh):
            v = px[x, y]
            if v > mx:
                mx = v
        raw.append(mx > 55)
    seg = bw / BAND_COLS
    cols = [any(raw[int(i * seg):max(int((i + 1) * seg), int(i * seg) + 1)])
            for i in range(BAND_COLS)]
    bl_anchor = int(0.17 * BAND_COLS)
    br_anchor = int(0.83 * BAND_COLS)
    return (_run_across(cols, bl_anchor) / BAND_COLS,
            _run_across(cols, br_anchor) / BAND_COLS)


def corner_states(frames):
    """每帧四角: (掩码, 边缘密度) + 底部带跨锚点连续 run"""
    im_boxes = None
    out = []
    for f in frames:
        im = Image.open(f).convert("L")
        w, h = im.size
        if im_boxes is None:
            im_boxes = {
                "tl": (0, 0, int(w * .16), int(h * .15)),
                "tr": (int(w * .84), 0, w, int(h * .15)),
                "bl": (0, int(h * .85), int(w * .16), h),
                "br": (int(w * .84), int(h * .85), w, h),
            }
        run_bl, run_br = _band_runs(f)
        st = {}
        for k, b in im_boxes.items():
            m, d = _mask_of(f, b)
            st[k] = {"mask": m, "edge": d}
        st["run_bl"], st["run_br"] = run_bl, run_br
        out.append(st)
    return out


def bottom_text(path):
    """底部文字量, 仅作记录(允许字幕/包装), 不参与判废"""
    im = Image.open(path)
    w, h = im.size
    sub = im.crop((0, int(h * 0.70), w, h))
    tmp = path + ".bottom.jpg"
    sub.save(tmp, quality=85)
    n = ocr_words(tmp)
    os.remove(tmp)
    return n


def check_frames(frames):
    """四角台标判定; 底部字幕/包装放行。返回 (ok, reasons, metrics)"""
    n = len(frames)
    if n == 0:
        return False, ["no-frames"], []
    states = corner_states(frames)
    metrics, reasons = [], []
    for i, f in enumerate(frames):
        bt = bottom_text(f)
        metrics.append({"f": i,
                        "corners": {k: round(v["edge"], 3) for k, v in states[i].items()
                                    if isinstance(v, dict)},
                        "bottom_words": bt})
    need = max(2, int(round(n * HIT_MIN_RATIO)))
    # 横贯字幕/包装豁免: 连续边缘跨过角区边界且 ≥35% 宽的帧占多数 => 放行下两角
    exempt = {k: sum(1 for s in states if s[f"run_{k}"] >= TH_RUN) >= max(2, int(round(n * 0.6)))
              for k in ("bl", "br")}
    for k in CORNERS:
        if exempt.get(k):
            continue
        edges = [s[k]["edge"] for s in states]
        hits = [i for i, e in enumerate(edges) if e > TH_EDGE]
        if len(hits) < need:
            continue
        masks = [states[i][k]["mask"] for i in hits]
        ious = []
        for a in range(len(masks)):
            for b in range(a + 1, len(masks)):
                inter = sum(1 for x, y in zip(masks[a], masks[b]) if x and y)
                union = sum(1 for x, y in zip(masks[a], masks[b]) if x or y)
                if union:
                    ious.append(inter / union)
        iou = sum(ious) / len(ious) if ious else 0.0
        if iou > TH_IOU:
            reasons.append(f"四角台标[{k}] {len(hits)}/{n}帧 叠加物形状固定(iou={iou:.2f})")
    return (len(reasons) == 0), reasons, metrics


def build_strip(frames, out_path):
    if not frames:
        return None
    r = _sh(["ffmpeg", "-v", "error", "-pattern_type", "glob",
             "-i", os.path.join(os.path.dirname(frames[0]), "*_*.jpg"),
             "-filter_complex", "tile=4x2", "-frames:v", "1", "-y", out_path], 90)
    if r and os.path.exists(out_path):
        return out_path
    return None
