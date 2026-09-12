# -*- coding: utf-8 -*-
"""
热点基因分析：调用云端大模型，对每条热榜词条输出
创作领域 / 内容形态 / 生命周期 / 核心话题词 / 话题性质

- 情感倾向由本地小模型 sentiment.py 提供，本脚本只负责领域维度并合并两者
- 所有分析结果均来自实时模型推理，无本地兜底、无规则伪造
- 话题性质（新闻性 / 娱乐性）同样由模型判定，用于话题聚合页的属性区分

接口：Agnes 2.5 Flash（OpenAI 兼容，免费档）
      POST https://apihub.agnes-ai.com/v1/chat/completions
      实测（2026-09-12，48 条抖音热榜）：JSON 全部合法解析，
      单条 7.0~32.7s、均值约 10.6s，串行全量约 8.5 分钟
      端点/模型/key 全部可经环境变量覆盖：LLM_API_URL / LLM_MODEL / LLM_API_KEY
      （默认值即 Agnes，不设环境变量也能跑）

回退模型：Ollama Cloud（https://ollama.com/v1/chat/completions，gpt-oss:20b）
      主模型连续失败后自动接管，主模型恢复即切回；同样是实时模型推理，
      只是换了供应商（环境变量 FALLBACK_API_URL / FALLBACK_MODEL / FALLBACK_API_KEY）
      未配 key 时回退自动禁用；把 FALLBACK_API_URL 置空可彻底关闭

历史方案（已弃用，勿据此排查）：GLM bigmodel（额度耗尽）

设计要点（配合每小时一次的高频调度）：
1. 全量实时：**每轮对全部词条重新调用模型**，不复用上一轮的任何结论
   —— 词条内容与热度随时在变，结论必须是当下的（约 50 次调用/轮）
2. 条数完整：**输出条数恒等于抓到的热榜条数**，不会因限流被截断
3. 限流熔断：连续多次限流则本轮停止调用，剩余词条留空、下轮补齐（不沿用旧结果）
4. 无变化不写盘：内容与上一轮完全一致时不写文件，避免每小时产生空提交
5. 失败保护：整轮成功率过低时不覆盖旧数据，避免页面大面积空档
"""
import os, re, sys, json, time, argparse, urllib.request, urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

# key 优先级：显式 LLM_API_KEY > Agnes（当前方案）> Ollama / GLM（历史遗留，仅兼容）
# 记录命中的变量名：secret 没配时（CI 里 ${{ secrets.X }} 会展开成空串）
# 必须能一眼看出「key 从哪来 / 根本没来」，否则会静默落到回退模型
_KEY_ORDER = ("LLM_API_KEY", "AGNES_API_KEY", "OLLAMA_API_KEY", "GLM_API_KEY")
API_KEY = ""
API_KEY_SRC = ""
for _n in _KEY_ORDER:
    if os.environ.get(_n):
        API_KEY, API_KEY_SRC = os.environ[_n], _n
        break
# 默认走 Agnes 2.5 Flash（OpenAI 兼容、免费）；CI 由 workflow 显式注入同名环境变量
API_URL = os.environ.get("LLM_API_URL", "https://apihub.agnes-ai.com/v1/chat/completions")
MODEL = os.environ.get("LLM_MODEL", "agnes-2.5-flash")

# ---- 回退模型：Ollama Cloud（主模型限流/不可用时接管，恢复后自动切回主模型）----
# 回退同样是**实时模型推理**，不是本地规则兜底 —— 只是换了个供应商，
# 全量实时、每条重新分析的约束不变（见上方设计要点）。
# 启用条件：FALLBACK_API_URL 非空 且（配了 key 或 FALLBACK_ALLOW_NO_KEY=1）。
# 想彻底关掉回退：把 FALLBACK_API_URL 置为空字符串即可。
FALLBACK_API_URL = os.environ.get("FALLBACK_API_URL",
                                  "https://ollama.com/v1/chat/completions")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "gpt-oss:20b")
FALLBACK_API_KEY = (os.environ.get("FALLBACK_API_KEY")
                    or os.environ.get("OLLAMA_API_KEY")
                    or "")
FALLBACK_ON = bool(FALLBACK_API_URL) and bool(
    FALLBACK_API_KEY or os.environ.get("FALLBACK_ALLOW_NO_KEY"))

