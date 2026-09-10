# -*- coding: utf-8 -*-
"""
热点基因分析：调用云端大模型，对每条热榜词条输出
创作领域 / 内容形态 / 生命周期 / 核心话题词 / 话题性质

- 情感倾向由本地小模型 sentiment.py 提供，本脚本只负责领域维度并合并两者
- 所有分析结果均来自实时模型推理，无本地兜底、无规则伪造
- 话题性质（新闻性 / 娱乐性）同样由模型判定，用于话题聚合页的属性区分

接口：Ollama Cloud（OpenAI 兼容）
      POST https://ollama.com/v1/chat/completions
      免费档实测可用：gpt-oss:20b（4.5s/条、JSON 稳定、中文分类准确）
      需订阅的模型（glm-5.x / deepseek-v4 / kimi / minimax / mistral-large 等）返回 402

设计要点（配合每小时一次的高频调度）：
1. 增量缓存：上一轮已分析过的词条直接复用结果，只有新上榜的词条才调模型
   —— 热榜每小时变化很小，调用量从 50 次/轮降到个位数
2. 条数完整：**输出条数恒等于抓到的热榜条数**，不会因限流被截断
3. 限流熔断：连续多次限流则本轮停止调用，剩余词条沿用缓存（无缓存则留空待下轮补齐）
4. 无变化不写盘：内容与上一轮完全一致时不写文件，避免每小时产生空提交
"""
import os, re, sys, json, time, urllib.request, urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

API_KEY = (os.environ.get("LLM_API_KEY")
           or os.environ.get("OLLAMA_API_KEY")
           or os.environ.get("GLM_API_KEY")
           or "")
API_URL = os.environ.get("LLM_API_URL", "https://ollama.com/v1/chat/completions")
MODEL = os.environ.get("LLM_MODEL", "gpt-oss:20b")

# 提示词版本：改动 build_prompt 或关键词规则时 +1，缓存中版本不同的词条会被重新分析
PROMPT_VERSION = "4"

DOMAINS = ["体育", "娱乐", "社会", "科技", "财经", "民生", "情感", "美食", "时尚",
           "健康", "教育", "汽车", "游戏", "影视", "旅游", "宠物", "国际", "其他"]
FORMS = ["短视频", "图文", "深度报道", "直播", "数据可视化", "互动话题"]
LIFECYCLE = ["短期", "中期", "长期"]
NATURES = ["新闻性", "娱乐性"]

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_PATH = os.path.join(BASE, "data", "raw_hotspots.json")
SENTI_PATH = os.path.join(BASE, "data", "sentiment.json")
OUT_PATH = os.path.join(BASE, "data", "hotspots.json")

MAX_RATE_STREAK = 3      # 连续限流达到此数即熔断，本轮不再调用
MAX_CALLS_PER_RUN = 60   # 单轮调用上限，防止失控
REQUEST_TIMEOUT = 90


