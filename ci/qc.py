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
        metrics.append({"f": i, "dark_mean": int(mean),
                        "ocr_words": ocr_words(f)})
        if ok:
            cand.append((i, peak, runs, cover))

    reasons = []
    # 全黑/空画面
    dark = sum(1 for m in metrics if m["dark_mean"] <= DARK_MEAN)
    if dark >= max(2, int(round(n * HIT_MIN_RATIO))):
        reasons.append(f"画面全黑/无内容 {dark}/{n}帧")
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