# 主模型连续失败达此次数后，后续词条直接走回退（不再逐条空等主模型重试）
PRIMARY_GIVE_UP_AFTER = 2
# 即便已判定主模型不可用，每 N 条仍探一次主模型，用于发现其恢复
PRIMARY_PROBE_EVERY = 15

_PRIMARY_FAILS = 0   # 主模型连续失败计数（成功即清零）
_PRIMARY_SKIP = 0    # 回退模式下已跳过的条数（用于探测间隔取模）
FALLBACK_USED = 0    # 本轮由回退模型接管的条数

# 提示词版本：改动 build_prompt 或关键词规则时 +1，随结果写入，便于回溯是哪版规则产出
PROMPT_VERSION = "6"

DOMAINS = ["体育", "娱乐", "社会", "科技", "财经", "民生", "情感", "美食", "时尚",
           "健康", "教育", "汽车", "游戏", "影视", "旅游", "宠物", "国际", "其他"]
FORMS = ["短视频", "图文", "深度报道", "直播", "数据可视化", "互动话题"]
LIFECYCLE = ["短期", "中期", "长期"]
NATURES = ["新闻性", "娱乐性"]

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_PATH = os.path.join(BASE, "data", "raw_hotspots.json")
SENTI_PATH = os.path.join(BASE, "data", "sentiment.json")
OUT_PATH = os.path.join(BASE, "data", "hotspots.json")
# 上一轮 authors.py 的产物：含话题页正文挖出的实体词库
AUTHORS_PATH = os.path.join(BASE, "data", "authors.json")
# 词条在榜轨迹（逐轮累积）：首见时间 / 在榜轮数 / 连续轮数 / 首末热度与榜位
HISTORY_PATH = os.path.join(BASE, "data", "history.json")

MAX_RATE_STREAK = 3       # 连续限流达到此数即熔断，本轮不再调用
MAX_CALLS_PER_RUN = 120   # 单轮调用上限：全量实时分析（当前约 50 条），留足余量
REQUEST_TIMEOUT = 90

# 生命周期的时间尺度：由 40 轮真实快照统计得出（可测跨度 228 条，中位 1.9h、
# P75 3.4h、P90 5.5h，超过 24h 的仅 0.9%）。以 6h / 24h 为界，既能切开长尾，
# 又不会像 24h/48h 那样把 99% 的词条挤进同一档。
HISTORY_KEEP_DAYS = 7     # 历史里超过这些天没再出现的词条会被清理
LIFE_MID_HOURS = 6        # 在榜 ≥ 6h → 中期
LIFE_LONG_HOURS = 24      # 在榜 ≥ 24h → 长期（少数跨天长尾）


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
        f"3. 生命周期：从 {LIFECYCLE} 中选一个。判据是「这个话题还能热多久」，"
        "不是「现在有多热」：\n"
        "   - 短期＝事件驱动、一次性爆发，通常当天即退（突发事故、明星八卦、单条爆料、"
        "单场比赛、单款产品开售）\n"
        "   - 中期＝有持续讨论或多轮后续节点，能维持数天（调查进展、连载作品、"
        "持续性争议、政策落地过程）\n"
        "   - 长期＝制度性/周期性话题，数月以上或每年反复出现（节日、高考、两会、"
        "年度榜单、长期存在的公共议题）\n"
        "   - 【注意】热度高 ≠ 寿命长：再爆的突发事件也是短期；只有反复复现或"
        "有长期制度背景的才算长期\n"
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
        "   - 不要把「介词/数词 + 虚词」当关键词：如「日本梅毒暴发与三个一有关」应输出"
        "[\"日本\",\"梅毒\",\"暴发\",\"三个一\"]，不可输出 [\"三个\",\"有关\"]（「三个一」是"
        "固定说法，须整块保留）；同理「霍去病其实被历史低估了」不可输出「其实」\n"
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