def build_prompt(keyword: str, siblings=None) -> str:
    """构造分析提示词

    siblings：同榜的「兄弟词条」——与当前词条共享长公共子串的其他上榜词条。
    热榜里同一实体常被拆成多条（早春晴朗云合 / 早春晴朗战绩 / 早春晴朗有收官见面会…），
    单看裸词条时模型无从知道「早春晴朗」是网剧名，只能按常用词切成 早春/晴朗。
    把兄弟词条作为实体线索喂进去，模型即可推断出实体边界。
    """
    ctx = ""
    if siblings:
        ctx = ("【背景参考】同榜相关词条（只帮你判断实体边界，"
               "其中的字词一律不得出现在答案里）：\n"
               + "\n".join("  - " + s for s in siblings) + "\n\n")
    return (
        "你是社交媒体热点分析专家。请对热搜词条做「热点基因」分析。\n"
        f"热搜词条：{keyword}\n"
        + ctx +
        "分析维度（只输出一个 JSON 对象，不要 markdown 代码块、不要任何多余文字）：\n"
        f"1. 创作领域：从 {DOMAINS} 中选一个，选最贴合的。"
        "影视剧/电影/综艺及其播放量、热度榜等衍生数据 → 选「影视」或「娱乐」\n"
        f"2. 内容形态：从 {FORMS} 中选一个\n"
        f"3. 生命周期：从 {LIFECYCLE} 中选一个\n"
        "4. 核心话题词：3-5 个，用于检索该话题的关键词，规则：\n"
        "   - 每个词都必须真实出现在「热搜词条」本身里；严禁使用「背景参考」里的任何字词\n"
        "   - 【先识别实体】作品名（剧名/电影/综艺/歌曲）、人名、品牌名、机构名必须整块保留，"
        "严禁拆成常用词\n"
        "   - 可参考「背景参考」判断实体：若一段字符在多个词条里反复出现，它多半就是实体名"
        "（如多条都含「早春晴朗」，说明「早春晴朗」是一个整体）\n"
        "   - 反例：「早春晴朗云合」中「早春晴朗」是网剧名，不可拆成 [\"早春\",\"晴朗\"]，"
        "正确是 [\"早春晴朗\",\"云合\"]；「刘亦菲曾被裁掉过」不可切成 [\"刘亦菲曾\",\"裁掉过\"]，"
        "正确是 [\"刘亦菲\",\"被裁\"]\n"
        "   - 每个词 2-8 个字（英文品牌/产品名保留原文，如 iPhone18Pro）\n"
        "   - 不得含标点、#号、空格；不得是单个虚字（如「的」「了」「曝」）\n"
        "   - 禁止整句照抄词条，也不要重复输出同一个词\n"
        f"5. 话题性质：从 {NATURES} 中选一个。"
        "新闻性＝时政/经济/社会/科技/民生等公共议题；娱乐性＝明星/影视/综艺/网红等消遣话题。"
        "【关键】看「话题对象」而不是字面用词：只要话题对象是影视剧/明星/综艺/网红，"
        "即使词条带「云合/收视/播放量/市占率/热度值/榜单」等数据字眼，也判「娱乐性」\n\n"
        '输出格式（严格）：{"创作领域":"","内容形态":"","生命周期":"","核心话题词":["",""],"话题性质":""}'
    )


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（用于找同榜兄弟词条）。词条很短、每轮仅几十条，DP 足够快"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def find_siblings(keyword: str, all_titles, k: int = 3):
    """找出与当前词条共享长公共子串（或共同前缀）的其他上榜词条

    这些词条是对模型最有效的实体线索：单条看不出是剧名，多条一起就能看出。
    """
    scored = []
    for t in all_titles:
        if not t or t == keyword:
            continue
        n = _lcs_len(keyword, t)
        p = _prefix_len(keyword, t)
        if n >= 3 or p >= 2:
            scored.append((n, p, t))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [t for _, _, t in scored[:k]]


def _common_entities(titles, min_len: int = 3, max_len: int = 8):
    """从同榜词条里挖出「至少在 2 条标题中出现的公共子串」→ {候选实体: 出现条数}

    早春晴朗云合 / 早春晴朗战绩 / 早春晴朗有收官见面会 三条共享「早春晴朗」，
    这类重复出现的串基本就是作品/人物/事件名，可作为模型漏识别时的确定性兜底。

    注意：**保留全部候选**（不做「只留最长」的合并）。因为它们粒度不同且各有用途：
    早春晴朗（剧名，support 3）与 早春晴朗云合（剧名+数据平台，support 2）会同时存在，
    由使用方按 support / 长度择优，避免长候选把更精确的短实体吃掉。
    """
    hit = defaultdict(set)
    for i, t in enumerate(titles):
        n = len(t)
        for L in range(min_len, min(max_len, n) + 1):
            for s in range(n - L + 1):
                hit[t[s:s + L]].add(i)
    sup = {s: len(idxs) for s, idxs in hit.items()
           if len(idxs) >= 2 and CJK.match(s)}     # 只认纯中文实体，避免英文子串噪声
    # 去嵌套：o 包含 s 时必然 support[o] <= support[s]。
    # 若两者 support 相等，说明凡出现 s 的标题都出现了 o，o 是同覆盖范围的更精确形式，
    # 此时丢掉 s（如「青岛货轮」「货轮火灾」都并入「青岛货轮火灾」）。
    # 若 support[s] 严格更大，则 s 是覆盖面更广的独立实体，必须保留
    # （如「早春晴朗」support 3 > 「早春晴朗云合」support 2 → 两者都留，各有用处）。
    return {s: c for s, c in sup.items()
            if not any(s != o and s in o and sup.get(o, -1) == c for o in sup)}


