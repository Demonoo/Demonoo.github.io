# -*- coding: utf-8 -*-
"""
热点基因分析（GLM 只做行业/形态/生命周期 + 关键词）
情感倾向由本地小模型 sentiment.py 提供，本脚本合并两者写出 hotspots.json

设计要点（配合 20 分钟一次的高频调度）：
1. 增量缓存：上一轮已分析过的词条直接复用结果，只有新上榜的词条才调 GLM
   —— 热榜每 20 分钟变化很小，这样 GLM 调用量从 50 次/轮降到个位数
2. 条数完整：**输出条数永远等于抓到的热榜条数**，不再因限流被截断
3. 本地兜底：GLM 429/失败时改用本地关键词分类器（零成本零额度），
   词条带上「分析来源=本地」标记，后续 GLM 恢复额度时会被自动重新分析升级
4. 熔断：连续 3 次 429 即停止本轮 GLM 调用（免费档是按天配额，重试无用）
5. 无变化不写盘：内容与上一轮完全一致时不写文件，避免每 20 分钟产生空提交
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

MAX_RATE_STREAK = 3      # 连续 429 达到此数直接熔断，本轮不再调 GLM
MAX_GLM_PER_RUN = 30     # 单轮 GLM 调用上限，防止新词条过多时跑太久

# ---------------------------------------------------------------
# 本地兜底分类器：GLM 限流/失败时用它顶上，保证 50 条都有可用标签
# （纯关键词规则，零成本、零额度、瞬时完成）
# ---------------------------------------------------------------
DOMAIN_KEYWORDS = [
    ("体育", ["足球", "篮球", "比赛", "夺冠", "冠军", "球队", "球员", "奥运", "世界杯", "联赛",
              "进球", "网球", "乒乓", "游泳", "田径", "大满贯", "教练", "转会", "NBA", "CBA",
              "中超", "女排", "男足", "国足", "马拉松", "滑雪", "全运会", "季后赛", "半决赛"]),
    ("汽车", ["汽车", "新车", "车型", "新能源", "电动车", "特斯拉", "比亚迪", "蔚来", "理想",
              "自动驾驶", "续航", "充电", "车主", "车展", "油车", "混动"]),
    ("游戏", ["游戏", "手游", "玩家", "版本", "皮肤", "王者荣耀", "原神", "电竞", "开服", "卡池",
              "主机", "任天堂", "索尼", "steam", "副本", "赛季"]),
    ("影视", ["电影", "电视剧", "票房", "上映", "导演", "角色", "剧集", "影院", "首映", "影帝",
              "视后", "预告", "豆瓣", "收官", "大结局", "综艺", "网剧"]),
    ("健康", ["疾病", "病毒", "医院", "医生", "癌症", "减肥", "睡眠", "疫苗", "流感", "感染",
              "养生", "猝死", "体检", "心理", "抑郁", "健康", "病例", "症状"]),
    ("教育", ["高考", "中考", "考研", "大学", "学校", "老师", "教师", "学生", "作业", "考试",
              "录取", "毕业", "校园", "教师节", "开学", "补课", "学位"]),
    ("科技", ["iPhone", "手机", "华为", "小米", "芯片", "AI", "大模型", "机器人", "苹果", "安卓",
              "软件", "硬件", "折叠屏", "屏幕", "电池", "算力", "OpenAI", "谷歌", "微软", "鸿蒙",
              "像素", "摄像头", "系统", "算法", "发布", "app", "App", "鸿蒙"]),
    ("财经", ["股市", "A股", "股票", "基金", "央行", "利率", "汇率", "经济", "GDP", "楼市",
              "房价", "银行", "上市", "IPO", "财报", "营收", "市值", "黄金", "油价", "降息",
              "关税", "财政", "投资", "消费", "营收"]),
    ("时尚", ["穿搭", "时尚", "口红", "化妆", "美妆", "奢侈", "时装", "潮流", "发型", "美甲",
              "香水", "包包", "穿搭", "秀场"]),
    ("美食", ["美食", "吃货", "餐厅", "火锅", "奶茶", "咖啡", "零食", "小吃", "探店", "食谱",
              "烧烤", "月饼", "粽子", "菜", "吃"]),
    ("宠物", ["宠物", "萌宠", "流浪猫", "流浪狗", "领养", "大熊猫", "猫咪", "狗狗", "动物"]),
    ("旅游", ["旅游", "景区", "门票", "酒店", "机票", "航班", "旅行", "游客", "打卡", "免签",
              "出境", "签证", "民宿"]),
    ("国际", ["美国", "日本", "韩国", "俄罗斯", "乌克兰", "以色列", "巴勒斯坦", "欧盟", "联合国",
              "特朗普", "外交", "访问", "峰会", "制裁", "战争", "停火", "首相", "总统", "法国",
              "英国", "德国", "印度", "朝鲜", "伊朗"]),
    ("民生", ["放假", "假期", "调休", "工资", "社保", "医保", "退休", "养老金", "天气", "台风",
              "暴雨", "高温", "寒潮", "春运", "地铁", "公交", "物价", "菜价", "快递", "外卖",
              "供水", "停电", "施工", "补贴"]),
    ("社会", ["警方", "通报", "案件", "事故", "火灾", "救援", "判决", "法院", "杀人", "诈骗",
              "拐卖", "失联", "调查", "处罚", "拘留", "伤亡", "遇难", "目击", "曝光", "举报",
              "网友", "保安", "盗窃", "冲突", "造谣", "辟谣"]),
    ("娱乐", ["明星", "演员", "歌手", "恋情", "结婚", "离婚", "官宣", "粉丝", "演唱会", "出道",
              "偶像", "塌房", "路透", "海报", "剧组", "剧透", "顶流", "艺人", "经纪", "秀恩爱",
              "回应", "合照"]),
    ("情感", ["恋爱", "分手", "表白", "婚姻", "婆媳", "相亲", "前任", "暗恋", "彩礼", "该不该",
              "心酸", "感动", "泪目", "遗憾", "陪伴", "思念"]),
]

# 补充词表：热榜高频但上面漏掉的表达（人名类无法靠规则覆盖，属已知盲区）
DOMAIN_KEYWORDS += [
    ("娱乐", ["恋综", "真人秀", "综艺", "杀青", "路透", "爆料", "内幕", "工作室", "经纪人",
              "红毯", "造型", "现身", "发声", "发文", "复出", "退圈", "雪藏", "塌房", "代言",
              "演唱会", "巡演", "专辑", "出道", "恋情", "复合", "求婚", "自曝", "官宣",
              "生图", "素颜", "机场", "街拍", "同框", "客串", "番位", "热播", "新歌"]),
    ("社会", ["网友", "评论区", "热议", "争议", "摆摊", "摊贩", "街坊", "陷阱", "情侣",
              "夫妻", "上班", "双休", "加班", "打工人", "普通人", "月薪", "富二代", "家业",
              "维权", "讨薪", "罚款", "曝光", "举报", "冲突", "吵架", "打人", "路人",
              "同事", "老板", "房东", "租客", "外卖员", "相亲角", "彩礼"]),
    ("社会", ["丈夫", "妻子", "女子", "男子", "老人", "家长", "孩子", "邻居", "小区",
              "工作", "商场", "公交", "地铁", "街头", "摆摊", "摊主", "网友"]),
    ("健康", ["怀孕", "流产", "肥胖", "猝死", "中毒", "手术", "感染", "症状", "熬夜"]),
    ("科技", ["宇树", "机器人", "摄像头", "系统更新", "发布会", "大模型", "折叠屏", "像素"]),
    ("游戏", ["英雄联盟", "职业选手", "战队", "赛事", "选手", "开黑"]),
    ("情感", ["亲密", "暧昧", "出轨", "冷暴力", "婚姻", "婆媳", "暗恋", "前任", "异地恋"]),
    ("影视", ["收视", "排片", "影评", "剧照", "重映", "翻拍"]),
    ("教育", ["开学", "培训", "补课", "招生", "保研", "留学", "论文", "导师"]),
    ("国际", ["中东", "欧洲", "美国大选", "白宫", "北约", "使馆", "难民"]),
]

FORM_RULES = [
    ("直播", ["直播", "带货"]),
    ("数据可视化", ["数据", "报告", "盘点", "榜单", "排名统计"]),
    ("互动话题", ["该不该", "你怎么看", "投票", "热议", "网友", "评论区", "为什么"]),
    ("深度报道", ["通报", "调查", "专访", "报道", "揭秘", "还原", "起底"]),
    ("短视频", ["视频", "现场", "画面", "影像", "直播回放", "镜头"]),
]

LIFE_SHORT = ["官宣", "回应", "通报", "突发", "刚刚", "最新", "今日", "今天", "本周", "爆", "热",
              "新", "首发", "现场"]
LIFE_LONG = ["周年", "纪念", "历史", "首次", "每年", "常年", "传统", "长期", "以来", "回归"]


def classify_domain(title: str) -> str:
    for domain, kws in DOMAIN_KEYWORDS:
        for k in kws:
            if k in title:
                return domain
    return "其他"


def guess_form(title: str) -> str:
    for form, kws in FORM_RULES:
        for k in kws:
            if k in title:
                return form
    return "图文"


def guess_life(title: str) -> str:
    for k in LIFE_LONG:
        if k in title:
            return "长期"
    for k in LIFE_SHORT:
        if k in title:
            return "短期"
    return "中期"


def guess_keywords(title: str):
    import re
    words = []
    for m in re.findall(r"[A-Za-z][A-Za-z0-9\.\+\-]{1,20}|\d{2,}", title):
        if m not in words:
            words.append(m)
    parts = re.split(r"[，。！？、：；\s]+|的|了|在|和|与|被|把|让|致|为|对|从|到|是|有|都|就|还|也|又|将|已", title)
    parts = sorted([p for p in parts if len(p) >= 2], key=len, reverse=True)
    for p in parts:
        if len(words) >= 4:
            break
        if p not in words:
            words.append(p)
    return words[:4]


def local_analysis(title: str):
    """本地兜底分析（GLM 不可用时使用）"""
    return {
        "创作领域": classify_domain(title),
        "内容形态": guess_form(title),
        "生命周期": guess_life(title),
        "核心话题词": guess_keywords(title),
    }


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
                # 免费档是按天配额，15 秒后重试从来没成功过 → 直接放弃，交给本地兜底
                print(f"  [RATE-LIMIT] {keyword}: 429（免费额度已用尽）")
                return "RATE_LIMITED"
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


def load_prev(path):
    """读取上一轮结果，返回 {title: item}"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        return {}
    return {it.get("title"): it for it in prev.get("items", []) if it.get("title")}