def _protect_spans(kws, title: str, entities):
    """词库实体「区间保护」：修掉实体被切错位的碎片

    词库（同榜挖掘 + 话题页正文挖掘）里的实体在标题中占据一段字符区间。
    模型给出的关键词只要与该区间**部分重叠**、或**落在实体内部**，就是错位碎片 → 丢弃。
    例：标题「早春晴朗云合超藏海传」，词库含「藏海传」，
        模型切出的「超藏」（跨界重叠）与「海传」（落在实体内）都会被丢掉。
    随后保证标题内每个未被覆盖的实体都作为关键词出现（已被更长关键词包含则跳过）。
    """
    if not entities or not kws:
        return kws
    tk = _norm(title)
    names = {_norm(e) for e in entities}
    spans = []
    for e in entities:
        ne = _norm(e)
        if not ne or ne == tk:
            continue
        st = 0
        while True:
            i = tk.find(ne, st)
            if i < 0:
                break
            spans.append((i, i + len(ne), e))
            st = i + 1
    if not spans:
        return kws

    out = []
    for k in kws:
        nk = _norm(k)
        if nk in names:          # 它本身就是词库实体 → 保留
            out.append(k)
            continue
        i = tk.find(nk)
        if i < 0:                # 不在标题里（理论上不该发生）→ 交给既有规则
            out.append(k)
            continue
        j = i + len(nk)
        if any(i < b and a < j for a, b, _ in spans):   # 与实体区间重叠 → 丢
            continue
        out.append(k)

    for _, _, e in spans:        # 标题内的实体必须出现
        ne = _norm(e)
        if any(_norm(x) == ne for x in out):
            continue
        if any(ne in _norm(x) for x in out):            # 已被更长关键词覆盖 → 不重复
            continue
        out.append(e)
    return out


KW_JUNK = re.compile(r"[#，。！？、：；,!?:;\"'“”‘’（）()\[\]【】<>《》/\\|~`^=+*&%$@]+")
KW_EDGE = "…—－-_·.,:;!?、，。！？# \t"
KW_STOP = {"的", "了", "在", "和", "与", "被", "把", "让", "致", "为", "对", "从", "到",
           "是", "有", "都", "就", "还", "也", "又", "将", "已", "曝", "传", "称", "等",
           # 纯谓语动词：做检索关键词没有信息量（精确匹配才生效，不影响「官方回应」这类实体短语）
           "造成", "导致", "引发", "致使", "成为", "表示", "进行", "予以",
           "建议", "建议大家", "大家", "告诉", "认为", "发现", "回应", "回应称",
           "采取", "必要措施", "可能", "该", "可",
           # 介词性/语气性虚词：单独成词时无信息量（如「与三个一有关」「其实还活着」）
           "有关", "关于", "其实", "至于", "因此", "所以", "但是", "而是", "不仅"}
# 纯「数词 + 量词」字集：只用于拦「三个」「一种」这类 2 字碎片（见 clean_keywords），
# 不含阿拉伯数字与实义名词字，故「25人」「3岁男童」「35岁员工」等不受影响
CJK_NUMQ = set("一二三四五六七八九十百千万两几零半个只种件次名位家条张份点些")
CJK = re.compile(r"^[\u4e00-\u9fff]+$")


def _norm(s: str) -> str:
    """归一化：去掉空白与 #、转小写，用于「关键词必须出自标题」的比对"""
    return re.sub(r"[\s#]+", "", s or "").lower()


