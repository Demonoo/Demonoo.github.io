# -*- coding: utf-8 -*-
"""
热点基因分析（GLM 只做行业/形态/生命周期 + 关键词）
情感倾向由本地小模型 sentiment.py 提供，本脚本合并两者写出 hotspots.json
"""
import os, sys, json, time, urllib.request, urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

API_KEY = os.environ.get("GLM_API_KEY", "")
API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = os.environ.get("GLM_MODEL", "glm-4.7-flash")

DOMAINS = ["体育", "娱乐", "社会", "科技", "财经", "民生", "情感", "美食", "时尚",
           "健康", "教育", "汽车", "游戏", "影视", "旅游", "宠物", "国际", "其他"]
FORMS = ["短视频", "图文", "深度报道", "直播", "数据可视化", "互动话题"]
LIFECYCLE = ["短期", "中期", "长期"]

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_PATH = os.path.join(BASE, "data", "raw_hotspots.json")
SENTI_PATH = os.path.join(BASE, "data", "sentiment.json")
OUT_PATH = os.path.join(BASE, "data", "hotspots.json")


def build_prompt(keyword: str) -> str:
    return (
        "你是社交媒体热点分析专家。请对热搜词条进行「热点基因」分析。\n"
        f"热搜词条：{keyword}\n\n"
        "请从以下维度分析，并只输出一个 JSON 对象（不要 markdown 代码块、不要任何多余文字）：\n"
        f"1. 创作领域：从 {DOMAINS} 中选一个\n"
        f"2. 内容形态：从 {FORMS} 中选一个\n"
        f"3. 生命周期：从 {LIFECYCLE} 中选一个\n"
        "4. 核心话题词：3-5 个关键词\n\n"
        '输出格式（严格）：{"创作领域":"","内容形态":"","生命周期":"","核心话题词":["",""]}'
    )


def extract_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    s = t.find("{")
    e = t.rfind("}")
    if s != -1 and e != -1 and e > s:
        t = t[s:e + 1]
    return json.loads(t)


def call_glm(keyword: str, retry: int = 1):
    prompt = build_prompt(keyword)
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=payload,
        headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"},
    )
    for attempt in range(retry + 1):
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            r = json.loads(resp.read().decode("utf-8"))
            content = r["choices"][0]["message"]["content"]
            return extract_json(content)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt == retry:
                    print(f"  [RATE-LIMIT] {keyword}: 429 限流未恢复")
                    return "RATE_LIMITED"
                print(f"  [RATE-LIMIT] {keyword}: 429，等待 15s 重试")
                time.sleep(15)
            else:
                if attempt == retry:
                    print(f"  [FAIL] {keyword}: HTTP {e.code}")
                    return None
                time.sleep(2.5 * (attempt + 1))
        except Exception as e:
            if attempt == retry:
                print(f"  [FAIL] {keyword}: {e}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def main():
    if not API_KEY:
        print("缺少 GLM_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    with open(RAW_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    items_raw = raw.get("items", [])
    # 读本地情感结果
    senti_map = {}
    if os.path.exists(SENTI_PATH):
        with open(SENTI_PATH, "r", encoding="utf-8") as f:
            senti = json.load(f)
        for r in senti.get("results", []):
            senti_map[r["title"]] = r["情感倾向"]

    print(f"共 {len(items_raw)} 条热榜，GLM 分析行业/形态/生命周期...")

    items = []
    rate_streak = 0
    MAX_RATE_STREAK = 6
    for i, it in enumerate(items_raw, 1):
        t = it.get("title", "")
        r = call_glm(t)
        if r == "RATE_LIMITED":
            rate_streak += 1
            if rate_streak >= MAX_RATE_STREAK:
                print(f"\n连续 {rate_streak} 条触发限流，提前结束本轮（已成功 {len(items)} 条）")
                break
            time.sleep(1)
            continue
        rate_streak = 0
        if r:
            item = {
                "title": t,
                "情感倾向": senti_map.get(t, "中性"),
                "创作领域": r.get("创作领域", "其他"),
                "内容形态": r.get("内容形态", ""),
                "生命周期": r.get("生命周期", ""),
                "核心话题词": r.get("核心话题词", []),
                "hot": it.get("hot", 0),
                "url": it.get("url", ""),
            }
            items.append(item)
            print(f"  [{i}/{len(items_raw)}] {item['情感倾向']}/{r.get('创作领域','?')}  {t}")
        else:
            print(f"  [{i}/{len(items_raw)}] FAIL  {t}")
        time.sleep(0.4)

    # 成功条数过少时保留旧数据
    if len(items) < 10 and os.path.exists(OUT_PATH):
        print(f"\n本轮仅 {len(items)} 条成功（疑似限流），保留原有数据不覆盖", file=sys.stderr)
        sys.exit(0)

    # 聚类：按创作领域分组
    clusters = defaultdict(list)
    for it in items:
        clusters[it["创作领域"]].append(it)
    cluster_list = sorted(
        [{"领域": k, "count": len(v), "items": v} for k, v in clusters.items()],
        key=lambda x: -x["count"],
    )

    emotion_counter = Counter(it["情感倾向"] for it in items)
    summary = {
        "情感分布": dict(emotion_counter),
        "领域分布": {k: len(v) for k, v in clusters.items()},
        "总词条数": len(items),
    }

    bj = timezone(timedelta(hours=8))
    result = {
        "updated_at": datetime.now(bj).strftime("%Y-%m-%d %H:%M"),
        "source": raw.get("source", ""),
        "summary": summary,
        "clusters": cluster_list,
        "items": items,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n完成：{len(items)} 条分析成功，写入 {OUT_PATH}")
    print("领域分布：", summary["领域分布"])
    print("情感分布：", summary["情感分布"])


if __name__ == "__main__":
    main()
