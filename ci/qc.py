#!/usr/bin/env python3
"""画面质检: 低码率取样 -> 抽帧 -> OCR查字幕 / 边角查台标 -> 判定能否下载"""
import os
import subprocess

from PIL import Image, ImageFilter

SAMPLES = [(0.20, 8), (0.65, 8)]   # 取两个位置各 8 秒


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


def grab_samples(url, ytdlp_base, workdir, key, dur):
    """下最低画质的整片(体积很小)用于抽帧质检; 返回 (文件列表, 错误信息)"""
    out = os.path.join(workdir, f"{key}_low.mp4")
    cmd = list(ytdlp_base) + [
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
    r = _sh(["tesseract", path, "-", "--psm", "6", "-l", "eng"], 60)
    if not r:
        return 0
    words = [w for w in (r.stdout or "").split()
             if sum(c.isalpha() for c in w) >= 3 and len(w) >= 3]
    return len(words)


def _edge_score(img):
    e = img.convert("L").filter(ImageFilter.FIND_EDGES)
    px = list(e.getdata())
    return sum(1 for p in px if p > 55) / max(len(px), 1)


def corner_scores(path):
    im = Image.open(path)
    w, h = im.size
    boxes = {
        "tl": (0, 0, int(w * .22), int(h * .18)),
        "tr": (int(w * .78), 0, w, int(h * .18)),
        "br": (int(w * .75), int(h * .80), w, h),
        "bl": (0, int(h * .80), int(w * .25), h),
    }
    return {k: _edge_score(im.crop(b)) for k, b in boxes.items()}


def bottom_text(path):
    im = Image.open(path)
    w, h = im.size
    sub = im.crop((0, int(h * 0.70), w, h))
    tmp = path + ".bottom.jpg"
    sub.save(tmp, quality=85)
    n = ocr_words(tmp)
    os.remove(tmp)
    return n


def check_frames(frames):
    """渐进式判定: 返回 (ok, reasons, metrics)"""
    reasons = []
    metrics = []
    corner_hits = 0
    for i, f in enumerate(frames):
        cs = corner_scores(f)
        mx = max(cs.values())
        bt = bottom_text(f)
        metrics.append({"f": i, "corner": round(mx, 3),
                        "bottom_words": bt})
        if bt >= 5:
            reasons.append(f"字幕/大字幕 f{i}({bt}词)")
        if mx > 0.16:
            corner_hits += 1
    if corner_hits >= max(2, len(frames) // 3):
        reasons.append(f"疑似台标/角标 {corner_hits}/{len(frames)}帧")
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
