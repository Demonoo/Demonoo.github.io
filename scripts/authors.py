# -*- coding: utf-8 -*-
"""
话题参与作者采集（微博话题聚合页）

背景：
- 微博话题聚合页的作者数据只能通过真实浏览器拿到（直连 API 返回 ok=-100、
  HTML 302，都会跳登录页；浏览器执行完 JS 访客流程后内容才渲染出来）。
- 本脚本用 headless Chrome + CDP 顺序渲染每个话题页，提取：
    · 话题统计：阅读量 / 讨论量 / 主持人 / 媒体发布数
    · **热门 tab 的前 N 条（默认 10）**：昵称、认证等级、认证说明、身份类型、互动量、**正文文案**
    · 正文实体词库：从帖子正文挖「《作品名》」与高频「#话题#」
      —— 热搜词条常把剧名切坏（早春晴朗云合超藏海传 → 超藏/海传），
         而帖子正文里《藏海传》会完整高频出现，可反向补回实体，交给 analyze.py 做区间保护
- 输出 data/authors.json（话题性质由 analyze.py 的大模型判定，此处不重复推断）

**采集口径 = 热门 tab（containerid type=60）**，见 norm_url 的注释：
  「综合」tab（type=1）把「热门微博」和「实时微博」两区混在一个页面里、
  且两区卡片类名相同 → 会掺进大量实时微博；换成 type=60 即纯热门，实测恰好 10 条。

微博认证图标对照（实测 2026-09）：
    i.m-icon-goldv   → 金V（优质创作者）
    i.m-icon-redv    → 红V（个人认证）
    i.m-icon-orangev → 橙V（早期个人认证，存量）
    i.m-icon-bluev   → 蓝V（机构 / 媒体 / 政务 / 企业）
    img.vipicon      → 微博会员（SVIP），与认证无关，不能当认证用

依赖：websocket-client
"""
import os, re, sys, json, time, argparse, subprocess, tempfile, urllib.request, urllib.parse, shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOTSPOTS = os.path.join(BASE, "data", "hotspots.json")
RAW = os.path.join(BASE, "data", "raw_hotspots.json")
OUT = os.path.join(BASE, "data", "authors.json")
DOUYIN_HOTSPOTS = os.path.join(BASE, "data", "douyin_hotspots.json")
DOUYIN_RAW = os.path.join(BASE, "data", "douyin_raw_hotspots.json")
DOUYIN_OUT = os.path.join(BASE, "data", "douyin_authors.json")

TOP_N = int(os.environ.get("AUTHORS_TOP_N", "10"))
LIMIT = int(os.environ.get("AUTHORS_LIMIT", "0"))      # 0 = 全部
PORT = int(os.environ.get("CDP_PORT", "49621"))
PAGE_WAIT = float(os.environ.get("AUTHORS_WAIT", "20"))

CHROME_CANDIDATES = [
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    os.path.expanduser("~/AppData/Local/Google/Chrome/Application/chrome.exe"),
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium",
]


def find_chrome():
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def launch_chrome(port, profile, headless=True):
    exe = find_chrome()
    if not exe:
        raise RuntimeError("未找到 Chrome/Chromium，无法渲染话题页")
    args = [exe, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
            "--disable-gpu", "--no-sandbox", "--no-first-run",
            "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
            "--no-proxy-server", "--proxy-bypass-list=*",
            "--window-size=430,900", "about:blank"]
    if headless:
        args.insert(4, "--headless=new")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class CDP:
    def __init__(self, ws_url, timeout=45):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self.ws.settimeout(0.5)          # 非阻塞轮询：无消息时 _recv 返回 None
        self._id = 0
        self._events = []                # cmd() 期间到达的事件先缓存，poll_events 消费

    def _recv(self):
        try:
            return json.loads(self.ws.recv())
        except Exception:
            return None

    def cmd(self, method, params=None):
        self._id += 1
        i = self._id
        self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        while True:
            r = self._recv()
            if r is None:
                return None             # 超时（0.5s）→ 不阻塞，交由上层重试
            if r.get("id") == i:
                return r
            self._events.append(r)      # 非本请求的响应 = 事件，缓存供 poll_events

    def poll_events(self):
        """取走缓存 + socket 上当前可用的事件消息"""
        out = []
        if self._events:
            out.extend(self._events)
            self._events = []
        while True:
            r = self._recv()
            if r is None:
                break
            out.append(r)
        return out

    def evaluate(self, expr):
        r = self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        try:
            return r["result"]["result"].get("value")
        except Exception:
            return None

    def get_response_body(self, request_id):
        """取 Network.responseReceived 命中的响应体（base64 自动解码）"""
        r = self.cmd("Network.getResponseBody", {"requestId": request_id})
        try:
            res = r["result"]["result"]
            body = res.get("body") or ""
            if res.get("base64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", "replace")
            return body
        except Exception:
            return None

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


