# -*- coding: utf-8 -*-
"""
热点基因分析：调用云端大模型，对每条热榜词条输出
创作领域 / 内容形态 / 生命周期 / 核心话题词

- 情感倾向由本地小模型 sentiment.py 提供，本脚本只负责领域维度并合并两者
- 所有分析结果均来自实时模型推理，无本地兜底、无规则伪造

接口：Ollama Cloud（OpenAI 兼容）
      POST https://ollama.com/v1/chat/completions
      免费档实测可用：gpt-oss:20b（4.5s/条、JSON 稳定、中文分类准确）
      需订阅的模型（glm-5.x / deepseek-v4 / kimi / minimax / mistral-large 等）返回 402

设计要点（配合 20 分钟一次的高频调度）：
1. 增量缓存：上一轮已分析过的词条直接复用结果，只有新上榜的词条才调模型
   —— 热榜每 20 分钟变化很小，调用量从 50 次/轮降到个位数
2. 条数完整：**输出条数恒等于抓到的热榜条数**，不会因限流被截断
3. 限流熔断：连续多次限流则本轮停止调用，剩余词条沿用缓存（无缓存则留空待下轮补齐）
4. 无变化不写盘：内容与上一轮完全一致时不写文件，避免每 20 分钟产生空提交
"""
import os, sys, json, time, urllib.request, urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("LLM_API_KEY")
           or os.environ.get("OLLAMA_API_KEY")
           or os.environ.get("GLM_API_KEY")
           or "")
API_URL = os.environ.get("LLM_API_URL", "https://ollama.com/v1/chat/completions")
MODEL = os.environ.get("LLM_MODEL", "gpt-oss:20b")

DOMAINS = ["体育", "娱乐", "社会", "科技", "财经", "民生", "情感", "美食", "时尚",
           "健康", "教育", "汽车", "游戏", "影视", "旅游", "宠物", "国际", "其他"]
FORMS = ["短视频", "图文", "深度报道", "直播", "数据可视化", "互动话题"]
LIFECYCLE = ["短期", "中期", "长期"]

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_PATH = os.path.join(BASE, "data", "raw_hotspots.json")
SENTI_PATH = os.path.join(BASE, "data", "sentiment.json")
OUT_PATH = os.path.join(BASE, "data", "hotspots.json")

MAX_RATE_STREAK = 3      # 连续限流达到此数即熔断，本轮不再调用
MAX_CALLS_PER_RUN = 60   # 单轮调用上限，防止失控
REQUEST_TIMEOUT = 90


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
    t = (text or "").strip()
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


def call_llm(keyword: str):
    """返回分析 dict / "RATE_LIMITED" / None"""
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": build_prompt(keyword)}],
        "temperature": 0.3,
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY

    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(API_URL, data=payload, headers=headers)
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            r = json.loads(resp.read().decode("utf-8"))
            content = r["choices"][0]["message"]["content"]
            j = extract_json(content)
            return {
                "创作领域": j.get("创作领域", "") or "",
                "内容形态": j.get("内容形态", "") or "",
                "生命周期": j.get("生命周期", "") or "",
                "核心话题词": j.get("核心话题词", []) or [],
            }
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:120]
            except Exception:
                pass
            last = f"HTTP {e.code} {body}"
            if e.code in (429, 402, 503):
                if attempt < 2:
                    time.sleep(4 * (attempt + 1))
                    continue
                print(f"  [LIMIT] {keyword}: {last}")
                return "RATE_LIMITED"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    print(f"  [FAIL] {keyword}: {last}")
    return None


