#!/usr/bin/env python3
"""Collect public Cloudflare preferred IPs, split by region, and generate
subscription B (auto VLESS nodes from the pool + template).

Reads:
  GIST_PAT   - GitHub PAT with gist scope (repo secret)
  GIST_LINK  - "username/gist_id" (repo secret)
  VLESS_UUID - the VLESS template UUID (repo secret, never in code)

Writes to the aggregator Gist:
  preferred_ips_<region>.txt  - per-region pool (hk/sg/jp/us/tw/kr)
  preferred_ips_all.txt       - full deduped pool, one IP per line
  clash.yaml / singbox.json   - subscription B (auto nodes)

Caps: MAX_PER_REGION = 250, MAX_TOTAL = 1000 (user-set, 2026-11-14).
Subscription B is built ONLY from the per-region lists, capped at
MAX_SUB_PER_REGION = 50 per region (user-set, 2026-11-14: the nodes are
untested, so 700+ in a subscription is useless). The full pool still goes
to preferred_ips_all.txt for copying in the space.
Sources that fail are skipped and logged; the run still succeeds.
Only the standard library is used (no pip install needed).
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error

IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

MAX_PER_REGION = 250
MAX_TOTAL = 1000
MAX_SUB_PER_REGION = 50
REGIONS = ["hk", "sg", "jp", "us", "tw", "kr"]

# (region or None for global, url)
SOURCES = [
    ("hk", "https://bestcf.pages.dev/tiancheng/hk.txt"),
    ("sg", "https://bestcf.pages.dev/tiancheng/sg.txt"),
    ("jp", "https://bestcf.pages.dev/tiancheng/jp.txt"),
    ("us", "https://bestcf.pages.dev/tiancheng/us.txt"),
    ("tw", "https://bestcf.pages.dev/tiancheng/tw.txt"),
    ("kr", "https://bestcf.pages.dev/tiancheng/kr.txt"),
    (None, "https://bestcf.pages.dev/wetest/ipv4.txt"),
    (None, "https://bestcf.pages.dev/uouin/all.txt"),
    (None, "https://bestcf.pages.dev/cfyes/ipv4.txt"),
    (None, "https://bestcf.pages.dev/tiancheng/all.txt"),
    (None, "https://bestcf.pages.dev/vvhan/ipv4.txt"),
    (None, "https://bestcf.pages.dev/nirevil/ipv4.txt"),
    (None, "https://bestcf.pages.dev/xinyitang3/ipv4.txt"),
    (None, "https://raw.githubusercontent.com/svip-s/cloudflare_ip/main/best_ips.txt"),
    (None, "https://raw.githubusercontent.com/svip-s/cloudflare_ip/main/full_ips.txt"),
    (None, "https://raw.githubusercontent.com/love-ztm/cfip/main/best_ips.txt"),
    (None, "https://raw.githubusercontent.com/ymyuuu/IPDB/main/BestCF/bestcfv4.txt"),
    (None, "https://raw.githubusercontent.com/joname1/BestCFip/main/ipv4.txt"),
]

# Template constants (non-sensitive). UUID comes from VLESS_UUID secret.
TLS_SERVER_NAME = "260509.pages.dev"
WS_PATH = "/?ed=2560"
WS_HOST = "260509.pages.dev"
SERVER_PORT = 443
URLTEST_URL = "https://www.google.com/generate_204"
URLTEST_INTERVAL = "5m"


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def valid_ipv4(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def collect():
    per_region = {r: set() for r in REGIONS}
    global_pool = set()
    for region, url in SOURCES:
        try:
            body = fetch(url)
        except Exception as exc:  # noqa: BLE001 - log and continue
            print(f"[skip] {url}: {type(exc).__name__}: {exc}", flush=True)
            continue
        ips = {ip for ip in IP_RE.findall(body) if valid_ipv4(ip)}
        print(f"[ok] {url}: {len(ips)} ips", flush=True)
        if region:
            per_region[region] |= ips
        else:
            global_pool |= ips
    # Cap per region, then build the deduped total pool.
    capped_region = {}
    for region in REGIONS:
        capped_region[region] = sorted(per_region[region])[:MAX_PER_REGION]
    total = set(global_pool)
    for region in REGIONS:
        total |= set(capped_region[region])
    total_sorted = sorted(total)[:MAX_TOTAL]
    return capped_region, total_sorted


def build_singbox(uuid, ips):
    tags = [f"auto-{ip}" for ip in ips]
    outbounds = []
    for ip, tag in zip(ips, tags):
        outbounds.append({
            "type": "vless",
            "tag": tag,
            "server": ip,
            "server_port": SERVER_PORT,
            "uuid": uuid,
            "tls": {
                "enabled": True,
                "server_name": TLS_SERVER_NAME,
                "insecure": False,
                "utls": {"enabled": True, "fingerprint": "chrome"},
            },
            "transport": {
                "type": "ws",
                "path": WS_PATH,
                "headers": {"Host": WS_HOST},
            },
            "packet_encoding": "xudp",
        })
    outbounds.append({"type": "selector", "tag": "优选", "outbounds": tags})
    outbounds.append({
        "type": "urltest",
        "tag": "自动选择",
        "outbounds": tags,
        "url": URLTEST_URL,
        "interval": URLTEST_INTERVAL,
    })
    return json.dumps({"outbounds": outbounds}, ensure_ascii=False, indent=1)


def build_clash(uuid, ips):
    lines = ["proxies:"]
    tags = []
    for ip in ips:
        tag = f"auto-{ip}"
        tags.append(tag)
        lines.append(f"  - name: {tag}")
        lines.append("    type: vless")
        lines.append(f"    server: {ip}")
        lines.append(f"    port: {SERVER_PORT}")
        lines.append(f"    uuid: {uuid}")
        lines.append("    tls: true")
        lines.append(f"    servername: {TLS_SERVER_NAME}")
        lines.append("    client-fingerprint: chrome")
        lines.append("    network: ws")
        lines.append("    ws-opts:")
        lines.append(f"      path: {WS_PATH}")
        lines.append("      headers:")
        lines.append(f"        Host: {WS_HOST}")
        lines.append("    udp: true")
    lines.append("proxy-groups:")
    lines.append("  - name: 优选")
    lines.append("    type: select")
    lines.append("    proxies:")
    for tag in tags:
        lines.append(f"      - {tag}")
    lines.append("  - name: 自动选择")
    lines.append("    type: url-test")
    lines.append("    proxies:")
    for tag in tags:
        lines.append(f"      - {tag}")
    lines.append(f"    url: {URLTEST_URL}")
    lines.append("    interval: 300")
    return "\n".join(lines) + "\n"


def update_gist(pat, gist_link, files):
    if "/" not in gist_link:
        raise ValueError("GIST_LINK must be 'username/gist_id'")
    gist_id = gist_link.rsplit("/", 1)[1].strip()
    url = f"https://api.github.com/gists/{gist_id}"
    payload = json.dumps({"files": {name: {"content": content}
                                    for name, content in files.items()}}).encode()
    req = urllib.request.Request(url, data=payload, method="PATCH", headers={
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "collect-ips",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def main():
    gist_pat = os.environ.get("GIST_PAT", "")
    gist_link = os.environ.get("GIST_LINK", "")
    uuid = os.environ.get("VLESS_UUID", "")
    if not gist_pat or not gist_link or not uuid:
        print("Error: GIST_PAT, GIST_LINK and VLESS_UUID must all be set", file=sys.stderr)
        return 1
    per_region, total = collect()
    for region in REGIONS:
        print(f"region {region}: {len(per_region[region])} ips", flush=True)
    print(f"total pool: {len(total)} ips", flush=True)
    if not total:
        print("Error: empty pool, not writing to Gist", file=sys.stderr)
        return 1
    files = {}
    for region in REGIONS:
        files[f"preferred_ips_{region}.txt"] = "\n".join(per_region[region]) + "\n"
    files["preferred_ips_all.txt"] = "\n".join(total) + "\n"
    # Subscription B: only the per-region IPs, at most 50 per region
    # (deduped across regions, since some IPs appear in several lists).
    sub_ips = set()
    for region in REGIONS:
        sub_ips |= set(per_region[region][:MAX_SUB_PER_REGION])
    sub_sorted = sorted(sub_ips)
    print(f"subscription: {len(sub_sorted)} ips "
          f"(<= {MAX_SUB_PER_REGION} per region)", flush=True)
    if not sub_sorted:
        print("Error: empty subscription list, not writing to Gist", file=sys.stderr)
        return 1
    files["singbox.json"] = build_singbox(uuid, sub_sorted)
    files["clash.yaml"] = build_clash(uuid, sub_sorted)
    status = update_gist(gist_pat, gist_link, files)
    print(f"Gist updated, HTTP {status}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
