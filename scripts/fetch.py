# -*- coding: utf-8 -*-
"""
热榜采集（微博 + 抖音）
- 微博：weibo.com/ajax/side/hotSearch（需 X-Requested-With 头）
- 抖音：iesdouyin.com/web/api/v2/hotsearch/billboard/word/（无 cookie）
输出 data/raw_hotspots.json，含词条、热度、跳转链接
"""
import os, sys, json, urllib.request, urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(BASE, "data", "raw_hotspots.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 话题聚合页链接（实测结论，2026-09）：
#   s.weibo.com/weibo?q=...  未登录会被 302 到 passport.weibo.com/sso/signin → 页面空白 ❌
#   m.weibo.cn/search?containerid=100103type%3D1%26q%3D<话题>  无需登录、内容完整 ✅
WEIBO_TOPIC = "https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D"
# 抖音热点榜落地页（实测可打开）；douyin.com/search/<词> 会命中验证码中间页，
# douyin.com/hot/<词> 返回「视频不存在」，故统一指向榜单页
DOUYIN_HOTLIST = "https://so-landing.douyin.com/landings/hotlist"


def _get(url, headers=None, timeout=15):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.read()


def fetch_weibo():
    """微博热搜：返回 [(title, hot, url), ...]"""
    url = "https://weibo.com/ajax/side/hotSearch"
    data = _get(url, headers={
        "Referer": "https://weibo.com/",
        "X-Requested-With": "XMLHttpRequest",
    })
    j = json.loads(data.decode("utf-8"))
    items = []
    for it in j.get("data", {}).get("realtime", []):
        word = it.get("word") or it.get("word_scheme", "").strip("#")
        if not word:
            continue
        # 话题词用 #话题# 形式检索，非话题词用裸词，命中率最高
        scheme = (it.get("word_scheme") or "").strip()
        if scheme.startswith("#") and scheme.endswith("#") and len(scheme) > 2:
            q = scheme
        elif it.get("topic_flag"):
            q = "#" + word + "#"
        else:
            q = word
        items.append({
            "title": word,
            "hot": it.get("num", 0),
            "realpos": it.get("realpos", 0),
            "label": it.get("label_name", ""),
            "url": WEIBO_TOPIC + urllib.parse.quote(q),
        })
    return items


def fetch_douyin():
    """抖音热榜：返回 [(title, hot, url), ...]"""
    url = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
    data = _get(url, headers={"Referer": "https://so-landing.douyin.com/landings/hotlist"})
    j = json.loads(data.decode("utf-8"))
    items = []
    for it in j.get("word_list", []):
        word = it.get("word", "")
        if not word:
            continue
        items.append({
            "title": word,
            "hot": it.get("hot_value", 0),
            "label": it.get("label", ""),
            "url": DOUYIN_HOTLIST,
        })
    return items


def main():
    sources = [
        ("微博", fetch_weibo),
        ("抖音", fetch_douyin),
    ]
    last_err = None
    for name, fn in sources:
        try:
            items = fn()
            if items and len(items) >= 10:
                items = items[:50]
                out = {
                    "source": name,
                    "count": len(items),
                    "titles": [it["title"] for it in items],
                    "items": items,
                }
                os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
                with open(OUT_PATH, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)
                print(f"[OK] 来源 {name}，抓到 {len(items)} 条")
                for i, it in enumerate(items, 1):
                    print(f"  {i}. {it['title']} ({it['hot']})")
                return
            else:
                print(f"[SKIP] {name} 返回不足 10 条")
        except Exception as e:
            last_err = e
            print(f"[FAIL] {name}: {e}")
    print(f"所有数据源均失败（最后错误：{last_err}）", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
