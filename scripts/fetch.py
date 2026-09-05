# -*- coding: utf-8 -*-
"""
热榜采集（多源容错）
依次尝试：微博 → 百度 → 今日头条，第一个成功即返回
输出 data/raw_hotspots.json
"""
import os, sys, json, gzip, re, urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(BASE, "data", "raw_hotspots.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _get(url, headers=None, timeout=15):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = resp.read()
    # 处理 gzip
    if resp.headers.get("Content-Encoding", "") == "gzip":
        data = gzip.decompress(data)
    return data


def fetch_weibo():
    """微博热搜（新版接口，可能需 cookie，海外 IP 可能受限）"""
    url = "https://weibo.com/ajax/side/hotSearch"
    data = _get(url, headers={"Referer": "https://weibo.com/"})
    j = json.loads(data.decode("utf-8"))
    titles = [item["word"] for item in j.get("data", {}).get("realtime", []) if item.get("word")]
    return titles


def fetch_baidu():
    """百度热搜"""
    url = "https://top.baidu.com/api/board?platform=wise&tab=realtime"
    data = _get(url)
    j = json.loads(data.decode("utf-8"))
    titles = []
    for card in j.get("data", {}).get("cards", []):
        for c in card.get("content", []):
            w = c.get("word") or c.get("query")
            if w:
                titles.append(w)
    return titles


def fetch_toutiao():
    """今日头条热榜"""
    url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
    data = _get(url)
    j = json.loads(data.decode("utf-8"))
    titles = [item.get("Title", "") for item in j.get("data", []) if item.get("Title")]
    return titles


def main():
    sources = [
        ("微博", fetch_weibo),
        ("百度", fetch_baidu),
        ("今日头条", fetch_toutiao),
    ]
    for name, fn in sources:
        try:
            titles = fn()
            if titles and len(titles) >= 10:
                titles = titles[:50]
                out = {"source": name, "count": len(titles), "titles": titles}
                os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
                with open(OUT_PATH, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)
                print(f"[OK] 来源 {name}，抓到 {len(titles)} 条")
                for i, t in enumerate(titles, 1):
                    print(f"  {i}. {t}")
                return
            else:
                print(f"[SKIP] {name} 返回不足 10 条")
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
    print("所有数据源均失败", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
