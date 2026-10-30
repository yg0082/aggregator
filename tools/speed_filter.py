#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测速筛选 + 地区分组（aggregator 小切口试用脚本，不改核心爬虫代码）

用法：
    python tools/speed_filter.py input.txt -o output/
输入：每行一个 IP，或 IP:端口，也兼容 "IP:端口#备注" 这种行（# 后面会被去掉）。
输出：
    output/filtered.txt          全部成功项，按延迟从低到高排序，格式 IP:端口
    output/filtered_<国家代码>.txt  按国家分组，如 SG.txt、HK.txt、US.txt
    终端打印：总数 / 成功 / 失败 / 各国数量

测速方法（和“批量测IP”App 同款思路）：
    对给定 IP:端口做一次 HTTPS 请求（SNI www.cloudflare.com，路径 /cdn-cgi/trace），
    2 秒超时，返回 200-399 算成功，耗时即延迟。失败的不进输出文件，只计入失败数。
归属地：查 ip-api，只取国家，转成中文简称显示在统计里，分组文件用国家代码命名。
只用 Python 标准库，不用额外装包，GitHub Actions / 本地 Python 都能跑。
"""
import argparse
import concurrent.futures
import json
import os
import re
import ssl
import sys
import time
import urllib.request

COUNTRY_ZH = {
    "Hong Kong": "香港", "HK": "香港",
    "Singapore": "新加坡", "SG": "新加坡",
    "United States": "美国", "US": "美国",
    "Germany": "德国", "DE": "德国",
    "Japan": "日本", "JP": "日本",
    "China": "中国", "CN": "中国",
    "South Korea": "韩国", "KR": "韩国",
    "United Kingdom": "英国", "GB": "英国",
    "France": "法国", "FR": "法国",
    "Netherlands": "荷兰", "NL": "荷兰",
    "Canada": "加拿大", "CA": "加拿大",
    "Australia": "澳大利亚", "AU": "澳大利亚",
    "Taiwan": "台湾", "TW": "台湾",
}
IP_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+|[0-9a-fA-F:]+)(?::(\d+))?$")


def parse_line(line):
    line = line.strip().split("#", 1)[0].strip()
    if not line:
        return None
    m = IP_RE.match(line)
    if not m:
        return None
    ip, port = m.group(1), int(m.group(2)) if m.group(2) else 443
    return ip, port


def https_latency_ms(ip, port, timeout=2):
    # 只测给定端口，不擅自换端口，这样导出结果和输入端口一一对应。
    # 关键：必须用 SNI www.cloudflare.com 直连 IP，否则 Cloudflare 会直接拒绝，
    # 这也是上一版用 urllib 直接访问 IP 导致全失败的原因，已改成 socket + SNI。
    import socket
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    try:
        raw = socket.create_connection((ip, port), timeout=timeout)
        with ctx.wrap_socket(raw, server_hostname="www.cloudflare.com") as s:
            s.settimeout(timeout)
            s.sendall(b"GET /cdn-cgi/trace HTTP/1.1\r\nHost: www.cloudflare.com\r\nUser-Agent: speed-filter/1.0\r\nConnection: close\r\n\r\n")
            data = s.recv(4096)
        if not data:
            return -1
        code = int(data.split(b" ", 2)[1])
        return int((time.time() - t0) * 1000) if 200 <= code <= 399 else -1
    except Exception:
        return -1


def lookup_country(ip, timeout=3):
    try:
        req = urllib.request.Request(
            f"https://ip-api.com/json/{ip}?fields=status,country,countryCode",
            headers={"User-Agent": "speed-filter/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        if d.get("status") == "success":
            return d.get("countryCode", "UNKNOWN"), d.get("country", "")
    except Exception:
        pass
    return "UNKNOWN", ""


def test_one(item):
    ip, port = item
    lat = https_latency_ms(ip, port)
    if lat < 0:
        return None
    code, country = lookup_country(ip)
    return {"ip": ip, "port": port, "latency_ms": lat, "code": code, "country": country}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default="output")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    items = []
    seen = set()
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            p = parse_line(line)
            if p and p not in seen:
                seen.add(p)
                items.append(p)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for r in pool.map(test_one, items):
            if r:
                results.append(r)
    results.sort(key=lambda x: x["latency_ms"])

    os.makedirs(args.output, exist_ok=True)
    with open(os.path.join(args.output, "filtered.txt"), "w", encoding="utf-8") as f:
        for r in results:
            f.write(f"{r['ip']}:{r['port']}\n")

    groups = {}
    for r in results:
        groups.setdefault(r["code"], []).append(r)
    for code, lst in groups.items():
        with open(os.path.join(args.output, f"filtered_{code}.txt"), "w", encoding="utf-8") as f:
            for r in lst:
                f.write(f"{r['ip']}:{r['port']}\n")

    print(f"总数 {len(items)}，成功 {len(results)}，失败 {len(items) - len(results)}")
    for code, lst in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        zh = COUNTRY_ZH.get(lst[0]["country"] or code, lst[0]["country"] or code)
        print(f"  {code}（{zh}）：{len(lst)} 条，最快 {lst[0]['latency_ms']}ms")
    if not results:
        print("没有成功项，请检查输入端口是否正确、网络是否通。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