EXTRACT_JS = r"""
(function(){
  function txt(el){ return el ? (el.innerText||'').replace(/\s+/g,' ').trim() : ''; }
  var body = document.body ? (document.body.innerText||'') : '';
  var stats = {};
  var m;
  m = body.match(/阅读量\s*([0-9.]+[万亿]?)/); if (m) stats.read = m[1];
  m = body.match(/讨论量\s*([0-9.]+[万亿]?)/); if (m) stats.discuss = m[1];
  m = body.match(/主持人[:：]\s*([^|\n]+)/);    if (m) stats.host = m[1].trim().slice(0,30);
  m = body.match(/(\d+)\s*家媒体/);            if (m) stats.media = parseInt(m[1],10);

  // 认证等级：只看 <i class="m-icon m-icon-*v">，vipicon 是会员图标不算认证
  function verifyOf(card){
    var els = card.querySelectorAll('header i[class*="m-icon"]');
    for (var i=0;i<els.length;i++){
      var cl = els[i].className || '';
      if (/goldv/.test(cl))   return '金V';
      if (/redv/.test(cl))    return '红V';
      if (/orangev/.test(cl)) return '橙V';
      if (/bluev/.test(cl))   return '蓝V';
      if (/yellowv/.test(cl)) return '黄V';
    }
    return '普通';
  }

  // 热门 tab 的正文卡片。**必须在 type=60 的热门 tab 页面上取** ——
  // 综合 tab（type=1）里「热门微博」和「实时微博」两区用的是同一个类名
  // `.card.weibo-member`，会混进来一堆实时微博（见 norm_url 注释）。
  // 热门 tab 实测恰好 10 条，取完即止。
  var cards = document.querySelectorAll('.card.weibo-member');
  var authors = [], seen = {};
  for (var i=0;i<cards.length;i++){
    var c = cards[i];
    var name = txt(c.querySelector('h3')).replace(/[\s\u200b]+/g,'');
    if (!name || seen[name]) continue;      // 同一作者只留首条（页面已按热度排序）
    seen[name] = 1;

    var raw = txt(c.querySelector('.from'));
    if (/^来自/.test(raw)) raw = '';         // 「来自 iPhone Air」是发布来源，非认证说明
    // 认证说明可能很长，是**叠加**的多重头衔，实测最长 38 字：
    //   「2023微博影像年年度银奖 2024微博影像年优秀摄影师 摄影世界认证摄影师」
    //   「财联社（https://www.cls.cn）官方微博」
    // 原先截到 24 字 → 尾巴被切掉（「…）官」/「…微博原创视」），而且会**破坏身份类型判定**
    // （截掉「官方微博」后蓝V 判不出「官方机构」）→ 放宽到 60，只留一个防脏值的上限
    var t = txt(c.querySelector('.time')).replace(/转赞人数.*$/,'').trim();
    var nums = (txt(c.querySelector('footer')).match(/\d+/g) || []).map(Number);
    var reposts = nums[0]||0, comments = nums[1]||0, likes = nums[2]||0;

    // 帖子正文（作者发布的文案）：正文节点是 .weibo-text；
    // innerText 末尾会带「…全文」展开按钮，一并去掉
    var wt = c.querySelector('.weibo-text');
    var text = wt ? (wt.innerText || '').replace(/\s+/g, ' ').trim() : '';
    text = text.replace(/[.．…\s]*全文$/, '').trim();
    if (text.length > 110) text = text.slice(0, 110) + '…';

    authors.push({
      name: name.slice(0,24),
      verify: verifyOf(c),
      identity_raw: raw.slice(0,60),
      time: t.slice(0,14),
      text: text,
      reposts: reposts, comments: comments, likes: likes,
      hot: reposts + comments + likes
    });
    if (authors.length >= 20) break;
  }

  // 正文实体线索：《作品名》精度极高；#话题# 噪声较大，交给 Python 侧按出现次数过滤
  function tally(re){
    var m, o = {};
    while ((m = re.exec(body))) { var k = (m[1] || '').trim(); if (k) o[k] = (o[k] || 0) + 1; }
    return o;
  }
  var book = tally(/《([^》]{1,20})》/g);
  var hash = tally(/#([^#\s]{2,20})#/g);

  // 微博文案 n-gram 词频：从每个 .weibo-text 节点抽 2-4 字连续中文片段
  // —— 真正的讨论热词（人名、产品名、事件术语）必出现 ≥2 次；单次 n-gram 噪声太多
  // 注意：保留下来由 Python 端再按「必须在标题里 / 过滤作者名 / 过滤停用词」筛选
  var wtNodes = document.querySelectorAll('.weibo-text');
  var kw_freq = {};
  for (var i = 0; i < wtNodes.length; i++) {
    var txt = (wtNodes[i].innerText || '').replace(/[#@《》\n\r\u200b]/g, ' ');
    var segs = txt.match(/[\u4e00-\u9fff]+/g) || [];
    for (var si = 0; si < segs.length; si++) {
      var seg = segs[si];
      if (seg.length < 4) continue;          // 至少 4 字才有 n-gram 价值
      for (var n = 2; n <= 4; n++) {         // 2/3/4 gram
        for (var j = 0; j + n <= seg.length; j++) {
          var g = seg.substr(j, n);
          kw_freq[g] = (kw_freq[g] || 0) + 1;
        }
      }
    }
  }
  var kw_freq_filtered = {};
  for (var k in kw_freq) if (kw_freq[k] >= 2) kw_freq_filtered[k] = kw_freq[k];

  return JSON.stringify({ok: !!cards.length, stats: stats, authors: authors,
                         book: book, hash: hash, kw_freq: kw_freq_filtered});
})()
"""


