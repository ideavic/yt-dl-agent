#!/usr/bin/env python3
"""AppVeyor 端批量抓取: 读 queue.json, 逐条下载, 结果放 out/"""
import concurrent.futures
import json
import os
import random
import re
import subprocess
import sys
import time

OUT = "out"
os.makedirs(OUT, exist_ok=True)

QUEUE = json.load(open("queue.json"))
JOBS = QUEUE.get("jobs", [])
BUDGET = int(os.environ.get("BUDGET", "2700"))
T0 = time.time()

YTDLP = ["yt-dlp", "--no-warnings", "--socket-timeout", "20",
         "--no-part", "--http-chunk-size", "5M", "--retries", "2",
         "--js-runtimes", "node", "--remote-components", "ejs:github"]

CHANNEL_BONUS = ["reuters", "associated press", "ap archive", "bloomberg", "cnbc", "bbc",
                 "dw news", "deutsche welle", "cna", "channel newsasia", "afp",
                 "al jazeera", "cbs", "nbc", "abc news", "sky news", "itn", "archival"]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def sh(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def ascii_ratio(s):
    if not s:
        return 0
    return sum(1 for c in s if ord(c) < 128) / len(s)


def pick(query, dmin, dmax, proxy=None):
    cmd = YTDLP + (["--proxy", proxy] if proxy else []) + [
        "--flat-playlist", "--dump-single-json", f"ytsearch10:{query}"]
    r = sh(cmd, 90)
    if not r or r.returncode != 0 or not (r.stdout or "").strip().startswith("{"):
        return None
    try:
        entries = json.loads(r.stdout).get("entries") or []
    except Exception:
        return None
    best, best_score = None, -99
    for e in entries:
        title = e.get("title") or ""
        dur = e.get("duration") or 0
        ch = (e.get("channel") or e.get("uploader") or "").lower()
        if dmin and dur < dmin * 0.85:
            continue
        if dmax and dur > dmax * 1.2:
            continue
        if ascii_ratio(title) < 0.8:
            continue
        if any(w in title.lower() for w in ["中字", "字幕", "搬运", "解说", "reaction"]):
            continue
        score = 2
        if any(c in ch for c in CHANNEL_BONUS):
            score += 5
        if 60 <= dur <= 1200:
            score += 2
        if (e.get("view_count") or 0) > 10000:
            score += 1
        if score > best_score:
            best_score, best = score, e
    return best


_POOL = None


def alive_proxies(limit=8):
    global _POOL
    if _POOL is not None:
        return _POOL
    raw = []
    for url in [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    ]:
        r = sh(["curl", "-s", "-L", "--max-time", "40", url], 60)
        if r and r.stdout:
            raw.append(r.stdout)
    proxies = []
    for txt in raw:
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("socks5://"):
                proxies.append("socks5h://" + line[8:])
            elif re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", line):
                proxies.append("http://" + line)
    proxies = list(dict.fromkeys(proxies))
    random.shuffle(proxies)
    log(f"代理池候选 {len(proxies)}")

    def test(p):
        r = sh(["curl", "-s", "-x", p, "--max-time", "6", "-o", "/dev/null",
                "-w", "%{http_code}", "https://www.youtube.com/generate_204"], 10)
        return p if r and r.stdout.strip().startswith("2") else None

    alive = []
    pool = proxies[:4000]
    with concurrent.futures.ThreadPoolExecutor(300) as ex:
        for x in ex.map(test, pool):
            if x:
                alive.append(x)
            if len(alive) >= limit * 2:
                break
    random.shuffle(alive)
    log(f"对 YouTube 存活 {len(alive)}")
    _POOL = alive[:limit]
    return _POOL


def download(url, dest, maxh, proxy=None, timeout=600):
    cmd = YTDLP + (["--proxy", proxy] if proxy else []) + [
        "-f", f"bv*[height<={maxh}]+ba/b[height<={maxh}]/b",
        "--merge-output-format", "mp4", "-o", dest, url]
    r = sh(cmd, timeout)
    if r is None:
        return False, "timeout"
    if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 1 << 20:
        return True, "ok"
    return False, (r.stderr or "")[-200:]


def clean():
    for f in list(os.listdir(OUT)):
        if f.endswith((".part", ".ytdl")) or ".part-" in f:
            try:
                os.remove(os.path.join(OUT, f))
            except Exception:
                pass


report = []
for idx, job in enumerate(JOBS):
    if time.time() - T0 > BUDGET:
        log("预算耗尽, 停止")
        break
    key = job["key"]
    dest = os.path.join(OUT, job["filename"] + ".mp4")
    if os.path.exists(dest):
        continue
    log(f"===== ({idx+1}/{len(JOBS)}) {key} =====")
    dmin, dmax = int(job.get("dmin", 0)), int(job.get("dmax", 36000))
    maxh = str(job.get("maxh", "720"))
    src = job["url"]

    if src.startswith("ytsearch:"):
        query = src[9:]
        hit = pick(query, dmin, dmax)
        if not hit:
            for p in alive_proxies(4)[:3]:
                hit = pick(query, dmin, dmax, p)
                if hit:
                    break
        if not hit:
            log(f"{key} 搜索无结果")
            report.append({"key": key, "ok": False, "why": "no-search-result"})
            continue
        log(f"选中: {hit.get('title')} | {hit.get('duration')}s")
        src = f"https://www.youtube.com/watch?v={hit['id']}"

    ok, why = download(src, dest, maxh)
    if not ok:
        log(f"直连失败({why[:80]}), 走代理")
        clean()
        for p in alive_proxies(6):
            if time.time() - T0 > BUDGET:
                break
            ok, why = download(src, dest, maxh, p, timeout=900)
            if ok:
                break
            clean()
    if ok:
        size = os.path.getsize(dest) // 1048576
        log(f"{key} ✓ {size}MB")
        report.append({"key": key, "ok": True, "file": os.path.basename(dest), "mb": size})
    else:
        log(f"{key} ✗ {why[:120]}")
        report.append({"key": key, "ok": False, "why": why[:120]})

json.dump(report, open("report.json", "w"), ensure_ascii=False, indent=1)
log(f"完成 {sum(1 for r in report if r['ok'])}/{len(JOBS)}")
