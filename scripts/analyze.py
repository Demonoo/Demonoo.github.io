# -*- coding: utf-8 -*-
"""
热点基因图谱分析管线
- 读取 data/raw_hotspots.json（热榜标题列表）
- 逐条调用 GLM-4-Flash 做四维分析（情感/领域/形态/生命周期）
- 按"创作领域"聚类
- 输出 data/hotspots.json 供前端渲染
"""
import os, sys, json, time, urllib.request, urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

API_KEY = os.environ.get("GLM_API_KEY", "")
API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = os.environ.get("GLM_MODEL", "glm-4-flash")

# 预定义领域枚举（让 LLM 从固定标签选，避免自由发挥导致标签不一致）
DOMAINS = ["体育", "娱乐", "社会", "科技", "财经", "民生", "情感", "美食", "时尚",
           "健康", "教育", "汽车", "游戏", "影视", "旅游", "宠物", "国际", "其他"]
EMOTIONS = ["振奋", "骄傲", "期待", "中立", "担忧", "愤怒", "悲伤", "惊讶", "开心"]
FORMS = ["短视频", "图文", "深度报道", "直播", "数据可视化", "互动话题"]
LIFECYCLE = ["短期", "中期", "长期"]

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_PATH = os.path.join(BASE, "data", "raw_hotspots.json")
OUT_PATH = os.path.join(BASE, "data", "hotspots.json")


def build_prompt(keyword: str) -> str:
    return (
        "你是社交媒体热点分析专家。请对微博热搜词条进行「热点基因图谱」分析。\n"
        f"热搜词条：{keyword}\n\n"
        "请从以下维度分析，并只输出一个 JSON 对象（不要 markdown 代码块、不要任何多余文字）：\n"
        f"1. 情感倾向：从 {EMOTIONS} 中选一个\n"
        f"2. 创作领域：从 {DOMAINS} 中选一个\n"
        f"3. 内容形态：从 {FORMS} 中选一个\n"
        f"4. 生命周期：从 {LIFECYCLE} 中选一个\n"
        "5. 核心话题词：3-5 个关键词\n\n"
        '输出格式（严格）：{"情感倾向":"","创作领域":"","内容形态":"","生命周期":"","核心话题词":["",""]}'
    )


def extract_json(text: str):
    """从可能被 ```json ... ``` 包裹的返回里稳健提取 JSON"""
    t = text.strip()
    if t.startswith("```"):
        # 去掉首行 ```json 和末行 ```
        lines = t.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    # 兜底：截取第一个 { 到最后一个 }
    s = t.find("{")
    e = t.rfind("}")
    if s != -1 and e != -1 and e > s:
        t = t[s:e + 1]
    return json.loads(t)


def call_glm(keyword: str, retry: int = 2):
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
    titles = raw.get("titles", [])
    print(f"共 {len(titles)} 条热榜，开始分析...")

    items = []
    for i, t in enumerate(titles, 1):
        r = call_glm(t)
        if r:
            item = {
                "title": t,
                "情感倾向": r.get("情感倾向", "中立"),
                "创作领域": r.get("创作领域", "其他"),
                "内容形态": r.get("内容形态", ""),
                "生命周期": r.get("生命周期", ""),
                "核心话题词": r.get("核心话题词", []),
            }
            items.append(item)
        print(f"  [{i}/{len(titles)}] {r.get('情感倾向','?') if r else 'FAIL'}  {t}")
        if i % 20 == 0:
            time.sleep(0.5)

    # 聚类：按创作领域分组
    clusters = defaultdict(list)
    for it in items:
        clusters[it["创作领域"]].append(it)
    cluster_list = sorted(
        [{"领域": k, "count": len(v), "items": v} for k, v in clusters.items()],
        key=lambda x: -x["count"],
    )

    # 汇总统计
    emotion_counter = Counter(it["情感倾向"] for it in items)
    summary = {
        "情感分布": dict(emotion_counter),
        "领域分布": {k: len(v) for k, v in clusters.items()},
        "总词条数": len(items),
    }

    # 北京时间
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