def norm_url(it):
    """把词条转成 m.weibo.cn 搜索页的 **热门 tab** URL。

    containerid 里的 `type=` 决定落在哪个 tab（实测 2026-09-12）：
      `type=1`  = 综合（混排：热门头图 + 「更多热门微博」+ **实时微博** 一大串，15~20 条不等）
      `type=60` = **热门**（就是我们要的：恰好 10 条，且不含实时微博）
    热门 tab 是 SPA 原地切换、URL 不变，但**直接导航 type=60 就能拿到同一批卡片**，
    省掉点击 + 二次等渲染（点击后平台会请求
    `/api/container/getIndex?containerid=100103type%3D60%26q%3D…&page_type=searchall`）。
    """
    u = (it.get("url") or "").strip()
    q = ""
    if "m.weibo.cn/search?containerid=" in u:
        # 词条自带 URL（fetch.py 写入）里已有 q，但 type 是 1（综合）→ 取 q 后重拼
        m = re.search(r"q%3D([^&]*)", u)
        if m:
            q = urllib.parse.unquote(m.group(1))
    if not q and "s.weibo.com/weibo?q=" in u:
        q = urllib.parse.unquote(u.split("q=", 1)[1].split("&")[0])
    if not q:
        q = "#" + it.get("title", "") + "#"
    return ("https://m.weibo.cn/search?containerid=100103type%3D60%26q%3D"
            + urllib.parse.quote(q))



# ---------- 正文实体词库 ----------

def _norm_name(s):
    return re.sub(r"[\s#]+", "", s or "").lower()


# 实体名只允许：字母数字下划线、汉字、间隔号、连字符、英文句点
# —— 挡掉「#早春晴朗#」「早春晴朗云合34.8%」这类含 # 或 % 的脏值
LEX_OK = re.compile(r"^[\w·\-\.]+$")


def _lex_ok(name) -> bool:
    return bool(name) and len(name) <= 20 and bool(LEX_OK.match(name))


def build_lexicon(book, hash_, kw_freq, title, max_items: int = 30):
    """从话题页正文挖出的实体名（供 analyze.py 做「实体区间保护」）

    - 《…》：中文作品名的强信号，出现 1 次即采纳（权重 100+）
    - #…#：噪声大（页面会混入无关话题号、乃至整条热搜标题），要求出现 ≥2 次且长度 ≤12
    - 微博文案 n-gram（kw_freq）：讨论热词，要求 ≥2 次且长度 2-6
    - 一律排除与词条本身等价的串（页面里词条自己的话题号会刷屏）
    """
    key = _norm_name(title)
    score = {}
    for name, c in (book or {}).items():
        n = _norm_name(name)
        if 2 <= len(name) <= 20 and n and n != key and _lex_ok(name):
            score[name] = max(score.get(name, 0), 100 + int(c))
    for name, c in (hash_ or {}).items():
        try:
            c = int(c)
        except Exception:
            continue
        n = _norm_name(name)
        if c >= 2 and 2 <= len(name) <= 12 and n and n != key and _lex_ok(name):
            score[name] = max(score.get(name, 0), 10 + c)
    for name, c in (kw_freq or {}).items():
        try:
            c = int(c)
        except Exception:
            continue
        n = _norm_name(name)
        # 2-4 字、出现 ≥2 次、CJK 字符；过滤明显纯数字
        if c >= 2 and 2 <= len(name) <= 4 and n and n != key and re.match(r'^[\u4e00-\u9fff]+$', name):
            score[name] = max(score.get(name, 0), c)
    return [k for k, _ in sorted(score.items(), key=lambda x: (-x[1], -len(x[0])))[:max_items]]


def top_kw_freq(kw_freq, title, top_n: int = 40):
    """从微博文案 n-gram 词频里挑 top_n（按频次降序，长度 2-4，纯 CJK）"""
    key = _norm_name(title)
    out = []
    if not kw_freq:
        return out
    for name, c in sorted(kw_freq.items(), key=lambda x: (-int(x[1] or 0), -len(x[0]))):
        try:
            c = int(c)
        except Exception:
            continue
        n = _norm_name(name)
        if c < 2 or not n or n == key:
            continue
        if not (2 <= len(name) <= 4):
            continue
        if not re.match(r'^[\u4e00-\u9fff]+$', name):
            continue
        out.append([name, c])
        if len(out) >= top_n:
            break
    return out


