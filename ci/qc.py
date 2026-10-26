#!/usr/bin/env python3
"""画面质检 v3 (用户确认规则):
  - 底部一排字 / 字幕条 / 字幕包装(lower third) -> 拒, 换源
  - 四角台标 / logo -> 允许, 不判废

字幕判定(不依赖 OCR, 中英文通用): 底部带里存在"亮色断续文字块",
且该文字行在多帧中位置固定(字幕总在同一高度), 横向覆盖足够宽。
四角 logo 是连续单个亮块(段数 1) 且只占窄条, 天然不会被判成字幕。
"""
import os
import subprocess

from PIL import Image

BAND_TOP = 0.72         # 字幕带: 画面高度 72% ~ 100%
LIT = 195               # 亮色像素阈值(白字)
COLS = 120              # 列投影精度
ROW_BOTTOM_MIN = 0.10   # 文字行必须在字幕带的下部(即画面 >=75% 高, 兼顾偏上的字幕)
MIN_LIT_COLS = 0.25     # 亮列占画面宽 >=25%(字幕横贯; 角落 logo 只有 ~13%)
MIN_RUNS = 4            # 亮块段数 >=4 => 断续文字(有词间距); logo 是 1 段连续块
ROW_STD_MAX = 4.0       # 文字行位置标准差(px)上限 => 行固定
HIT_MIN_RATIO = 0.6     # 至少 60% 帧命中才判字幕
DARK_MEAN = 12          # 黑帧均值上限

# 界面/PPT 判定: 大量长横竖线 + 大面积单色底(实景照片/街景不具备)
SCREEN_W = 240          # 分析用宽度
EDGE_G = 28             # 梯度阈值
MIN_LINE_LEN = 0.20     # 长直线: 横线 ≥20% 宽, 竖线 ≥20% 高
SCREEN_LINES = 3        # 长直线条数下限(辅助, 排除纯自然单色如蓝天)
SCREEN_FLAT = 0.32      # 主灰度占比下限(大面积纯色底; 实景实测 0.04~0.17)
SCREEN_RATIO = 0.5      # 半数以上帧像界面/PPT => 判废


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


SAMPLES_SEC = 90        # 取样只截中间 90 秒(慢代理下比下整片快 5 倍以上)
FMT_LOW = "bv*[height<=360]+ba/b[height<=360]/b/worst"


def grab_samples(url, ytdlp_base, workdir, key, dur, proxy=None):
    """取一段最低画质片段用于抽帧质检(体积小, 代理慢也扛得住);
    截段失败再回退整片。返回 (文件列表, 错误信息)"""
    out = os.path.join(workdir, f"{key}_low.mp4")
    base = list(ytdlp_base) + (["--proxy", proxy] if proxy else [])

    def _ok(p, min_size=30000):
        return os.path.exists(p) and os.path.getsize(p) > min_size

    # 第一次: 只截中间一段(--download-sections, 需 ffmpeg)
    d = dur if dur and dur > 0 else 300
    start = max(int(d * 0.15), 5)
    end = min(start + SAMPLES_SEC, max(int(d - 5), start + 10))
    def _ts(s):
        s = max(int(s), 0)
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    seg = os.path.join(workdir, f"{key}_sec.mp4")
    r = _sh(base + ["-f", FMT_LOW,
                    "--download-sections", f"*{_ts(start)}-{_ts(end)}",
                    "--force-keyframes-at-cuts",
                    "--merge-output-format", "mp4", "-o", seg, url], 240)
    if _ok(seg):
        return [seg], ""

    # 回退: 整片最低画质(部分源不支持精确截段)
    r = _sh(base + ["-f", FMT_LOW, "--merge-output-format", "mp4",
                    "-o", out, url], 600)
    if _ok(out, 50000):
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
    """OCR 词数, 仅作辅助证据写入 report(不参与判定)"""
    try:
        r = _sh(["tesseract", path, "-", "--psm", "6", "-l", "eng+chi_sim"], 60)
    except FileNotFoundError:
        return 0
    if not r:
        return 0
    txt = r.stdout or ""
    words = [w for w in txt.split() if len(w) >= 2]
    cjk = sum(1 for c in txt if "\u4e00" <= c <= "\u9fff")
    return len(words) + cjk // 2


def band_profile(path):
    """底部字幕带的亮像素剖面 -> (行投影, 列投影, 亮像素占比)"""
    im = Image.open(path).convert("L")
    w, h = im.size
    band = im.crop((0, int(h * BAND_TOP), w, h))
    bw, bh = band.size
    b = band.point(lambda p: 255 if p > LIT else 0)
    lit = sum(b.getdata())
    ratio = (lit / 255) / max(bw * bh, 1)
    rows = list(b.resize((1, bh), Image.BOX).getdata())
    cols = list(b.resize((COLS, 1), Image.BOX).getdata())
    return rows, cols, ratio


