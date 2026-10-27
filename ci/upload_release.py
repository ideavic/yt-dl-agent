#!/usr/bin/env python3
"""把本构建产物上传到 GitHub Releases: 绕开 AppVeyor artifact 1GB/天下载配额。
公共仓库 Releases 免费、单文件上限 2GB、无日流量限制。
需要环境变量 GH_TOKEN(有 repo 权限); 没有则静默跳过, 走 artifacts 兜底。"""
import json
import os
import urllib.parse
import urllib.request

REPO = "ideavic/yt-dl-agent"
TOKEN = os.environ.get("GH_TOKEN", "").strip()
TAG = "av-v" + os.environ.get("APPVEYOR_BUILD_VERSION", "0")


def api(url, method="GET", body=None, ctype="application/json"):
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json",
                 "Content-Type": ctype, "User-Agent": "ci-uploader"},
        method=method)
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    with urllib.request.urlopen(req, data=data, timeout=300) as r:
        txt = r.read()
    return json.loads(txt) if txt[:1] in (b"{", b"[") else {}


def main():
    files = sorted(os.path.join("out", f) for f in os.listdir("out") if f.endswith(".mp4"))
    files += sorted(os.path.join("qc", f) for f in os.listdir("qc") if f.endswith(".jpg"))
    if os.path.exists("report.json"):
        files.append("report.json")
    files = [f for f in files if os.path.getsize(f) > 0]
    if not TOKEN:
        print("GH_TOKEN 未设置, 跳过 Releases 上传(走 artifacts 兜底)")
        return
    if not files:
        print("本构建无产物, 不建 release")
        return
    rel = None
    try:
        rel = api(f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}")
    except Exception:
        rel = None
    if not rel or "upload_url" not in rel:
        rel = api(f"https://api.github.com/repos/{REPO}/releases", "POST",
                  {"tag_name": TAG, "name": f"CI batch {TAG}", "prerelease": True})
    have = {a["name"] for a in rel.get("assets", [])}
    total = 0
    for f in files:
        name = os.path.basename(f)
        if name in have:
            print("已存在, 跳过:", name)
            continue
        data = open(f, "rb").read()
        up = rel["upload_url"].split("{")[0] + "?name=" + urllib.parse.quote(name)
        api(up, "POST", data, ctype="application/octet-stream")
        total += len(data)
        print(f"上传: {name} {len(data)//1048576}MB")
    print(f"Releases 上传完成, 共 {total//1048576}MB / {len(files)} 个文件")


if __name__ == "__main__":
    main()