def clean_keywords(raw, title: str = "", entities=None):
    """清洗模型给出的核心话题词

    规则：去标点/#/空白 → 去虚词 → 限长 → 去重 → **必须是标题的子串**（挡幻觉）→ 上限 5 个
    注意：本函数只能挡结构性垃圾与幻觉，挡不住「字符上合法但语义是碎片」的词
    （如「早春」「晴朗」）——那类靠 build_prompt 的实体约束 + entities 还原/区间保护兜底。
    entities：{实体名: support}，来源为同榜重复子串 + 上一轮话题页正文词库
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
        # 纯「数词 + 量词」2 字碎片（如「三个」「一种」「两个」）→ 无检索信息量，丢弃。
        # 只拦 2 字词：既不误伤「三个一」这类固定说法，也不误伤含实义字的「个税」「一箭六星」
        if len(k) == 2 and all(ch in CJK_NUMQ for ch in k):
            continue
        if CJK.match(k):
            if not (2 <= len(k) <= 8):
                continue
        elif not (2 <= len(k) <= 24):
            continue
        # 必须真实出现在词条里：既排除整句照抄，也排除模型凭空补的词
        if title_key and _norm(k) not in title_key:
            continue
        if k == title and not is_entity_title and len(title) > 3:
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
    # 词库实体区间保护：修掉「实体被切错位」的碎片（超藏/海传 → 藏海传）
    pruned = _protect_spans(pruned, title, entities)
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


def _chat_once(url, key, model, prompt):
    """单次 chat/completions 调用，返回剥壳后的 JSON dict（失败抛异常）"""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=payload, headers=headers)
    resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    r = json.loads(resp.read().decode("utf-8"))
    return extract_json(r["choices"][0]["message"]["content"])


def _try_model(url, key, model, prompt, attempts):
    """按 attempts 次重试调用，返回 (json_dict|None, 错误描述|None, 末次 HTTP 码|None)

    限流类（429/402/503）用更长退避 —— 这几类等一等往往就能过。
    """
    last, code = None, None
    for attempt in range(attempts):
        try:
            return _chat_once(url, key, model, prompt), None, None
        except urllib.error.HTTPError as e:
            code = e.code
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:120]
            except Exception:
                pass
            last = f"HTTP {e.code} {body}"
            if attempt < attempts - 1:
                time.sleep(4 * (attempt + 1) if e.code in (429, 402, 503)
                           else 2 * (attempt + 1))
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
    return None, last, code


def call_llm(keyword: str, siblings=None, entities=None):
    """返回分析 dict / "RATE_LIMITED" / None

    主模型（Agnes）优先；连续失败后由回退模型（Ollama）接管，
    并每 PRIMARY_PROBE_EVERY 条探一次主模型，一旦恢复立刻切回。
    """
    global _PRIMARY_FAILS, _PRIMARY_SKIP, FALLBACK_USED
    prompt = build_prompt(keyword, siblings)

    def settle(j, model_name):
        return {
            "创作领域": j.get("创作领域", "") or "",
            "内容形态": j.get("内容形态", "") or "",
            "生命周期": j.get("生命周期", "") or "",
            "核心话题词": clean_keywords(j.get("核心话题词", []), keyword, entities),
            "话题性质": j.get("话题性质", "") or "",
            "_model": model_name,
        }

    last = None
    primary_limited = False
    # 主模型已判定不可用时，只在探测间隔上重试一次，避免每条都空等退避
    probe_primary = True
    if FALLBACK_ON and _PRIMARY_FAILS >= PRIMARY_GIVE_UP_AFTER:
        _PRIMARY_SKIP += 1
        if _PRIMARY_SKIP % PRIMARY_PROBE_EVERY != 0:
            probe_primary = False
            last = f"主模型连续失败 {_PRIMARY_FAILS} 次，本轮跳过"

    if probe_primary:
        j, err, code = _try_model(API_URL, API_KEY, MODEL, prompt, 3)
        if j is not None:
            _PRIMARY_FAILS = 0
            _PRIMARY_SKIP = 0
            return settle(j, MODEL)
        primary_limited = code in (429, 402, 503)
        _PRIMARY_FAILS += 1
        last = err

    # 回退模型：Ollama Cloud
    if FALLBACK_ON:
        j, err, _ = _try_model(FALLBACK_API_URL, FALLBACK_API_KEY,
                               FALLBACK_MODEL, prompt, 2)
        if j is not None:
            FALLBACK_USED += 1
            print(f"  [FALLBACK] {keyword}: 主模型不可用（{last}）→ {FALLBACK_MODEL} 接管")
            return settle(j, FALLBACK_MODEL)
        print(f"  [FAIL] {keyword}: 主模型与回退均失败（主 {last} / 回退 {err}）")
    else:
        print(f"  [FAIL] {keyword}: {last}")

    return "RATE_LIMITED" if primary_limited else None


def load_prev(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        return {}
    return {it.get("title"): it for it in prev.get("items", []) if it.get("title")}


def load_topic_lexicon(path=None):
    """读取上一轮 authors.py 从话题页正文挖出的实体词库 → {标题: [实体名]}

    注意：analyze 在 authors 之前跑，所以这里读到的是**上一轮**的词库（最多 1 小时旧）。
    剧名是稳定实体，隔一轮完全够用；且缓存词条每轮都会重跑 clean_keywords，
    因此新词库生效后，下一轮会自动把关键词修正过来（自愈），无需升 PROMPT_VERSION。
    """
    path = path or AUTHORS_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    out = {}
    for t, tv in (d.get("topics") or {}).items():
        if not isinstance(tv, dict):
            continue
        lex = [x for x in (tv.get("lexicon") or []) if isinstance(x, str) and x]
        if lex:
            out[t] = lex
    return out


def load_topic_kw_freq(path=None):
    """读取上一轮 authors.py 从微博文案挖出的 n-gram 词频 → {标题: [(词, 次数), ...]}

    与 lexicon 一样读到的是上一轮的产物；每轮重跑，1 小时内能自愈。
    """
    path = path or AUTHORS_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    out = {}
    for t, tv in (d.get("topics") or {}).items():
        if not isinstance(tv, dict):
            continue
        kf = tv.get("kw_freq") or []
        if isinstance(kf, list) and kf:
            out[t] = [tuple(x) if isinstance(x, (list, tuple)) else (x, 1) for x in kf]
    return out


def enrich_kw_freq(kws, title, kw_freq_list, max_kws: int = 5):
    """用微博文案 n-gram 词频兜底：当 LLM 输出不足（< 2 个）时，
    按频次从 kw_freq 里选 top-N 补齐，目标是补到至少 3 个可检索的关键词。

    安全前提：kw_freq 来自 authors.py 对该词条**自己的话题页/参与作者**微博
    挖出的 n-gram（每条作者博文一个 .weibo-text → 2~4 gram CJK 片段，按频次聚
    合），是话题内封闭语料，不存在跨话题污染。所以补的词不需要必须出现在标
    题里——「黄金」这种 2 字短标题尤其需要从文案里挖出「加息/金价/大跌」这种
    真正在讨论的内容。

    仍走的硬约束：① 非停用词 ② 2~6 字 ③ 纯 CJK ④ 跟已有词不重复。
    """
    kws = list(kws or [])
    if not kw_freq_list or not title:
        return kws
    if len(kws) >= 2:
        return kws          # LLM 已给出至少 2 个，不污染
    tk = _norm(title)
    used = {_norm(k) for k in kws}
    for name, c in kw_freq_list:
        n = _norm(name)
        if not n or n in used:
            continue
        if name in KW_STOP or all(ch in KW_STOP for ch in name):
            continue
        if not (2 <= len(name) <= 6 and CJK.match(name)):
            continue
        # 短标题（≤3 字）几乎不可能包含讨论关键词，所以短标题跳过「必须在标题里」；
        # 长标题（≥4 字）仍要求补词出现在标题里，避免误从同名作者的其他无关讨论里捞词
        if len(title) >= 4 and n not in tk:
            continue
        kws = kws + [name]
        used.add(n)
        if len(kws) >= max(3, max_kws):
            break
    return kws[:max_kws]


def signature(result) -> str:
    return json.dumps({
        "source": result.get("source"),
        "summary": result.get("summary"),
        "clusters": result.get("clusters"),
        "items": result.get("items"),
    }, ensure_ascii=False, sort_keys=True)


# ========== 词条在榜轨迹（逐轮累积，供生命周期客观判定） ==========

BJ_TZ = timezone(timedelta(hours=8))   # 统一用北京时间，避免 naive/aware 混比


def _parse_ts(s: str):
    """解析 history.json 里的北京时间戳（带时区，便于与 now 直接相减）"""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=BJ_TZ)
    except Exception:
        return None


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"last_run": "", "topics": {}}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            h = json.load(f)
    except Exception:
        return {"last_run": "", "topics": {}}
    if not isinstance(h, dict) or not isinstance(h.get("topics"), dict):
        return {"last_run": "", "topics": {}}
    return h


def update_history(hist, metrics, now_str, now_dt):
    """把本轮每条的 hot / realpos 并入轨迹

    关键区分（决定生命周期判定是否成立）：
      · streak（连续在榜轮数）：上次被看到的时刻 == 上一轮运行时刻 → 续上，否则从 1 重来；
      · s_first / s_hot / s_pos：**当前这一段连续在榜**的起点时刻与起点热度、榜位。

    只用「首次出现到现在」会把间歇复现的老词条（5 天里断断续续上了 3 次）算成
    「在榜 120 小时」，那是错的；真正有意义的是「这一轮连续挂了多久」。
    """
    prev_run = hist.get("last_run", "")
    topics = hist.setdefault("topics", {})
    for title, m in metrics.items():
        rec = topics.get(title)
        if rec is None:
            rec = {"first": now_str, "rounds": 0, "first_hot": m["hot"], "first_pos": m["realpos"]}
            topics[title] = rec
        rec["rounds"] = int(rec.get("rounds", 0)) + 1
        if rec.get("seen", "") == prev_run:
            rec["streak"] = int(rec.get("streak", 0)) + 1
        else:                       # 中间至少空过一轮 → 新的一段连续在榜
            rec["streak"] = 1
            rec["s_first"] = now_str
            rec["s_hot"] = m["hot"]
            rec["s_pos"] = m["realpos"]
        rec["seen"] = now_str
        rec["last_hot"] = m["hot"]
        rec["last_pos"] = m["realpos"]
    hist["last_run"] = now_str

    # 清理久未出现的词条，避免历史文件无限膨胀
    cut = now_dt - timedelta(days=HISTORY_KEEP_DAYS)
    for title in list(topics):
        d = _parse_ts(topics[title].get("seen", ""))
        if d is None or d < cut:
            del topics[title]
    return hist


def save_history(hist):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))


def lifecycle_metrics(rec, now_dt):
    """由在榜轨迹得出（客观标签, 连续在榜小时, 在榜轮数, 连续轮数, 榜位斜率, 热度增速）

    判定用的是「当前这一段连续在榜的时长」（now - s_first），不是首次出现至今，
    否则间歇复现的老词条会被误算成长寿话题。
    """
    if not rec:
        return ("短期", 0.0, 1, 1, 0.0, 0.0)
    s_first = _parse_ts(rec.get("s_first") or rec.get("first", ""))
    span = max((now_dt - s_first).total_seconds() / 3600.0, 0.0) if s_first else 0.0
    rounds = int(rec.get("rounds", 1))
    streak = int(rec.get("streak", 1))
    if span >= LIFE_LONG_HOURS:
        lab = "长期"
    elif span >= LIFE_MID_HOURS:
        lab = "中期"
    else:
        lab = "短期"
    fh, lh = float(rec.get("s_hot", rec.get("first_hot", 0)) or 0), float(rec.get("last_hot", 0) or 0)
    fp, lp = float(rec.get("s_pos", rec.get("first_pos", 0)) or 0), float(rec.get("last_pos", 0) or 0)
    pos_slope = (lp - fp) / max(streak - 1, 1) if fp and lp else 0.0   # 负值 = 榜位在上升
    hot_gain = (lh / fh - 1.0) if fh > 0 else 0.0
    return (lab, round(span, 1), rounds, streak, round(pos_slope, 2), round(hot_gain, 3))


def fuse_lifecycle(obj_label, llm_label):
    """客观时长优先；客观不足以支撑「长期」时，允许模型以周期性/制度性话题为由判长期

    「中期」是时间事实（需真的在榜 6h+），模型无从得知，故模型不能把短期抬成中期。
    """
    if obj_label in ("中期", "长期"):
        return obj_label
    return "长期" if llm_label == "长期" else "短期"


def main():
    global RAW_PATH, SENTI_PATH, OUT_PATH, HISTORY_PATH, AUTHORS_PATH

    parser = argparse.ArgumentParser(description="热点基因分析（LLM 四维）")
    parser.add_argument("--platform", choices=["weibo", "douyin"], default="weibo")
    args = parser.parse_args()
    if args.platform == "douyin":
        # 抖音独立数据文件，在榜轨迹（history）也独立，避免跨平台词条混淆
        RAW_PATH = os.path.join(BASE, "data", "douyin_raw_hotspots.json")
        SENTI_PATH = os.path.join(BASE, "data", "douyin_sentiment.json")
        OUT_PATH = os.path.join(BASE, "data", "douyin_hotspots.json")
        HISTORY_PATH = os.path.join(BASE, "data", "douyin_history.json")
        AUTHORS_PATH = os.path.join(BASE, "data", "douyin_authors.json")
        PLATFORM = "douyin"
    else:
        PLATFORM = "weibo"

    if not API_KEY:
        print("缺少 LLM_API_KEY / OLLAMA_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    with open(RAW_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    items_raw = raw.get("items", [])
    if not items_raw:
        print("raw_hotspots.json 中没有 items", file=sys.stderr)
        sys.exit(1)

    now_dt = datetime.now(BJ_TZ)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M")
    # 在榜轨迹：把本轮的 hot / realpos 并入历史，据此得到生命周期的时间判据
    metrics = {it["title"]: {"hot": int(it.get("hot") or 0),
                             "realpos": int(it.get("realpos") or 0)}
               for it in items_raw if it.get("title")}
    hist = update_history(load_history(), metrics, now_str, now_dt)
    save_history(hist)
    hist_topics = hist.get("topics", {})
    print(f"在榜轨迹：累计 {len(hist_topics)} 个词条（保留 {HISTORY_KEEP_DAYS} 天）")

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
    # 话题页正文词库（上一轮 authors.py 产物）：按标题并入实体集，供区间保护使用
    topic_lex = load_topic_lexicon()
    print(f"共 {len(items_raw)} 条热榜；全量实时分析（不沿用旧结果）；主模型 {MODEL}")
    print(f"  端点 {API_URL}")
    if API_KEY:
        print(f"  鉴权 key 来源：{API_KEY_SRC}（{API_KEY[:4]}…{API_KEY[-4:]}）")
    else:
        # CI 里 ${{ secrets.X }} 若不存在会展开成空串 → 这里必须吼一声，
        # 否则整轮会静默落到回退模型（表现为「跑成功了但模型不对」）
        print("  ⚠️ 未取到任何 API key（LLM_API_KEY / AGNES_API_KEY / "
              "OLLAMA_API_KEY / GLM_API_KEY 全为空）→ 主模型调用会 401，"
              "本轮将由回退模型接管")
    if FALLBACK_ON:
        print(f"回退模型已启用：{FALLBACK_MODEL} @ {FALLBACK_API_URL}"
              f"（主模型连续失败 {PRIMARY_GIVE_UP_AFTER} 次后接管，"
              f"每 {PRIMARY_PROBE_EVERY} 条探一次主模型是否恢复）")
    else:
        print("回退模型未启用（需 FALLBACK_API_KEY 或 OLLAMA_API_KEY；"
              "FALLBACK_API_URL 置空亦可关闭）")
    if entities:
        print(f"同榜识别到 {len(entities)} 个候选实体：{sorted(entities, key=len, reverse=True)[:8]}")
    if topic_lex:
        print(f"正文词库覆盖 {len(topic_lex)} 个话题，"
              f"合计 {sum(len(v) for v in topic_lex.values())} 个实体")

    def ents_for(title):
        """该词条的实体集 = 同榜实体 + 正文词库里确实出现在本词条中的实体

        正文词库只对「出现在本词条标题里」的实体才有意义（区间保护的输入）；
        其余一律不并入，避免无关话题号污染实体集。
        """
        tk = _norm(title)
        extra = [e for e in (topic_lex.get(title) or []) if _norm(e) in tk]
        if not extra:
            return entities
        merged = dict(entities)
        for name in extra:
            merged.setdefault(name, 1)
        return merged

    # 微博文案 n-gram 词频：LLM 切空/切少时按真实讨论用词兜底
    topic_kw_freq = load_topic_kw_freq()
    if topic_kw_freq:
        print(f"文案词频覆盖 {len(topic_kw_freq)} 个话题，"
              f"合计 {sum(len(v) for v in topic_kw_freq.values())} 个候选词")

    items = []
    called = failed = 0
    rate_streak = 0
    blocked = False

    for i, it in enumerate(items_raw, 1):
        t = it.get("title", "")
        if not t:
            continue

        # 全量实时：每条词条、每一轮都重新调用模型，不复用上一轮结论
        analysis = None
        if not blocked and called < MAX_CALLS_PER_RUN:
            r = call_llm(t, find_siblings(t, all_titles), ents_for(t))
            called += 1
            if r == "RATE_LIMITED":
                rate_streak += 1
                if rate_streak >= MAX_RATE_STREAK:
                    blocked = True
                    print(f"  连续 {rate_streak} 次限流，本轮停止调用模型，"
                          f"剩余词条留空、下轮补齐（不沿用任何旧结论）")
            elif r:
                rate_streak = 0
                analysis = r
            time.sleep(0.2)

        # 生命周期：以「在榜时长」这一客观事实为主，模型只在客观为短期时
        # 以「周期性/制度性话题」为由判长期（否则模型无从知道它挂了多久）
        m = metrics.get(t, {})
        obj_lab, span_h, rounds_n, streak_n, pos_slope, hot_gain = lifecycle_metrics(
            hist_topics.get(t), now_dt)
        life = fuse_lifecycle(obj_lab, (analysis or {}).get("生命周期", ""))
        # 依据文案与判定口径保持一致：span_h 是「连续在榜时长」，所以配「连续轮数」
        if rounds_n <= 1:
            life_basis = "本轮首见"
        elif life == "长期" and obj_lab != "长期":
            life_basis = "模型判定·周期性"
        elif streak_n <= 1:
            life_basis = "重新上榜"
        else:
            life_basis = f"连续在榜 {span_h:g}h / {streak_n} 轮"
        common = {
            "生命周期": life,
            "生命周期依据": life_basis,
            "在榜小时": span_h,
            "在榜轮数": rounds_n,
            "连续轮数": streak_n,
            "榜位斜率": pos_slope,
            "热度增速": hot_gain,
            "热度": m.get("hot", 0),
            "榜位": m.get("realpos", 0),
            # 微博 label 本身就是文案（爆/沸/热/新…），直接透传；
            # 抖音 label 是数字档位，文案由 fetch.py 的 DOUYIN_LABELS 映射后落在 label_text
            # （1=新 3=热 5=首发 8=独家 9=挑战 16=辟谣 17=热议），未知码留空不猜
            # —— 2026-09-12 逐张核对 label_url 徽标图后确认，原「抖音不显示标签」的依据已不成立
            "标签": ((it.get("label_text") or "") if PLATFORM == "douyin"
                     else it.get("label", "")),
        }
        # 抖音聚合页跳转依赖 gid/position/event_time。此前只留在 raw（已 gitignore），
        # 导致 authors.py 只能改读 raw —— 而 raw 是「刚抓的榜」、hotspots 是「已分析的榜」，
        # 热榜分钟级刷新时两者会错位（实测交集仅 42/50，前端表现为「词条有、作者空」）。
        # 透传到产物文件后，authors.py 可直接以「前端消费的同一份榜单」为输入。
        if PLATFORM == "douyin":
            for _k in ("gid", "position", "event_time"):
                if it.get(_k) is not None:
                    common[_k] = it[_k]

        emo = senti_map.get(t) or "中性"
        if analysis:
            # 微博文案 n-gram 兜底：标题短/LLM 切空时按真实讨论用词补
            analysis["核心话题词"] = enrich_kw_freq(
                analysis.get("核心话题词", []), t, topic_kw_freq.get(t, []))
            row = {
                "title": t,
                "情感倾向": emo,
                "创作领域": analysis["创作领域"],
                "内容形态": analysis["内容形态"],
                "核心话题词": analysis["核心话题词"],
                "话题性质": analysis["话题性质"],
                "分析来源": "LLM",
                "分析模型": analysis.pop("_model", ""),
                "分析版本": PROMPT_VERSION,
                "url": it.get("url", ""),
            }
        else:
            failed += 1
            row = {
                "title": t,
                "情感倾向": emo,
                "创作领域": "",
                "内容形态": "",
                "核心话题词": [],
                "话题性质": "",
                "分析来源": "",
                "分析模型": "",
                "分析版本": "",
                "url": it.get("url", ""),
            }
        row.update(common)
        items.append(row)

    solid = sum(1 for x in items if x["创作领域"])
    print(f"共 {len(items)} 条：本轮调用模型 {called} 次 / "
          f"待下轮补齐 {failed} 条；已分析 {solid} 条")
    if FALLBACK_ON:
        print(f"其中由回退模型 {FALLBACK_MODEL} 接管 {FALLBACK_USED} 条")

    # 失败保护：全量实时分析下，若整轮成功率过低（多为限流），
    # 保留上一份完整数据不覆盖，避免页面大面积空档；两种情况都不复用单条结果。
    if prev_map and solid < len(items) * 0.5:
        print(f"本轮仅 {solid}/{len(items)} 条分析成功（疑似限流），保留原有数据不覆盖",
              file=sys.stderr)
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

    result = {
        "updated_at": now_str,
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