def looks_like_subtitle(path):
    """单帧: 底部是否有"连续行位置的断续文字块"。返回 (bool, peak_row, runs, lit_cols)"""
    rows, cols, ratio = band_profile(path)
    bh = len(rows)
    if bh == 0:
        return False, 0, 0, 0.0
    peak = max(range(bh), key=lambda j: rows[j])
    if peak < bh * ROW_BOTTOM_MIN:
        return False, peak, 0, 0.0
    th = max(cols) * 0.35
    lit_cols = [c > th for c in cols]
    cover = sum(lit_cols) / len(lit_cols)
    runs, prev = 0, False
    for c in lit_cols:
        if c and not prev:
            runs += 1
        prev = c
    ok = (cover >= MIN_LIT_COLS) and (runs >= MIN_RUNS)
    return ok, peak, runs, cover


def screen_score(path):
    """界面/PPT 特征: (长横线条数, 长竖线条数, 主灰度占比)
    PPT/录屏 = 大量横竖矩形线框 + 大面积单色底; 实景照片两者都很低"""
    im = Image.open(path).convert("L")
    w0, h0 = im.size
    im = im.resize((SCREEN_W, max(int(SCREEN_W * h0 / w0), 8)))
    w, h = im.size
    px = im.load()
    min_h = max(int(w * MIN_LINE_LEN), 8)
    min_v = max(int(h * MIN_LINE_LEN), 5)
    hl = vl = 0
    # 横线: y-1 -> y 有突变(垂直梯度), 且沿 x 连续
    for y in range(1, h):
        run = 0
        for x in range(w):
            if abs(px[x, y] - px[x, y - 1]) > EDGE_G:
                run += 1
                if run == min_h:
                    hl += 1
            else:
                run = 0
    # 竖线: x-1 -> x 有突变(水平梯度), 且沿 y 连续
    for x in range(1, w):
        run = 0
        for y in range(h):
            if abs(px[x, y] - px[x - 1, y]) > EDGE_G:
                run += 1
                if run == min_v:
                    vl += 1
            else:
                run = 0
    hist = im.histogram()
    flat = max(hist) / max(w * h, 1)
    return hl, vl, flat


def looks_like_screen(path):
    hl, vl, flat = screen_score(path)
    return (hl + vl >= SCREEN_LINES) and (flat >= SCREEN_FLAT), hl, vl, flat


def check_frames(frames):
    """字幕条判定。返回 (ok, reasons, metrics)"""
    n = len(frames)
    if n < 3:
        return False, [f"帧数不足({n})"], []
    metrics, cand = [], []
    for i, f in enumerate(frames):
        im = Image.open(f).convert("L")
        mean = sum(im.resize((1, 1), Image.BOX).getdata()) / 255 * 255
        ok, peak, runs, cover = looks_like_subtitle(f)
        scr, hl, vl, flat = looks_like_screen(f)
        metrics.append({"f": i, "dark_mean": int(mean), "screen": scr,
                        "lines": hl + vl, "flat": round(flat, 2),
                        "ocr_words": ocr_words(f)})
        if ok:
            cand.append((i, peak, runs, cover))

    reasons = []
    # 全黑/空画面
    dark = sum(1 for m in metrics if m["dark_mean"] <= DARK_MEAN)
    if dark >= max(2, int(round(n * HIT_MIN_RATIO))):
        reasons.append(f"画面全黑/无内容 {dark}/{n}帧")
        return False, reasons, metrics

    # 界面演示 / PPT 画面(用户明确不要)
    scr = sum(1 for m in metrics if m["screen"])
    if scr >= max(2, int(round(n * SCREEN_RATIO))):
        lines = sum(m["lines"] for m in metrics) // n
        flat = sum(m["flat"] for m in metrics) / n
        reasons.append(f"界面演示/PPT画面 {scr}/{n}帧(直线{lines}条 单色底{flat:.0%})")
        return False, reasons, metrics

    need = max(2, int(round(n * HIT_MIN_RATIO)))
    if len(cand) >= need:
        peaks = [c[1] for c in cand]
        mean = sum(peaks) / len(peaks)
        std = (sum((p - mean) ** 2 for p in peaks) / len(peaks)) ** 0.5
        if std <= ROW_STD_MAX:
            cover = sum(c[3] for c in cand) / len(cand)
            runs = sum(c[2] for c in cand) / len(cand)
            reasons.append(f"底部字幕条 {len(cand)}/{n}帧 行固定(y±{std:.1f}) "
                           f"文字块{runs:.0f}段 覆盖{cover:.0%}宽")
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