def signature(result) -> str:
    """内容指纹（不含 updated_at），用于判断是否真的需要写盘"""
    return json.dumps({
        "source": result.get("source"),
        "summary": result.get("summary"),
        "clusters": result.get("clusters"),
        "items": result.get("items"),
    }, ensure_ascii=False, sort_keys=True)


def main():
    if not API_KEY:
        print("缺少 GLM_API_KEY 环境变量", file=sys.stderr)
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
    print(f"共 {len(items_raw)} 条热榜；上一轮缓存 {len(prev_map)} 条")

    items = []
    reused = called = local_fb = 0
    rate_streak = 0
    blocked = False

    for i, it in enumerate(items_raw, 1):
        t = it.get("title", "")
        if not t:
            continue

        cached = prev_map.get(t)
        # 旧数据没有「分析来源」字段，一律视为 GLM 产出（当时只有 GLM 一条路）
        cached_glm = (
            cached
            and cached.get("分析来源", "GLM") == "GLM"
            and cached.get("创作领域")
            and cached.get("创作领域") != "其他"
        )
        engine = "GLM"

        if cached_glm:
            analysis = {
                "创作领域": cached.get("创作领域", "其他"),
                "内容形态": cached.get("内容形态", ""),
                "生命周期": cached.get("生命周期", ""),
                "核心话题词": cached.get("核心话题词", []),
            }
            reused += 1
        elif blocked or called >= MAX_GLM_PER_RUN:
            analysis = local_analysis(t)
            engine = "本地"
            local_fb += 1
        else:
            r = call_glm(t)
            called += 1
            if r == "RATE_LIMITED":
                rate_streak += 1
                analysis = local_analysis(t)
                engine = "本地"
                local_fb += 1
                if rate_streak >= MAX_RATE_STREAK:
                    blocked = True
                    print(f"  连续 {rate_streak} 次 429，GLM 熔断（免费额度已用尽），"
                          f"本轮剩余词条改用本地兜底分类")
            elif r:
                rate_streak = 0
                analysis = {
                    "创作领域": r.get("创作领域", "其他") or "其他",
                    "内容形态": r.get("内容形态", ""),
                    "生命周期": r.get("生命周期", ""),
                    "核心话题词": r.get("核心话题词", []) or [],
                }
            else:
                analysis = local_analysis(t)
                engine = "本地"
                local_fb += 1
            time.sleep(0.4)

        emo = senti_map.get(t) or (cached or {}).get("情感倾向") or "中性"
        items.append({
            "title": t,
            "情感倾向": emo,
            "创作领域": analysis["创作领域"],
            "内容形态": analysis["内容形态"],
            "生命周期": analysis["生命周期"],
            "核心话题词": analysis["核心话题词"],
            "分析来源": engine,
            "url": it.get("url", ""),
        })

    solid = sum(1 for x in items if x["创作领域"] not in ("其他", ""))
    print(f"共 {len(items)} 条：复用 GLM 缓存 {reused} 条 / 本轮调 GLM {called} 次 / "
          f"本地兜底 {local_fb} 条；有有效领域标签 {solid} 条")
    if solid < len(items):
        print(f"  未命中领域的词条 {len(items) - solid} 条（显示为「其他」）")

    clusters = defaultdict(list)
    for it in items:
        clusters[it["创作领域"]].append(it)
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

    # 内容无变化 → 不写盘，workflow 也就不会产生空提交
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
