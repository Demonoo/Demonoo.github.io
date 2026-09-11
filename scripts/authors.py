# -*- coding: utf-8 -*-
"""
话题参与作者采集（微博话题聚合页）

背景：
- 微博话题聚合页的作者数据只能通过真实浏览器拿到（直连 API 返回 ok=-100、
  HTML 302，都会跳登录页；浏览器执行完 JS 访客流程后内容才渲染出来）。
- 本脚本用 headless Chrome + CDP 顺序渲染每个话题页，提取：
    · 话题统计：阅读量 / 讨论量 / 主持人 / 媒体发布数
    · 前 N 位热门作者：昵称、认证等级、认证说明、身份类型、互动量、**正文文案**
    · 正文实体词库：从帖子正文挖「《作品名》」与高频「#话题#」
      —— 热搜词条常把剧名切坏（早春晴朗云合超藏海传 → 超藏/海传），
         而帖子正文里《藏海传》会完整高频出现，可反向补回实体，交给 analyze.py 做区间保护
- 输出 data/authors.json（话题性质由 analyze.py 的大模型判定，此处不重复推断）

微博认证图标对照（实测 2026-09）：
    i.m-icon-goldv   → 金V（优质创作者）
    i.m-icon-redv    → 红V（个人认证）
    i.m-icon-orangev → 橙V（早期个人认证，存量）
    i.m-icon-bluev   → 蓝V（机构 / 媒体 / 政务 / 企业）
    img.vipicon      → 微博会员（SVIP），与认证无关，不能当认证用

依赖：websocket-client
"""
import os, re, sys, json, time, subprocess, tempfile, urllib.request, urllib.parse, shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOTSPOTS = os.path.join(BASE, "data", "hotspots.json")
RAW = os.path.join(BASE, "data", "raw_hotspots.json")
OUT = os.path.join(BASE, "data", "authors.json")

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


def launch_chrome(port, profile):
    exe = find_chrome()
    if not exe:
        raise RuntimeError("未找到 Chrome/Chromium，无法渲染话题页")
    args = [exe, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
            "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
            "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
            "--no-proxy-server", "--proxy-bypass-list=*",
            "--window-size=430,900", "about:blank"]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class CDP:
    def __init__(self, ws_url, timeout=45):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._id = 0

    def cmd(self, method, params=None):
        self._id += 1
        i = self._id
        self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        while True:
            try:
                r = json.loads(self.ws.recv())
            except Exception:
                return None
            if r.get("id") == i:
                return r

    def evaluate(self, expr):
        r = self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        try:
            return r["result"]["result"].get("value")
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

  var cards = document.querySelectorAll('.card.weibo-member');
  var authors = [], seen = {};
  for (var i=0;i<cards.length;i++){
    var c = cards[i];
    var name = txt(c.querySelector('h3')).replace(/[\s\u200b]+/g,'');
    if (!name || seen[name]) continue;      // 同一作者只留首条（页面已按热度排序）
    seen[name] = 1;

    var raw = txt(c.querySelector('.from'));
    if (/^来自/.test(raw)) raw = '';         // 「来自 iPhone Air」是发布来源，非认证说明
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
      identity_raw: raw.slice(0,24),
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
    """把词条转成 m.weibo.cn containerid 话题页"""
    u = (it.get("url") or "").strip()
    if "m.weibo.cn/search?containerid=" in u:
        return u
    q = ""
    if "s.weibo.com/weibo?q=" in u:
        q = urllib.parse.unquote(u.split("q=", 1)[1].split("&")[0])
    if not q:
        q = "#" + it.get("title", "") + "#"
    return "https://m.weibo.cn/search?containerid=100103type%3D1%26q%3D" + urllib.parse.quote(q)


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


def main():
    src = HOTSPOTS if os.path.exists(HOTSPOTS) else RAW
    with open(src, encoding="utf-8") as f:
        d = json.load(f)
    items = d.get("items") or [{"title": t} for t in (d.get("titles") or [])]
    items = [it for it in items if it.get("title")]
    if LIMIT > 0:
        items = items[:LIMIT]
    print(f"[authors] 话题数 {len(items)}（源 {os.path.basename(src)}），每话题取前 {TOP_N} 位作者")

    profile = tempfile.mkdtemp(prefix="chrome-authors-")
    proc = launch_chrome(PORT, profile)
    prev_lex = load_prev_lexicon(OUT)

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

    topics = {}
    ok_cnt = 0
    lex_cnt = 0
    t0 = time.time()
    try:
        for i, it in enumerate(items, 1):
            title = it["title"]
            url = norm_url(it)
            try:
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
                    authors = [enrich(a) for a in got["authors"][:TOP_N]]
                    lex = build_lexicon(got.get("book"), got.get("hash"), got.get("kw_freq"), title)
                    if not lex:
                        lex = prev_lex.get(title, [])      # 本轮没挖到 → 沿用上一轮，避免闪断
                    kw_freq_list = top_kw_freq(got.get("kw_freq"), title)
                    topics[title] = {"stats": got.get("stats", {}),
                                     "authors": authors, "lexicon": lex,
                                     "kw_freq": kw_freq_list}
                    ok_cnt += 1
                    if lex:
                        lex_cnt += 1
                    print(f"  [{i:>2}/{len(items)}] ✓ {title[:26]} 作者 {len(authors)} "
                          f"词库 {len(lex)} (阅读 {topics[title]['stats'].get('read','?')})")
                else:
                    topics[title] = {"stats": {}, "authors": [],
                                     "lexicon": prev_lex.get(title, [])}
                    print(f"  [{i:>2}/{len(items)}] ✗ {title[:26]} 未渲染出作者")
            except Exception as e:
                print(f"  [{i:>2}/{len(items)}] ! {title[:26]} {type(e).__name__}: {e}")
                topics[title] = {"stats": {}, "authors": [],
                                 "lexicon": prev_lex.get(title, [])}
    finally:
        cdp.close()
        proc.terminate()

    # 整体成功率过低（多为风控/网络问题）时保留上一次结果，避免把线上数据冲稀
    # —— 仅 ok_cnt==0 保护不住「部分成功但大量失败」的轮次
    if ok_cnt <= len(items) * 0.2 and os.path.exists(OUT):
        print(f"[authors] 本轮仅成功 {ok_cnt}/{len(items)}（成功率过低），保留上一次结果不覆盖", file=sys.stderr)
        sys.exit(0)

    out = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
        "source": d.get("source", "微博"),
        "top_n": TOP_N,
        "topic_count": len(topics),
        "ok_count": ok_cnt,
        "lexicon_count": lex_cnt,
        "topics": topics,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # 原子写入：先序列化并校验为合法 JSON，再写临时文件 + os.replace。
    # 避免进程被中断时把半截 JSON 提交上线（2026-09-12 线上因此损坏一次）
    blob = json.dumps(out, ensure_ascii=False, indent=1)
    # 清洗孤立 surrogate（CDP 传来的 emoji 会被 JSON 拆成 \ud83d\ude00 这类半对，
    # ensure_ascii=False 写 UTF-8 时会抛 UnicodeEncodeError 导致整轮白跑）
    blob = blob.encode("utf-8", "replace").decode("utf-8")
    json.loads(blob)  # 防御性校验：写盘前确保序列化结果可解析
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(blob)
    os.replace(tmp, OUT)
    print(f"[authors] 完成 {ok_cnt}/{len(items)}，其中 {lex_cnt} 个话题挖到正文词库，"
          f"耗时 {time.time()-t0:.0f}s → {OUT}")


if __name__ == "__main__":
    main()