def _restore_entities(kws, title: str, entities):
    """模型把实体名切成碎片时，用完整实体名还原

    仅在「碎片按序拼接后恰好等于实体名」时才替换，避免误伤正常分词。
    """
    t = _norm(title)
    out = list(kws)
    for e in sorted(entities, key=len, reverse=True):
        if e in out or _norm(e) not in t:
            continue
        parts = [k for k in out if k and k in e]
        parts.sort(key=lambda x: e.index(x))
        used, pos = [], 0
        for k in parts:
            i = e.index(k)
            if i < pos:          # 与前一个碎片重叠，跳过
                continue
            used.append(k)
            pos = i + len(k)
        if len(used) >= 2 and "".join(used) == e:
            # 碎片本身已是公认实体时不合并：说明它是独立成立的词
            # （「早春晴朗」已是实体，就不该被并回「早春晴朗云合」，否则粒度反而变粗）
            if any(k in entities for k in used):
                continue
            out = [k for k in out if k not in used]
            out.append(e)
            out.sort(key=lambda x: t.find(_norm(x)) if _norm(x) in t else 999)
            break
    return out


KW_JUNK = re.compile(r"[#，。！？、：；,!?:;\"'“”‘’（）()\[\]【】<>《》/\\|~`^=+*&%$@]+")
KW_EDGE = "…—－-_·.,:;!?、，。！？# \t"
KW_STOP = {"的", "了", "在", "和", "与", "被", "把", "让", "致", "为", "对", "从", "到",
           "是", "有", "都", "就", "还", "也", "又", "将", "已", "曝", "传", "称", "等",
           # 纯谓语动词：做检索关键词没有信息量（精确匹配才生效，不影响「官方回应」这类实体短语）
           "造成", "导致", "引发", "致使", "成为", "表示", "进行", "予以"}
CJK = re.compile(r"^[\u4e00-\u9fff]+$")


def _norm(s: str) -> str:
    """归一化：去掉空白与 #、转小写，用于「关键词必须出自标题」的比对"""
    return re.sub(r"[\s#]+", "", s or "").lower()


