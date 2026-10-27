#!/usr/bin/env python3
"""画面质检 v3.2 (用户确认规则):
  - 底部一排字 / 字幕条 / 字幕包装(lower third) -> 拒, 换源
  - 四角台标 / logo -> 允许, 不判废
  - PPT 幻灯片 -> 拒, 换源
  - 产品界面演示(新闻/产品相关) -> 允许(不能把产品 UI 误杀)

字幕判定(不依赖 OCR, 中英文通用): 底部带里存在"亮色断续文字块",
且该文字行在多帧中位置固定(字幕总在同一高度), 横向覆盖足够宽。
四角 logo 是连续单个亮块(段数 1) 且只占窄条, 天然不会被判成字幕。

PPT vs 产品界面的区分: 两者都有长直线+大面积单色底, 但
  PPT = 大面积白底 + 几乎无彩色(高亮低饱和) -> 拒
  产品界面 = 深色底或彩色元素丰富(深/彩) -> 放行
"""
import os
import subprocess

from PIL import Image

BAND_TOP = 0.72         # 字幕带: 画面高度 72% ~ 100%
LIT = 195               # 亮色像素阈值(白字)
COLS = 120              # 列投影精度
ROW_BOTTOM_MIN = 0.10   # 文字行必须在字幕带的下部(即画面 >=75% 高, 兼顾偏上的字幕)
MIN_LIT_COLS = 0.15     # 亮列占画面宽 >=15%(新闻包装条实测 0.19; 角落 logo 只有 ~13%)
MIN_RUNS = 4            # 亮块段数 >=4 => 断续文字(有词间距); logo 是 1 段连续块
ROW_STD_MAX = 4.0       # 文字行位置标准差(px)上限 => 行固定
HIT_MIN_RATIO = 0.6     # 至少 60% 帧命中才判字幕
DARK_MEAN = 12          # 黑帧均值上限

# 界面判定: 大量长横竖线 + 大面积单色底(实景照片/街景不具备)
SCREEN_W = 240          # 分析用宽度
EDGE_G = 28             # 梯度阈值
MIN_LINE_LEN = 0.20     # 长直线: 横线 ≥20% 宽, 竖线 ≥20% 高
SCREEN_LINES = 3        # 长直线条数下限(辅助, 排除纯自然单色如蓝天)
SCREEN_FLAT = 0.32      # 主灰度占比下限(大面积纯色底; 实景实测 0.04~0.17)
SCREEN_RATIO = 0.5      # 半数以上帧像 PPT => 判废(产品界面不算)

# PPT 特征: 大面积白底 + 无彩色。产品界面多为深色底或彩色元素丰富, 据此放行
PPT_BRIGHT = 140        # 平均亮度下限(PPT 白底实测 >180, 产品界面深色底 <120)
PPT_SAT = 0.12          # 高饱和像素占比上限(PPT 彩色极少; 软件界面常有大量彩色)

# 人脸检测(帧级): 主播/口播博主/采访出镜 -> 拒(用户规则: 不要有人对镜头讲话的画面)
FACE_MIN_RATIO = 0.18   # 最大脸宽 >= 画面宽 18% 算"明显出镜"(路人小脸不受影响)
FACE_HIT_RATIO = 0.4    # >=40% 帧有明显人脸 => 拒


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
                    "--merge-output-format", "mp4", "-o", seg, url], 180)
    if _ok(seg):
        return [seg], ""

    # 回退: 整片最低画质(部分源不支持精确截段)
    r = _sh(base + ["-f", FMT_LOW, "--merge-output-format", "mp4",
                    "-o", out, url], 420)
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
    """界面特征: (长横线条数, 长竖线条数, 主灰度占比, 平均亮度, 彩色像素占比)
    PPT/录屏 = 大量横竖矩形线框 + 大面积单色底; 实景照片两者都很低
    亮度+彩色占比用于区分 PPT(白底无彩) 与 产品界面(深色/彩色)"""
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
    mean = sum(i * c for i, c in enumerate(hist)) / max(w * h, 1)

    rgb = Image.open(path).convert("RGB").resize((160, 90))
    npx = 160 * 90
    r = rgb.getdata()
    sat = sum(1 for p in r if max(p) - min(p) > 60) / npx
    return hl, vl, flat, mean, sat