def load_prev(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        return {}
    return {it.get("title"): it for it in prev.get("items", []) if it.get("title")}


def signature(result) -> str:
    return json.dumps({
        "source": result.get("source"),
        "summary": result.get("summary"),
        "clusters": result.get("clusters"),
        "items": result.get("items"),
    }, ensure_ascii=False, sort_keys=True)


def main():
    if not API_KEY:
        print("缺少 LLM_API_KEY / OLLAMA_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    with open(RAW_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    items_raw = raw.get("items", [])
    if not items_raw:
        print("raw_hotspots.json 中没有 items", file=sys.stderr)
        sys.exit(1)

    senti_map = {}
    if os.path.exists(SENTI_PATH):
        with open(SENTI_PATH, "r", encoding="utf-8") as f:
            senti = json.load(f)
        for r in senti.get("results", []):
            senti_map[r["title"]] = r["情感倾向"]

    prev_map = load_prev(OUT_PATH)
    print(f"共 {len(items_raw)} 条热榜；上一轮缓存 {len(prev_map)} 条；模型 {MODEL}")

    items = []
    reused = called = failed = 0
    rate_streak = 0
    blocked = False

    for i, it in enumerate(items_raw, 1):
        t = it.get("title", "")
        if not t:
            continue

        cached = prev_map.get(t)
        # 旧数据没有「分析来源」字段，一律视为已由模型分析过；
        # 历史上被标记为「本地」的（关键词规则产物）必须重新用模型分析
        src = (cached or {}).get("分析来源", "LLM")
        cached_ok = bool(cached and src in ("LLM", "GLM") and cached.get("创作领域"))
        analysis = None

        if cached_ok:
            analysis = {
                "创作领域": cached.get("创作领域", ""),
                "内容形态": cached.get("内容形态", ""),
                "生命周期": cached.get("生命周期", ""),
                "核心话题词": cached.get("核心话题词", []),
            }
            reused += 1
        elif blocked or called >= MAX_CALLS_PER_RUN:
            analysis = None
        else:
            r = call_llm(t)
            called += 1
            if r == "RATE_LIMITED":
                rate_streak += 1
                if rate_streak >= MAX_RATE_STREAK:
                    blocked = True
                    print(f"  连续 {rate_streak} 次限流，本轮停止调用模型，"
                          f"剩余词条沿用缓存（无缓存则留空，下轮补齐）")
            elif r:
                rate_streak = 0
                analysis = r
            time.sleep(0.2)

        emo = senti_map.get(t) or (cached or {}).get("情感倾向") or "中性"
        if analysis:
            items.append({
                "title": t,
                "情感倾向": emo,
                "创作领域": analysis["创作领域"],
                "内容形态": analysis["内容形态"],
                "生命周期": analysis["生命周期"],
                "核心话题词": analysis["核心话题词"],
                "分析来源": "LLM",
                "url": it.get("url", ""),
            })
        else:
            failed += 1
            items.append({
                "title": t,
                "情感倾向": emo,
                "创作领域": "",
                "内容形态": "",
                "生命周期": "",
                "核心话题词": [],
                "分析来源": "",
                "url": it.get("url", ""),
            })

    solid = sum(1 for x in items if x["创作领域"])
    print(f"共 {len(items)} 条：复用缓存 {reused} 条 / 本轮调用模型 {called} 次 / "
          f"待下轮补齐 {failed} 条；已分析 {solid} 条")

    if solid == 0 and prev_map:
        print("本轮全部分析失败，保留原有数据不覆盖", file=sys.stderr)
        sys.exit(0)

    clusters = defaultdict(list)
    for it in items:
        clusters[it["创作领域"] or "未分析"].append(it)
    cluster_list = sorted(
        [{"领域": k, "count": len(v), "items": v} for k, v in clusters.items()],
        key=lambda x: -x["count"],
    )
    summary = {
        "情感分布": dict(Counter(it["情感倾向"] for it in items)),
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

    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            if signature(old) == signature(result):
                print("内容与上一轮一致，保持原文件不变（不产生提交）")
                return
        except Exception:
            pass

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"完成：{len(items)} 条写入 {OUT_PATH}")
    print("领域分布：", summary["领域分布"])
    print("情感分布：", summary["情感分布"])


if __name__ == "__main__":
    main()