def clean_keywords(raw, title: str = "", entities=None):
    """清洗模型给出的核心话题词

    规则：去标点/#/空白 → 去虚词 → 限长 → 去重 → **必须是标题的子串**（挡幻觉）→ 上限 5 个
    注意：本函数只能挡结构性垃圾与幻觉，挡不住「字符上合法但语义是碎片」的词
    （如「早春」「晴朗」）——那类靠 build_prompt 的实体约束 + entities 还原兜底。
    """
    if isinstance(raw, str):
        raw = re.split(r"[，,、;；\s]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    title_key = _norm(title)
    # 标题本身就是一个被同榜多次印证的实体名时（如「早春晴朗」「青岛货轮火灾」），
    # 允许「关键词 == 整条标题」——它不是整句照抄，而是一个完整的实体名。
    is_entity_title = bool(entities) and title_key in {_norm(e) for e in entities}
    out = []
    for k in raw:
        if not isinstance(k, str):
            continue
        k = KW_JUNK.sub("", k).strip().strip(KW_EDGE)
        # 中文词去掉所有空白；含拉丁字母的保留单词间单空格（如 Apple Duo）
        k = re.sub(r"\s+", "" if CJK.match(k) else " ", k)
        if not k or k in KW_STOP:
            continue
        # 整词都由虚词/功能字构成（如「已致」）→ 它不是话题词
        if all(ch in KW_STOP for ch in k):
            continue
        if CJK.match(k):
            if not (2 <= len(k) <= 8):
                continue
        elif not (2 <= len(k) <= 24):
            continue
        # 必须真实出现在词条里：既排除整句照抄，也排除模型凭空补的词
        if title_key and _norm(k) not in title_key:
            continue
        if k == title and not is_entity_title:
            continue
        if k in out:
            continue
        out.append(k)
        if len(out) >= 8:
            break

    # 去冗余碎片：若某词比另一个保留词只多出 ≤2 个字且包含它，
    # 说明它是「词 + 少量虚字」拼出来的碎片（如「四大底层陷阱」含「底层陷阱」）→ 丢掉长的
    pruned = [k for k in out
              if not any(k != o and o in k and 0 < len(k) - len(o) <= 2 for o in out)]
    # 实体还原：模型把网剧名/人名等切成了碎片时，用同榜挖出的实体名拼回来
    if entities:
        pruned = _restore_entities(pruned, title, entities)
        # 实体兜底：若清洗后的关键词完全没有覆盖标题里的实体名，说明模型把主角整块漏了
        # （如长标题里的网剧名）→ 补上标题中最具代表性的那个实体（最长优先）。
        # 仅当选出的关键词「一个实体都没沾上」时才动手，避免像旧版那样反过来把精确短实体吃掉。
        in_title = [(e, c) for e, c in entities.items()
                    if _norm(e) in title_key and _norm(e) != title_key]
        if in_title and not any(e in "".join(pruned) for e, _ in in_title):
            best = max(in_title, key=lambda x: (len(x[0]), x[1]))[0]
            pruned = [best] + pruned
    # 去冗余碎片（二）：同一个词下挂着 ≥2 个更短的子串，说明这些短串是同一个词被切碎的产物
    # （「重大人员伤亡」下挂「人员」「伤亡」；「早春晴朗」下挂「早春」「晴朗」）→ 丢掉短串。
    # 用「≥2 个」作门槛：单个短串可能独立成立（如「华为发布会」下的「华为」），不该误伤。
    pruned = [k for k in pruned
              if not (len(k) <= 3 and any(
                  k != o and k in o
                  and sum(1 for x in pruned if x != o and x in o) >= 2
                  for o in pruned))]
    seen = []
    for k in pruned:
        if k not in seen:
            seen.append(k)
    return seen[:5]


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


def call_llm(keyword: str, siblings=None, entities=None):
    """返回分析 dict / "RATE_LIMITED" / None"""
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": build_prompt(keyword, siblings)}],
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
                "核心话题词": clean_keywords(j.get("核心话题词", []), keyword, entities),
                "话题性质": j.get("话题性质", "") or "",
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
    # 同榜实体线索：兄弟词条（喂给模型）+ 重复公共子串（确定性还原兜底）
    all_titles = [it.get("title", "") for it in items_raw if it.get("title")]
    entities = _common_entities(all_titles)
    print(f"共 {len(items_raw)} 条热榜；上一轮缓存 {len(prev_map)} 条；模型 {MODEL}")
    if entities:
        print(f"同榜识别到 {len(entities)} 个候选实体：{sorted(entities, key=len, reverse=True)[:8]}")

    items = []
    reused = called = failed = 0
    rate_streak = 0
    blocked = False

    for i, it in enumerate(items_raw, 1):
        t = it.get("title", "")
        if not t:
            continue

        cached = prev_map.get(t)
        # 可复用条件：模型产出且提示词版本一致。
        # 旧规则产物（分析来源=本地）与旧版本提示词的结果一律重新分析。
        src = (cached or {}).get("分析来源", "LLM")
        cached_ok = bool(
            cached
            and src in ("LLM", "GLM")
            and cached.get("创作领域")
            and cached.get("核心话题词")
            and cached.get("话题性质")
            and str(cached.get("分析版本", "")) == PROMPT_VERSION
        )
        analysis = None

        if cached_ok:
            # 缓存的关键词也过一遍当前规则：清洗规则升级时无需重调模型即可自愈
            kws = clean_keywords(cached.get("核心话题词", []), t, entities)
            if kws:
                analysis = {
                    "创作领域": cached.get("创作领域", ""),
                    "内容形态": cached.get("内容形态", ""),
                    "生命周期": cached.get("生命周期", ""),
                    "核心话题词": kws,
                    "话题性质": cached.get("话题性质", ""),
                }
                reused += 1

        if analysis is None and not blocked and called < MAX_CALLS_PER_RUN:
            r = call_llm(t, find_siblings(t, all_titles), entities)
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
                "话题性质": analysis["话题性质"],
                "分析来源": "LLM",
                "分析版本": PROMPT_VERSION,
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
                "话题性质": "",
                "分析来源": "",
                "分析版本": "",
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
        "性质分布": dict(Counter(it["话题性质"] for it in items if it["话题性质"])),
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
    print("性质分布：", summary["性质分布"])


if __name__ == "__main__":
    main()
