#!/usr/bin/env python3
"""AppVeyor 端批量抓取: 读 queue.json, 逐条下载, 结果放 out/"""
import concurrent.futures
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time

import qc

OUT = "out"
TMP = "work"
os.makedirs(OUT, exist_ok=True)
os.makedirs(TMP, exist_ok=True)
os.makedirs("qc", exist_ok=True)

QUEUE = json.load(open("queue.json"))
JOBS = QUEUE.get("jobs", [])
BUDGET = int(os.environ.get("BUDGET", "2700"))
T0 = time.time()

_BASE = ["yt-dlp", "--no-warnings", "--socket-timeout", "20",
         "--no-part", "--http-chunk-size", "5M", "--retries", "2",
         "--js-runtimes", "node", "--remote-components", "ejs:github",
         "--sleep-requests", "1"]

CLIENTS = [None, "android", "ios", "tv", "web_safari"]
PROXY_ONLY = os.environ.get("PROXY_ONLY", "0") == "1"
if PROXY_ONLY:
    CLIENTS = []


def ytdlp_args(client=None):
    extra = "youtube:fetch_pot=always" + (f";player_client={client}" if client else "")
    return _BASE + ["--extractor-args", extra]


YTDLP = ytdlp_args(None)

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


_STOP = {"the", "a", "an", "of", "for", "to", "and", "in", "on", "with", "how",
         "what", "is", "are", "this", "that", "your", "you", "it", "best", "new"}


def relevant(title, query, need=0.4):
    """标题必须覆盖查询词的一半以上实词, 防止 agent->特工/影评 这类误配"""
    toks = [w for w in re.findall(r"[a-z]{3,}", query.lower()) if w not in _STOP]
    if not toks:
        return True
    t = title.lower()
    hit = sum(1 for w in toks if w in t)
    return hit / len(toks) >= need