def load_prev_lexicon(path):
    """上一轮已挖到的词库：本轮某话题渲染失败时沿用，避免词库闪断"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return {t: v["lexicon"] for t, v in (d.get("topics") or {}).items()
            if isinstance(v, dict) and v.get("lexicon")}


# ---------- 身份类型推断 ----------

# —— 机构蓝V ——
MEDIA_KW = ["日报", "晚报", "时报", "早报", "新闻", "电视台", "频道", "广播", "传媒", "媒体",
            "周刊", "资讯", "前线", "网", "报", "财经", "日报社"]
OFFICIAL_KW = ["公安", "消防", "法院", "检察", "政府", "宣传部", "团委", "交警", "政务",
               "外事", "气象", "应急", "卫健", "文旅", "教育局", "发布厅", "网信", "共青团"]
BRAND_KW = ["官方微博", "旗舰", "品牌", "商城", "俱乐部", "工作室", "公司", "集团", "科技",
            "汽车", "手机", "游戏", "电影", "音乐", "银行", "保险"]
# —— 个人认证（红V/橙V/金V）——
# 强媒体标识：命中即判「媒体人」，优先于博主（如「资深媒体人 … 新知博主」应算媒体人）
MEDIA_STRONG = ["媒体人", "法人微博", "通讯社", "联社", "电视台", "频道", "广播", "日报",
                "晚报", "时报", "早报", "新闻", "传媒", "周刊", "资讯", "报业", "融媒体", "前线"]
# 博主标识
BLOGGER_KW = ["博主", "创作者", "作者", "达人", "大V", "观察官", "自媒体", "作家", "评论人"]
# 弱媒体标识：仅在没有博主标识时才生效
# （否则「网络作家」「电视剧博主」「微博社区志愿者」会被「网/电视/社」误判成媒体人）
MEDIA_WEAK = ["官方微博", "网", "报", "媒体"]


# ---------- 抖音作者身份推断 ----------
# 抖音横滑区作者卡自带认证徽章图标（x-image[class*="verify"]，src 含颜色词），
# 可直接判黄V/蓝V/红V，不必像早期「搜索卡片」那样靠红标或昵称猜。
# 蓝V 里既有官方机构也有企业，再按昵称关键词细分；判词与微博 enrich 同源，
# 保证前端「媒体 / 官方机构 / 企业品牌 / 认证」标签语义一致。

# 媒体强标识：命中即判「媒体人」
DY_MEDIA_KW = ["日报", "晚报", "时报", "早报", "新闻", "电视台", "广播", "央视", "新华社",
               "人民网", "参考消息", "新华网", "视听", "融媒体", "传媒", "周刊", "资讯",
               "报业", "前线", "新京报", "封面", "观察者", "澎湃", "南都", "界面",
               "新媒体", "光明网", "环球网", "中新网", "海外网", "中青报", "经济日报"]
# 官方机构标识
DY_OFFICIAL_KW = ["公安", "消防", "法院", "检察", "政府", "政务", "文旅", "教育局",
                  "气象", "应急", "卫健委", "共青团", "海关", "税务", "市场监管",
                  "交警", "发布", "网信"]
# 企业品牌标识
DY_BRAND_KW = ["公司", "集团", "科技", "汽车", "手机", "银行", "保险", "官方旗舰",
               "旗舰店", "品牌", "数码", "电商"]
# 认证创作者（明星/达人/工作室）
DY_CREATOR_KW = ["演员", "歌手", "导演", "主持人", "运动员", "达人", "博主", "工作室",
                 "主理人", "大V", "作家", "医生", "律师", "老师"]


def enrich(a):
    """补 identity（身份类型）"""
    raw = (a.get("identity_raw") or "").strip()
    verify = a.get("verify", "普通")
    if verify == "蓝V":
        if any(k in raw for k in OFFICIAL_KW):
            ident = "官方机构"
        elif any(k in raw for k in MEDIA_KW):
            ident = "媒体"
        elif any(k in raw for k in BRAND_KW):
            ident = "企业品牌"
        else:
            ident = "机构"
    elif verify in ("金V", "红V", "橙V", "黄V"):
        if any(k in raw for k in MEDIA_STRONG):
            ident = "媒体人"
        elif any(k in raw for k in BLOGGER_KW):
            ident = "认证博主"
        elif any(k in raw for k in MEDIA_WEAK):
            ident = "媒体人"
        else:
            ident = "认证博主"
    else:
        ident = "普通用户"
    a2 = dict(a)
    a2["identity"] = ident
    return a2


# ---------- 抖音搜索聚合页作者采集（非 headless Chrome + DOM 提取昵称） ----------

# 抖音无感风控（rmc-nocaptcha）会识别 headless 自动化指纹：headless 下搜索 API
# 返回 verify_check 空数据，DOM 无作者；真实窗口（含 xvfb 虚拟显示）可通过验证。
# 因此抖音作者采集必须用非 headless 模式（CI 上经 xvfb-run 提供显示）。
# 聚合页必须带落地页点击词条时的完整参数（gid/hotlist_param/extra），
# 裸 so.douyin.com/s?keyword=… 会被风控；实测昵称节点 class 含 nickName（2026-09-12）。
DOUYIN_WAIT = float(os.environ.get("DOUYIN_WAIT", "15"))
# 综合 tab 顶部横滑区（模块 douyin_hotspot_horizontal，卡片 id search-horizontal-item-N）
# —— **本页唯一的作者来源**。
# 实测（2026-09-12）：横滑区卡片是结构化渲染的，作者三件套各自独立成节点：
#   x-text[class*="w-full"]   视频文案（作者发布内容）
#   x-image[class*="avatar"]  作者头像
#   x-text[class*="ml-4"]     作者昵称
#   x-image[class*="verify"]  认证徽章图标（src 含颜色词 → 零请求判类型）
# 为什么不用下方「搜索结果卡片」的作者行：那里是关键词搜索命中的任意投稿者，
# 实测多为个人小号且**无认证节点**（verify 字段恒空 → 身份类型维度失效）；
# 横滑区则是该热点的精选/媒体内容，作者带真实认证，与页面所见一致。
# 容量：横滑区为固定一组，实测 track.scrollWidth == clientWidth == 560px（5~6 张），
#       横向滚动**不会**加载更多 → 单话题作者上限就是 5~6 位。
DOUYIN_HZ_JS = r"""(() => {
  const out = [];
  document.querySelectorAll('[id^="search-horizontal-item-"]').forEach(el => {
    const clsOf = e => String((e.getAttribute && e.getAttribute('class')) || '');
    const txt = e => (e.innerText || '').replace(/\s+/g, ' ').trim();
    const texts = Array.from(el.querySelectorAll('x-text, [class*="normal-text"]'))
      .filter(e => txt(e));
    const titleEl = texts.find(e => /w-full/.test(clsOf(e))) || texts[0];
    const nameEl = texts.find(e => /ml-4/.test(clsOf(e))) || texts[texts.length - 1];
    if (!nameEl) return;
    const name = txt(nameEl).slice(0, 24);
    if (!name) return;
    const imgs = Array.from(el.querySelectorAll('x-image, img'));
    const imgSrc = e => String((e && e.getAttribute && e.getAttribute('src')) || '');
    const verifyEl = imgs.find(i => /verify/.test(clsOf(i)));
    out.push({
      name: name,
      text: titleEl ? txt(titleEl).slice(0, 80) : '',
      verifySrc: imgSrc(verifyEl).slice(0, 160),
    });
  });
  return JSON.stringify(out);
})()"""

# 失败现场诊断：区分「被风控拦截」与「该话题本来就没有横滑精选位」。
# 判据：
#   · 页面正常渲染（bodyLen 有量、x-text 有节点）但没有 search-horizontal-item
#     → 该话题确实没有精选位，是**正确结果**，不该当故障
#   · URL 被重定向（含 /login、verify）、bodyLen 极小、出现 captcha 节点
#     → 被风控拦了，是**环境故障**
# 有了它，CI 日志不再只有一句模糊的「未渲染出作者」，能直接看出是哪种。
DOUYIN_DIAG_JS = r"""(() => {
  const n = s => { try { return document.querySelectorAll(s).length } catch (e) { return -1 } };
  const body = document.body ? (document.body.innerText || '') : '';
  return JSON.stringify({
    url: String(location.href).slice(0, 130),
    bodyLen: body.length,
    hz: n('[id^="search-horizontal-item-"]'),
    hzAny: n('[id*="search-horizontal"]'),
    captcha: n('[class*="captcha"],[id*="captcha"]'),
    xtext: n('x-text'),
    head: body.slice(0, 50).replace(/\s+/g, ' ')
  });
})()"""


def douyin_diag(cdp):
    """采集失败时的现场诊断摘要（任何异常都不上抛，返回一行字符串）"""
    try:
        d = json.loads(cdp.evaluate(DOUYIN_DIAG_JS) or "{}")
    except Exception as e:
        return f"diag 失败({type(e).__name__})"
    if not d:
        return "diag 空"
    # 注意：**不能拿 captcha 节点数当判据**。实测抖音搜索页常驻一个隐藏的 captcha 容器，
    # 正常渲染时也会命中它 —— 本地 41/50 成功那轮里，失败的 9 条 cap 全是 1，
    # 但 body/head 显示页面其实正常渲染了搜索结果（只是该话题没有精选位）。
    # 真正的风控特征是「页面几乎空白」或「URL 被重定向到登录/验证页」。
    url = str(d.get("url") or "")
    flag = "疑似风控" if (d.get("bodyLen", 0) < 300 or "/login" in url) else "无精选位"
    return (f"[{flag}] body={d.get('bodyLen')} hz={d.get('hz')}/{d.get('hzAny')} "
            f"cap={d.get('captcha')} x-text={d.get('xtext')} "
            f"url={d.get('url')} head={d.get('head')!r}")


def build_douyin_topic_url(it, pd=None):
    """构造落地页点击词条后的完整聚合页 URL（参数结构与落地页跳转一致）

    pd 保留兼容（"video" 会带 video tab 参数）；默认综合 tab（与落地页跳转一致）。
    """
    title = it.get("title", "")
    gid = str(it.get("gid") or "")
    try:
        rank = int(it.get("position") or it.get("realpos") or 0)
    except Exception:
        rank = 0
    try:
        ts = int(it.get("event_time") or 0)
    except Exception:
        ts = 0
    if not (gid and rank and ts):
        return None
    hp = {"board_type": 0, "rank": rank, "time": ts}
    hp_s = json.dumps(hp, ensure_ascii=False, separators=(",", ":"))
    extra = {"hotlist_param": hp_s, "previous_page": "trending_board_page",
             "gid": gid, "enter_method": "hot_list_page"}
    # 视频 tab 用 switch_tab 入口，与落地页实际跳转参数一致（实测 2026-09-12）
    enter = "switch_tab" if pd else "hot_list_page"
    params = {
        "hideMiddlePage": "1", "needBack2Origin": "1", "from": "hot_list_page",
        "enter_method": enter, "previous_page": "trending_board_page",
        "keyword": title, "gid": gid,
        "hotlist_param": hp_s,
        "extra": json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
    }
    if pd:
        params["pd"] = pd
        params["offset"] = "0"
    q = urllib.parse.urlencode(params)
    return "https://so.douyin.com/s?" + q


def dy_verify_from_url(url):
    """按认证徽章图标 URL 判定认证类型（横滑推荐位 x-image，零请求）

    实测（2026-09-12）：URL 直接含颜色词：
      icon_verify_yellow_outlined → 黄V（个人认证）
      icon_verify_blue_outlined   → 蓝V（机构/企业认证）
      icon_verify_red             → 红V（持新闻许可媒体，注意无 _outlined 后缀）
    """
    u = (url or "")
    if "icon_verify_yellow" in u or "icon_verify_yellow_outlined" in u:
        return "黄V"
    if "icon_verify_blue" in u or "icon_verify_blue_outlined" in u:
        return "蓝V"
    if "icon_verify_red" in u:
        return "红V"
    return ""


def collect_douyin(cdp, it, wait=DOUYIN_WAIT):
    """打开抖音搜索聚合页综合 tab，以**顶部横滑区**为作者信息源

    实测（2026-09-12，访客态未登录）：
    - 横滑区（douyin_hotspot_horizontal）卡片里作者三件套各自独立成节点：
      昵称 / 文案 / 认证徽章图标 URL，src 含颜色词 → 零请求判黄V·蓝V·红V
    - 下方「搜索结果卡片」的作者行虽然带点赞数，但实测多为个人小号且**无认证节点**，
      与页面所见不符，身份类型维度失效 → 不再采用（详见 DOUYIN_HZ_JS 上方注释）
    - 横滑区是固定一组（实测 5~6 张）且卡片不含点赞数 → 本源 likes 恒为 0
    - 未渲染出横滑区时该话题本轮跳过（上游保留上一次 authors.json）

    返回 (结果, 诊断)：结果为 {authors, stats}，失败时为 None；
    诊断仅在失败时有内容（区分风控拦截 / 话题本来就没有精选位，见 douyin_diag）。
    """
    url = build_douyin_topic_url(it, pd=None)
    if not url:
        return None, "缺 gid/position，无法构造聚合页 URL"
    cdp.cmd("Page.navigate", {"url": url})
    deadline = time.time() + wait
    cards = []
    stable = 0
    while time.time() < deadline:
        time.sleep(1.2)
        try:
            raw = cdp.evaluate(DOUYIN_HZ_JS)
            if raw:
                cur = [x for x in json.loads(raw) if x and x.get("name")]
                if cur:
                    if len(cur) > len(cards):
                        cards, stable = cur, 0
                    else:
                        stable += 1
                    # 横滑区为固定一组、不随滚动增长：连续两轮数量不变即渲染完
                    if stable >= 2:
                        break
        except Exception:
            pass
    if not cards:
        return None, douyin_diag(cdp)
    # 同一账号可能占据多张精选卡 → 按昵称去重，保留首次出现顺序
    uniq = {}
    for x in cards:
        uniq.setdefault(x["name"], x)
    authors = []
    for n, x in list(uniq.items())[:TOP_N]:
        verify = dy_verify_from_url(x.get("verifySrc", ""))
        # 身份类型：带认证按 V 标 + 昵称关键词推断；无认证留空（前端显示普通用户）
        ident = ""
        if verify:
            if verify == "红V":
                ident = "媒体人"
            elif verify == "黄V":
                ident = "认证创作者"
            else:
                if any(k in n for k in DY_MEDIA_KW):
                    ident = "媒体人"
                elif any(k in n for k in DY_OFFICIAL_KW):
                    ident = "官方机构"
                elif any(k in n for k in DY_BRAND_KW):
                    ident = "企业品牌"
                elif any(k in n for k in DY_CREATOR_KW):
                    ident = "认证创作者"
                else:
                    ident = "机构认证"
        authors.append({
            "name": n[:24],
            "verify": verify,
            "identity": ident, "identity_raw": "",
            "sec_uid": "",
            "text": (x.get("text") or "")[:80],
            "likes": 0, "hot": 0,
        })
    return {"authors": authors, "stats": {}}, ""


def main():
    parser = argparse.ArgumentParser(description="话题参与作者采集（微博/抖音）")
    parser.add_argument("--platform", choices=["weibo", "douyin"], default="weibo")
    args = parser.parse_args()
    is_douyin = args.platform == "douyin"
    if is_douyin:
        out_path = DOUYIN_OUT
        # 抖音需要 gid/position/event_time 拼聚合页 URL。
        # **优先读产品文件 douyin_hotspots.json**（= 前端消费的同一份，词条天然对齐）；
        # 早期的 hotspots 没透传这些字段，此时按 title 从 raw 补（见下方补字段逻辑）。
        # 反例教训：以前无条件优先 raw，而 raw 是「刚抓的榜」、hotspots 是「已分析的榜」，
        # 热榜分钟级刷新时两批会错位 —— 实测交集仅 42/50，前端表现为「词条在、作者空」。
        src = DOUYIN_HOTSPOTS if os.path.exists(DOUYIN_HOTSPOTS) else DOUYIN_RAW
        source = "抖音"
        # 抖音无感风控识别 headless 指纹 → 非 headless（CI 上经 xvfb-run 提供显示）
        headless = False
    else:
        out_path = OUT
        src = HOTSPOTS if os.path.exists(HOTSPOTS) else RAW
        source = "微博"
        headless = True

    with open(src, encoding="utf-8") as f:
        d = json.load(f)
    items = d.get("items") or [{"title": t} for t in (d.get("titles") or [])]
    items = [it for it in items if it.get("title")]

    # 抖音：为缺跳转参数的条目从 raw 按 title 补齐（raw 是唯一带 gid 的原始产物）
    if is_douyin and os.path.exists(DOUYIN_RAW):
        try:
            with open(DOUYIN_RAW, encoding="utf-8") as f:
                _rd = json.load(f)
            _idx = {x.get("title"): x for x in (_rd.get("items") or []) if x.get("title")}
            _filled = 0
            for it in items:
                if it.get("gid"):
                    continue
                r = _idx.get(it.get("title"))
                if not r:
                    continue
                for k in ("gid", "position", "event_time"):
                    if r.get(k) is not None:
                        it[k] = r[k]
                _filled += 1
            _lack = sum(1 for x in items if not x.get("gid"))
            if _filled or _lack:
                print(f"[authors] 从 raw 补齐 {_filled} 条跳转参数；"
                      f"{_lack} 条缺 gid（榜单已刷新、raw 里已无该词条）→ 本轮跳过")
        except Exception as e:
            print(f"[authors] raw 补字段失败：{type(e).__name__}: {e}", file=sys.stderr)

    if LIMIT > 0:
        items = items[:LIMIT]
    scope = "聚合页横滑精选位" if is_douyin else "热门 tab（containerid type=60）"
    print(f"[authors] 话题数 {len(items)}（源 {os.path.basename(src)}，{source}），"
          f"采集口径 {scope}，每话题取前 {TOP_N} 条")

    profile = tempfile.mkdtemp(prefix="chrome-authors-")
    proc = launch_chrome(PORT, profile, headless=headless)
    prev_lex = load_prev_lexicon(out_path)

    ts = None
    for _ in range(60):
        try:
            ts = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/json", timeout=2).read())
            if ts:
                break
        except Exception:
            time.sleep(0.5)
    if not ts:
        print("[authors] Chrome 未就绪", file=sys.stderr)
        sys.exit(1)

    page = next((t for t in ts if t["type"] == "page"), ts[0])
    cdp = CDP(page["webSocketDebuggerUrl"])
    cdp.cmd("Page.enable")
    cdp.cmd("Runtime.enable")
    cdp.cmd("Network.enable")   # 抖音搜索页需监听数据 API 响应；对微博无副作用

    topics = {}
    ok_cnt = 0
    lex_cnt = 0
    t0 = time.time()
    # 早退保护：连续 N 个话题一个作者都没渲染出来，且本轮至今 0 成功
    # → 几乎必然是机房 IP 被风控 / 浏览器环境异常，而不是话题真的没有作者。
    # 实测 GitHub Actions 上微博只成功前 3/50，剩下 47 个每个空等 PAGE_WAIT，
    # 白烧 ~40 分钟（接近 90 分钟 job timeout）。此时直接早退并保留上一次结果。
    early_abort_n = int(os.environ.get("AUTHORS_EARLY_ABORT", "10"))
    streak_fail = 0
    try:
        for i, it in enumerate(items, 1):
            title = it["title"]
            diag = ""
            try:
                if is_douyin:
                    got, diag = collect_douyin(cdp, it)
                else:
                    url = norm_url(it)
                    cdp.cmd("Page.navigate", {"url": url})
                    got = None
                    deadline = time.time() + PAGE_WAIT
                    while time.time() < deadline:
                        time.sleep(1.0)
                        raw = cdp.evaluate(EXTRACT_JS)
                        if not raw:
                            continue
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        if data.get("authors"):
                            # 作者 DOM 出现得比帖子正文早：再等一会让正文加载完，
                            # 否则正文词库（《作品名》/高频话题）会挖不全
                            time.sleep(1.5)
                            raw2 = cdp.evaluate(EXTRACT_JS)
                            try:
                                d2 = json.loads(raw2)
                                if d2.get("authors"):
                                    data = d2
                            except Exception:
                                pass
                            got = data
                            break
                if got:
                    if is_douyin:
                        # 抖音作者已带 identity，无正文词库可挖
                        authors = got["authors"][:TOP_N]
                        lex = prev_lex.get(title, [])
                        kw_freq_list = []
                    else:
                        authors = [enrich(a) for a in got["authors"][:TOP_N]]
                        lex = build_lexicon(got.get("book"), got.get("hash"),
                                            got.get("kw_freq"), title)
                        if not lex:
                            lex = prev_lex.get(title, [])   # 本轮没挖到 → 沿用上一轮，避免闪断
                        kw_freq_list = top_kw_freq(got.get("kw_freq"), title)
                    topics[title] = {"stats": got.get("stats", {}),
                                     "authors": authors, "lexicon": lex,
                                     "kw_freq": kw_freq_list}
                    ok_cnt += 1
                    streak_fail = 0
                    if lex:
                        lex_cnt += 1
                    print(f"  [{i:>2}/{len(items)}] ✓ {title[:26]} 作者 {len(authors)} "
                          f"词库 {len(lex)} (阅读 {topics[title]['stats'].get('read','?')})")
                else:
                    topics[title] = {"stats": {}, "authors": [],
                                     "lexicon": prev_lex.get(title, [])}
                    streak_fail += 1
                    print(f"  [{i:>2}/{len(items)}] ✗ {title[:26]} 未渲染出作者"
                          + (f"\n        ↳ {diag}" if diag else ""))
                    # 连续失败即判环境异常：抖音「合法没有精选位」的话题是**散落分布**的
                    # （实测 13/48，前后都有成功项），连续这么多个几乎不可能是内容原因。
                    # 旧条件还附带 ok_cnt==0，导致「跑一半才被风控」时（本轮 11 成功 → 34 连败）
                    # 完全不触发，白烧 34×21s ≈ 12 分钟。
                    if streak_fail >= early_abort_n:
                        print(f"[authors] 连续 {streak_fail} 个话题未渲染出作者"
                              f"（本轮已成功 {ok_cnt} 个）→ 判定为风控/环境异常 → "
                              f"提前终止，保留上一次结果", file=sys.stderr)
                        break
            except Exception as e:
                print(f"  [{i:>2}/{len(items)}] ! {title[:26]} {type(e).__name__}: {e}")
                topics[title] = {"stats": {}, "authors": [],
                                 "lexicon": prev_lex.get(title, [])}
                streak_fail += 1
                if ok_cnt == 0 and streak_fail >= early_abort_n:
                    print(f"[authors] 连续 {streak_fail} 个话题均异常且 0 成功 → 提前终止",
                          file=sys.stderr)
                    break
    finally:
        cdp.close()
        proc.terminate()

    # 整体成功率过低（多为风控/网络问题）时保留上一次结果，避免把线上数据冲稀
    # —— 仅 ok_cnt==0 保护不住「部分成功但大量失败」的轮次
    if ok_cnt <= len(items) * 0.2 and os.path.exists(out_path):
        print(f"[authors] 本轮仅成功 {ok_cnt}/{len(items)}（成功率过低），保留上一次结果不覆盖", file=sys.stderr)
        sys.exit(0)

    out = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
        "source": d.get("source", source),
        "top_n": TOP_N,
        "topic_count": len(topics),
        "ok_count": ok_cnt,
        "lexicon_count": lex_cnt,
        "topics": topics,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # 原子写入：先序列化并校验为合法 JSON，再写临时文件 + os.replace。
    # 避免进程被中断时把半截 JSON 提交上线（2026-09-12 线上因此损坏一次）
    blob = json.dumps(out, ensure_ascii=False, indent=1)
    # 清洗孤立 surrogate（CDP 传来的 emoji 会被 JSON 拆成 \ud83d\ude00 这类半对，
    # ensure_ascii=False 写 UTF-8 时会抛 UnicodeEncodeError 导致整轮白跑）
    blob = blob.encode("utf-8", "replace").decode("utf-8")
    json.loads(blob)  # 防御性校验：写盘前确保序列化结果可解析
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(blob)
    os.replace(tmp, out_path)
    print(f"[authors] 完成 {ok_cnt}/{len(items)}，其中 {lex_cnt} 个话题挖到正文词库，"
          f"耗时 {time.time()-t0:.0f}s → {out_path}")


if __name__ == "__main__":
    main()