def looks_like_screen(path):
    """返回 (screen_like, ppt_like, hl, vl, flat, mean, sat)
    screen_like: 像"某类界面"(PPT 或产品界面都算)
    ppt_like:    是 PPT 幻灯片(白底无彩) -> 该拒; 产品界面(深/彩) -> 放行"""
    hl, vl, flat, mean, sat = screen_score(path)
    screen_like = (hl + vl >= SCREEN_LINES) and (flat >= SCREEN_FLAT)
    ppt_like = screen_like and (mean >= PPT_BRIGHT) and (sat <= PPT_SAT)
    return screen_like, ppt_like, hl, vl, flat, mean, sat


def _face_detector():
    """OpenCV Haar 人脸检测器; cv2 不可用返回 None(优雅跳过)"""
    try:
        import cv2
        det = cv2.CascadeClassifier(cv2.data.haarcascades
                                    + "haarcascade_frontalface_default.xml")
        return None if det.empty() else det
    except Exception:
        return None


def face_hits(frames, det=None):
    """各帧最大人脸占比列表。det=None 时返回全 0(不拦截)。"""
    if det is None:
        det = _face_detector()
    if det is None:
        return [0.0] * len(frames)
    import cv2
    out = []
    for f in frames:
        try:
            img = cv2.imread(f)
            if img is None:
                out.append(0.0)
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = det.detectMultiScale(gray, 1.15, 5, minSize=(36, 36))
            out.append(max((fw for x, y, fw, fh in faces), default=0.0) / img.shape[1])
        except Exception:
            out.append(0.0)
    return out


def check_frames(frames):
    """字幕条判定。返回 (ok, reasons, metrics)"""
    n = len(frames)
    if n < 3:
        return False, [f"帧数不足({n})"], []
    metrics, cand = [], []
    fh_all = face_hits(frames)
    for i, f in enumerate(frames):
        im = Image.open(f).convert("L")
        mean = sum(im.resize((1, 1), Image.BOX).getdata()) / 255 * 255
        ok, peak, runs, cover = looks_like_subtitle(f)
        scr, ppt, hl, vl, flat, fmean, sat = looks_like_screen(f)
        metrics.append({"f": i, "dark_mean": int(mean), "screen": ppt,
                        "ui": bool(scr and not ppt),
                        "lines": hl + vl, "flat": round(flat, 2),
                        "bright": int(fmean), "sat": round(sat, 2),
                        "face": round(fh_all[i], 2),
                        "ocr_words": ocr_words(f)})
        if ok:
            cand.append((i, peak, runs, cover))

    reasons = []
    # 主播/口播出镜(帧级人脸检测): 明显人脸出现在多帧 => 拒
    big = sum(1 for r in fh_all if r >= FACE_MIN_RATIO)
    if big >= max(2, int(round(n * FACE_HIT_RATIO))):
        reasons.append(f"人物出镜/口播 {big}/{n}帧(最大脸占比{max(fh_all):.0%})")
        return False, reasons, metrics

    # 全黑/空画面
    dark = sum(1 for m in metrics if m["dark_mean"] <= DARK_MEAN)
    if dark >= max(2, int(round(n * HIT_MIN_RATIO))):
        reasons.append(f"画面全黑/无内容 {dark}/{n}帧")
        return False, reasons, metrics

    # PPT 幻灯片画面(用户明确不要; 产品界面演示不算, 放行)
    scr = sum(1 for m in metrics if m["screen"])
    if scr >= max(2, int(round(n * SCREEN_RATIO))):
        lines = sum(m["lines"] for m in metrics) // n
        flat = sum(m["flat"] for m in metrics) / n
        bright = sum(m["bright"] for m in metrics) / n
        reasons.append(f"PPT幻灯片画面 {scr}/{n}帧(直线{lines}条 单色底{flat:.0%} "
                       f"亮度{bright:.0f}低彩)")
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