def pick_all(query, dmin, dmax, proxy=None, limit=6):
    """返回按评分排序的候选列表 (供逐个质检换源)"""
    cmd = YTDLP + (["--proxy", proxy] if proxy else []) + [
        "--flat-playlist", "--dump-single-json", f"ytsearch10:{query}"]
    r = sh(cmd, 90)
    if not r or r.returncode != 0 or not (r.stdout or "").strip().startswith("{"):
        return []
    try:
        entries = json.loads(r.stdout).get("entries") or []
    except Exception:
        return []
    scored = []
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
        if not relevant(title, query):
            continue
        score = 2
        if any(c in ch for c in CHANNEL_BONUS):
            score += 5
        if 60 <= dur <= 1200:
            score += 2
        if (e.get("view_count") or 0) > 10000:
            score += 1
        scored.append((score, dur, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, _, e in scored[:limit]]


_POOL = None
_POOL_LOCK = threading.Lock()


def alive_proxies(limit=8):
    global _POOL
    if _POOL is not None:
        return _POOL
    if not _POOL_LOCK.acquire(blocking=False):
        # 另一个线程正在探测: 等它出结果
        with _POOL_LOCK:
            pass
        return _POOL or []
    try:
        return _discover_proxies(limit)
    finally:
        _POOL_LOCK.release()


def _discover_proxies(limit=8):
    global _POOL
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

    def hostport(p):
        body = p.split("://", 1)[-1].split("@")[-1]
        h, _, pt = body.partition(":")
        return h, int(pt) if pt.isdigit() else 0

    def tcp_ok(p):
        """纯 socket 连通性预筛: 不 fork 子进程, 千级候选几秒完成"""
        h, pt = hostport(p)
        if not h or not pt:
            return None
        try:
            s = socket.create_connection((h, pt), timeout=1.2)
            s.close()
            return p
        except Exception:
            return None

    # 阶段1: TCP 连通快筛(候选截断, 存活即止)
    pool = proxies[:3000]
    tcp_alive = []
    with concurrent.futures.ThreadPoolExecutor(400) as ex:
        futs = [ex.submit(tcp_ok, p) for p in pool]
        for f in concurrent.futures.as_completed(futs):
            try:
                p = f.result()
            except Exception:
                p = None
            if p:
                tcp_alive.append(p)
    log(f"TCP 连通 {len(tcp_alive)}")
    if not tcp_alive:
        _POOL = []
        return _POOL

    random.shuffle(tcp_alive)

    def test(p):
        r = sh(["curl", "-s", "-x", p, "--max-time", "5", "-o", "/dev/null",
                "-w", "%{http_code}", "https://www.youtube.com/generate_204"], 9)
        return p if r and r.stdout.strip().startswith("2") else None

    # 阶段2: 只对 TCP 存活的验证能否出网到 YouTube, 凑够即停
    alive = []
    cand = tcp_alive[:600]
    with concurrent.futures.ThreadPoolExecutor(80) as ex:
        futs = [ex.submit(test, p) for p in cand]
        try:
            for f in concurrent.futures.as_completed(futs, timeout=120):
                try:
                    p = f.result()
                except Exception:
                    p = None
                if p:
                    alive.append(p)
                    if len(alive) >= limit * 2:
                        break
        except concurrent.futures.TimeoutError:
            log("代理验证超时, 用已有结果")
        for f in futs:
            f.cancel()
    random.shuffle(alive)
    log(f"对 YouTube 存活 {len(alive)}")
    _POOL = alive[:limit]
    return _POOL


def download(url, dest, maxh, proxy=None, timeout=600, client=None):
    cmd = ytdlp_args(client) + (["--proxy", proxy] if proxy else []) + [
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


REPO = "ideavic/yt-dl-agent"
DONE_URLS = [
    f"https://raw.githubusercontent.com/{REPO}/main/done.json",
    f"https://api.github.com/repos/{REPO}/contents/done.json",
]


def fetch_done():
    """已入库槽位清单: 排队中的重复构建据此跳过, 避免多开重复下载"""
    for url in DONE_URLS:
        r = sh(["curl", "-s", "-L", "--max-time", "25", url], 40)
        if not r or r.returncode != 0 or not r.stdout.strip():
            continue
        txt = r.stdout.strip()
        try:
            d = json.loads(txt)
            if isinstance(d, list):
                return set(d)
            # contents API 返回 base64
            import base64
            d = json.loads(base64.b64decode(d.get("content", "")).decode())
            if isinstance(d, list):
                return set(d)
        except Exception:
            continue
    return set()


PROBE_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"   # 19 秒老视频, 解析最便宜


def direct_available():
    """20 秒内试解析一次: 通了说明机房 IP 没被拦, 直连比代理快 20 倍"""
    try:
        r = sh(ytdlp_args(None) + ["--socket-timeout", "12", "--skip-download",
                                   "--print", "%(id)s", PROBE_VIDEO], 45)
    except Exception:
        return False
    return bool(r) and r.returncode == 0 and "jNQXAC9IVRw" in (r.stdout or "")


report = []
_REPORT_LOCK = threading.Lock()
JOB_PAR = int(os.environ.get("JOB_PAR", "2"))     # 并发任务数(每条走不同代理, 2 比较稳)


def add_report(item):
    with _REPORT_LOCK:
        report.append(item)
        json.dump(report, open("report.json", "w"), ensure_ascii=False, indent=1)


_done = fetch_done()
_left = [j for j in JOBS if j["key"] not in _done]

# 开工先探一次直连: 机房 IP 没被拦就用直连(比代理池快约 20 倍)
if PROXY_ONLY and os.environ.get("PROBE_DIRECT", "1") == "1":
    log("直连探测中…")
    if direct_available():
        PROXY_ONLY = False
        CLIENTS = [None, "android", "ios", "tv", "web_safari"]
        log("直连可用 → 本构建走直连")
    else:
        log("直连被拦 → 走代理池")

log(f"队列 {len(JOBS)} 条, 已完成 {len(JOBS) - len(_left)} 条, 待做 {len(_left)} 条, 并发 {JOB_PAR}")
if not _left:
    log("本批已全部完成(别的构建已收), 直接退出")
    json.dump([], open("report.json", "w"))
    raise SystemExit(0)


def run_job(idx, job):
    """跑一条任务, 结果写进 report; 返回 True 表示有产出"""
    if time.time() - T0 > BUDGET:
        return False
    key = job["key"]
    dest = os.path.join(OUT, job["filename"] + ".mp4")
    if os.path.exists(dest):
        return False
    if key in fetch_done():
        log(f"{key} 已由其他构建完成, 跳过")
        return False
    log(f"===== ({idx+1}/{len(JOBS)}) {key} =====")
    dmin, dmax = int(job.get("dmin", 0)), int(job.get("dmax", 36000))
    maxh = str(job.get("maxh", "720"))
    src = job["url"]

    if src.startswith("ytsearch:"):
        query = src[9:]
        cands = pick_all(query, dmin, dmax)
        if not cands:
            for p in alive_proxies(4)[:3]:
                cands = pick_all(query, dmin, dmax, p)
                if cands:
                    break
        if not cands:
            log(f"{key} 搜索无结果")
            add_report({"key": key, "ok": False, "why": "no-search-result"})
            return False
    else:
        cands = [{"id": src.rsplit("=", 1)[-1], "title": "", "duration": 0}]

    # ===== 逐个候选: 取样质检(便宜) -> 通过才正式下载(贵) =====
    picked_title = ""
    ok, why = False, "qc-rejected"
    qc_log = []
    for ci, c in enumerate(cands):
        if time.time() - T0 > BUDGET:
            break
        vurl = f"https://www.youtube.com/watch?v={c['id']}" if c.get("id") else src
        title = c.get("title") or ""
        dur = c.get("duration") or 0
        log(f"候选{ci+1}/{len(cands)}: {title[:60]} | {dur}s")
        # 每条任务用不同的代理起点, 避免并发任务抢同一个代理
        proxy_list = list(alive_proxies(8)) if PROXY_ONLY else []
        if proxy_list:
            off = (hash(key) + ci) % len(proxy_list)
            proxy_list = proxy_list[off:] + proxy_list[:off]
        work = os.path.join(TMP, f"{job['filename']}_c{ci}")
        os.makedirs(work, exist_ok=True)
        samples, serr = [], ""
        if PROXY_ONLY:
            for pr in proxy_list:
                samples, serr = qc.grab_samples(vurl, ytdlp_args(None), work, "s", dur, proxy=pr)
                if samples:
                    log(f"    代理取样成功: {pr}")
                    break
        for cl in CLIENTS:
            samples, serr = qc.grab_samples(vurl, ytdlp_args(cl), work, "s", dur)
            if samples:
                break
            if "not a bot" not in serr and "Sign in" not in serr and "cookies" not in serr:
                break
            log(f"    bot校验拦截, 换客户端 {cl} 重试")
        if not samples and ("bot" in serr.lower() or "Sign in" in serr or "cookies" in serr):
            log("    换代理取样")
            for pr in alive_proxies(6)[:5]:
                samples, serr = qc.grab_samples(vurl, ytdlp_args("android"), work, "s", dur, proxy=pr)
                if samples:
                    log(f"    代理可用: {pr}")
                    break
        frames = []
        for si, sp in enumerate(samples):
            frames += qc.extract_frames(sp, work, f"f{si}", n=8)
        if len(frames) < 3:
            log(f"  取样失败(samples={len(samples)} frames={len(frames)} err={serr[:100]}), 换源")
            qc_log.append({"cand": title, "pass": False,
                           "why": f"sample-failed s={len(samples)} f={len(frames)} {serr[:120]}"})
            continue
        qc.build_strip(frames, os.path.join("qc", f"{job['filename']}_c{ci}.jpg"))
        passed, reasons, metrics = qc.check_frames(frames)
        qc_log.append({"cand": title, "pass": passed,
                       "why": "; ".join(reasons) or "ok", "metrics": metrics})
        if not passed:
            log(f"  质检不过: {'; '.join(reasons)[:90]} → 换源")
            continue
        log("  质检通过, 开始正式下载")
        ok, why = False, "skipped"
        if PROXY_ONLY:
            for pr in proxy_list:
                ok, why = download(vurl, dest, maxh, pr, timeout=900)
                if ok:
                    log(f"    代理下载成功: {pr}")
                    break
                clean()
        if not ok and not PROXY_ONLY:
            ok, why = download(vurl, dest, maxh)
        if not ok and ("not a bot" in why or "Sign in" in why):
            for cl in CLIENTS[1:]:
                clean()
                ok, why = download(vurl, dest, maxh, client=cl)
                if ok:
                    break
        if not ok:
            log(f"  直连失败({why[:60]}), 走代理")
            clean()
            for p in alive_proxies(4):
                if time.time() - T0 > BUDGET:
                    break
                ok, why = download(vurl, dest, maxh, p, timeout=900)
                if ok:
                    break
                clean()
        if ok:
            strip = qc.build_strip(frames, os.path.join("qc", job["filename"] + ".jpg"))
            picked_title = title
            log(f"  留档拼图: {strip}")
            break

    if ok:
        size = os.path.getsize(dest) // 1048576
        log(f"{key} ✓ {size}MB")
        add_report({"key": key, "ok": True, "file": os.path.basename(dest),
                    "mb": size, "title": picked_title, "qc": qc_log})
        return True
    log(f"{key} ✗ {why[:120]}")
    add_report({"key": key, "ok": False, "why": why[:120], "qc": qc_log})
    return False


def _worker(idx, job):
    try:
        return run_job(idx, job)
    except Exception as e:
        log(f"{job.get('key')} 任务异常: {e}")
        add_report({"key": job.get("key"), "ok": False, "why": f"exception: {e}"})
        return False


# 并发跑: 每条任务独立代理 + 独立工作目录, 2 路并发把单构建产出翻倍
if JOB_PAR <= 1:
    for idx, job in enumerate(JOBS):
        if time.time() - T0 > BUDGET:
            log("预算耗尽, 停止")
            break
        _worker(idx, job)
        time.sleep(20)   # 任务间降温
else:
    with concurrent.futures.ThreadPoolExecutor(max_workers=JOB_PAR) as ex:
        futs = []
        for idx, job in enumerate(JOBS):
            if time.time() - T0 > BUDGET:
                log("预算耗尽, 不再派新任务")
                break
            futs.append(ex.submit(_worker, idx, job))
            time.sleep(8)          # 错开启动, 别同时打搜索接口
        for f in concurrent.futures.as_completed(futs):
            try:
                f.result()
            except Exception as e:
                log(f"任务异常: {e}")

log(f"完成 {sum(1 for r in report if r['ok'])}/{len(JOBS)}")
json.dump(report, open("report.json", "w"), ensure_ascii=False, indent=1)
