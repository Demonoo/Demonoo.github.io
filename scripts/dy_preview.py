# -*- coding: utf-8 -*-
"""抖音热榜 · 本地抓取测试预览页生成器

直接复用 fetch.fetch_douyin() 的正式抓取逻辑（不另起一套请求），产出一张
自包含的 HTML：品牌字体内联，无外部依赖，离线可打开。

采集内容只有文本字段（词条 / 热度 / 徽标 / 计数 / 链接），不涉及任何图片。

用法：
  python scripts/dy_preview.py
    → .workbuddy/preview/douyin-hotlist.html

设计语言与 trends.html 保持一致（纯白纸面 / 发丝线 / 无圆角无阴影 / gold 唯一强调色）。
"""
import os
import sys
import time
import base64
import html as _html

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import fetch_douyin, DOUYIN_HOTLIST  # noqa: E402

OUT_DIR = os.path.join(BASE, ".workbuddy", "preview")
OUT_PATH = os.path.join(OUT_DIR, "douyin-hotlist.html")
FONT_PATH = os.path.join(BASE, "fonts", "Marcellus-Regular.ttf")

esc = _html.escape

# 徽标配色：与 trends.html 的 LAB_CLS 保持同一套语义（未登记 → 默认 --v-orange）
# 目的：这条预览就是 trends.html 抖音页效果的预演，配色不能各说各话
LABEL_TONE = {
    "爆": "hot", "沸": "hot", "首发": "hot", "独家": "hot", "热议": "hot",
    "新": "new", "挑战": "new", "辟谣": "new",
    "热": "",  # 站点里「热」走 .tm-label 默认色，这里同样不落类
}


def font_css():
    """内联品牌字体，让预览页不依赖相对路径（预览页在 .workbuddy/ 下，非站点目录）"""
    try:
        with open(FONT_PATH, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return ("@font-face{font-family:'Marcellus';"
                "src:url(data:font/ttf;base64," + b64 + ") format('truetype');"
                "font-weight:400;font-style:normal;font-display:swap;}")
    except Exception:
        return ""


def fmt_wan(v):
    """热度值 → 中文「万」单位（抖音 App 的口径）"""
    return f"{v / 10000:.1f}万"


def build_rows(items):
    top = max((it["hot"] for it in items), default=1)
    out = []
    for i, it in enumerate(items, 1):
        tone = LABEL_TONE.get(it.get("label_text") or "", "")
        badge = ""
        if it.get("label_text"):
            cls = f"badge b-{tone}" if tone else "badge"
            badge = f'<span class="{cls}">{esc(it["label_text"])}</span>'
        bar = max(2.0, round(it["hot"] / top * 100, 2))
        disc = it.get("discuss_count") or 0
        disc_html = f'<span class="meta-item">讨论 {disc}</span>' if disc else ""
        out.append(f"""      <li class="row" data-badge="{1 if it.get('label_text') else 0}" data-k="{esc(it['title'].lower())}">
        <div class="rank">{i:02d}</div>
        <div class="body">
          <a class="title" href="{esc(it['url'])}" target="_blank" rel="noopener">{esc(it['title'])}</a>
          <div class="tags">
            {badge}
            <span class="meta-item">视频 {it.get('video_count') or 0}</span>
            {disc_html}
            <span class="meta-item gid">gid {esc(str(it.get('gid') or '—'))}</span>
          </div>
        </div>
        <div class="heat">
          <div class="heat-num">{it['hot']:,}</div>
          <div class="heat-wan">{fmt_wan(it['hot'])}</div>
          <div class="heat-bar"><i style="width:{bar}%"></i></div>
        </div>
      </li>""")
    return "\n".join(out)


def build_dist(items):
    from collections import Counter
    c = Counter(it.get("label_text") or "无徽标" for it in items)
    order = ["热", "新", "首发", "独家", "热议", "辟谣", "挑战", "无徽标"]
    cells = []
    for k in order:
        if c.get(k):
            cells.append(f'<span class="dist-item"><b>{k}</b>{c[k]}</span>')
    return "".join(cells)


def main():
    t0 = time.time()
    items = fetch_douyin()
    if not items:
        print("[FAIL] 抖音返回 0 条", file=sys.stderr)
        sys.exit(1)
    elapsed = time.time() - t0
    raw_n = len(items)
    items = items[:50]
    print(f"[OK] 抓到 {len(items)} 条，耗时 {elapsed:.2f}s")

    hot_max = max(it["hot"] for it in items)
    hot_min = min(it["hot"] for it in items)
    badge_n = sum(1 for it in items if it.get("label_text"))
    video_n = sum(it.get("video_count") or 0 for it in items)
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    html = TEMPLATE
    for k, v in {
        "/*FONT*/": font_css(),
        "/*ROWS*/": build_rows(items),
        "__DIST__": build_dist(items),
        "__NOW__": now,
        "__ELAPSED__": f"{elapsed:.2f}s",
        "__COUNT__": str(len(items)),
        # 接口有时只返回 48 条，那不是被我们截断的 —— 只有超过 50 才标记为截断
        "__TRUNC__": "是" if raw_n > 50 else "否",
        "__RAW_N__": str(raw_n),
        "__BADGE_N__": str(badge_n),
        "__VIDEO_N__": f"{video_n:,}",
        "__HOT_MAX__": f"{hot_max:,}",
        "__HOT_MIN__": f"{hot_min:,}",
        "__HOT_MAX_W__": fmt_wan(hot_max),
        "__HOT_MIN_W__": fmt_wan(hot_min),
        "__ENDPOINT__": DOUYIN_HOTLIST,
    }.items():
        html = html.replace(k, v)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    size = os.path.getsize(OUT_PATH) / 1024
    print(f"[OK] 预览已生成 → {OUT_PATH}（{size:.0f} KB，总耗时 {time.time() - t0:.2f}s）")


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>抖音热榜 · 本地抓取测试 · DMEDIA</title>
<style>
/*FONT*/
:root {
  --paper:#ffffff; --paper-2:#faf9f7; --paper-3:#f3f1ec;
  --ink:#121212; --ink-2:#4a4a48; --ink-3:#8b8a86; --ink-4:#b6b4af;
  --rule:#e8e5df; --rule-2:#d5d1c8; --gold:#a1834f;
  --v-red:#9c4a3c; --v-orange:#a85a3c; --v-blue:#4a5f7a;
  --font-display:'Marcellus','Songti SC',Georgia,serif;
  --font:Arial,'Helvetica Neue',Helvetica,'PingFang SC','Microsoft YaHei',sans-serif;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--paper);color:var(--ink);font-family:var(--font);font-size:15.5px;
  line-height:1.75;letter-spacing:.004em;-webkit-font-smoothing:antialiased;}
a{color:inherit;text-decoration:none;}
code{font-family:Consolas,'Courier New',monospace;font-size:11.5px;
  background:var(--paper-3);padding:1px 5px;}
::selection{background:var(--ink);color:var(--paper);}
.wrap{max-width:1280px;margin:0 auto;padding:0 48px;}

/* 通告条 */
.bulletin{background:var(--paper-2);border-bottom:1px solid var(--rule);}
.bulletin .wrap{display:flex;justify-content:space-between;gap:20px;padding:9px 48px;
  font-size:11.5px;letter-spacing:.26em;text-transform:uppercase;color:var(--ink-3);}

/* 刊头 */
.masthead{padding:64px 0 36px;border-bottom:1px solid var(--rule);}
.kicker{font-size:11px;letter-spacing:.34em;text-transform:uppercase;color:var(--gold);margin-bottom:20px;}
h1{font-family:var(--font-display);font-size:76px;line-height:1.04;font-weight:400;}
h1 .sub{display:block;font-family:var(--font);font-size:12px;letter-spacing:.3em;
  text-transform:uppercase;color:var(--ink-3);margin-top:20px;}
.lede{max-width:680px;margin-top:26px;color:var(--ink-2);font-size:15px;}
.meta-grid{display:flex;flex-wrap:wrap;gap:0 56px;margin-top:34px;}
.meta-cell dt{font-size:10.5px;letter-spacing:.24em;text-transform:uppercase;
  color:var(--ink-4);margin-bottom:6px;}
.meta-cell dd{font-size:13.5px;color:var(--ink);}
.meta-cell dd.mono{color:var(--ink-2);}

/* 统计条 */
.stats{max-width:1280px;margin:0 auto;padding:0 48px;display:grid;
  grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--rule);}
.stat{padding:30px 0 30px 32px;border-right:1px solid var(--rule);}
.stat:first-child{padding-left:0;}
.stat:last-child{border-right:0;}
.stat b{display:block;font-family:var(--font-display);font-size:38px;line-height:1;font-weight:400;}
.stat span{display:block;font-size:10.5px;letter-spacing:.24em;text-transform:uppercase;
  color:var(--ink-4);margin-top:12px;}

/* 工具条 */
.toolbar{display:flex;align-items:center;justify-content:space-between;gap:24px;
  padding:22px 0;border-bottom:1px solid var(--rule);position:sticky;top:0;
  background:rgba(255,255,255,.94);backdrop-filter:blur(12px);z-index:20;}
.search{width:280px;border:0;border-bottom:1px solid var(--rule-2);background:none;
  font:inherit;font-size:14px;padding:7px 2px;color:var(--ink);outline:none;}
.search:focus{border-bottom-color:var(--gold);}
.search::placeholder{color:var(--ink-4);}
.tools-right{display:flex;align-items:center;gap:26px;}
.chip{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--ink-3);
  font-family:var(--font);cursor:pointer;padding:0 0 3px;border:0;border-bottom:1px solid transparent;
  background:none;transition:.18s;}
.chip:hover{color:var(--ink);}
.chip.on{color:var(--gold);border-bottom-color:var(--gold);}
.dist{display:flex;gap:18px;flex-wrap:wrap;font-size:11.5px;color:var(--ink-3);}
.dist-item{display:inline-flex;align-items:center;gap:7px;letter-spacing:.06em;}
.dist-item b{font-weight:400;color:var(--ink-2);}

/* 榜单 */
.list{list-style:none;}
.row{display:grid;grid-template-columns:58px 1fr 200px;gap:26px;align-items:center;
  padding:22px 0;border-bottom:1px solid var(--rule);transition:.18s;}
.row:hover{background:var(--paper-2);}
.row:hover .rank{color:var(--gold);}
.row[data-hide="1"]{display:none;}
.rank{font-size:19px;color:var(--ink-4);letter-spacing:.04em;transition:.18s;}
.row:nth-child(1) .rank,.row:nth-child(2) .rank,.row:nth-child(3) .rank{color:var(--gold);}
.body{min-width:0;}
.title{font-size:17px;line-height:1.5;color:var(--ink);}
.title:hover{color:var(--gold);}
.tags{display:flex;flex-wrap:wrap;align-items:center;gap:14px;margin-top:10px;}
.badge{color:var(--v-orange);font-size:11px;letter-spacing:.14em;padding:1.5px 8px;
  border:1px solid currentColor;}
.b-hot{color:var(--v-red);} .b-new{color:var(--v-blue);}
.meta-item{font-size:11.5px;color:var(--ink-3);letter-spacing:.05em;}
.meta-item.gid{color:var(--ink-4);}
.heat{text-align:right;}
.heat-num{font-size:15px;letter-spacing:.02em;}
.heat-wan{font-size:11.5px;color:var(--ink-3);margin-top:3px;}
.heat-bar{height:1px;background:var(--rule);margin-top:9px;position:relative;}
.heat-bar i{position:absolute;left:0;top:-1px;height:3px;background:var(--gold);}
.list-foot{padding:22px 0;font-size:11.5px;color:var(--ink-4);letter-spacing:.06em;}
.empty{padding:60px 0;color:var(--ink-3);font-size:14px;display:none;}

/* 页脚 */
.foot{border-top:1px solid var(--rule);background:var(--paper-2);margin-top:8px;}
.foot .wrap{padding:40px 48px 56px;display:grid;grid-template-columns:1.4fr 1fr;gap:48px;}
.foot h3{font-size:10.5px;letter-spacing:.24em;text-transform:uppercase;color:var(--ink-4);
  font-weight:400;margin-bottom:14px;}
.foot p,.foot li{font-size:12.5px;color:var(--ink-2);line-height:1.85;}
.foot ul{list-style:none;}

@media(max-width:900px){
  .wrap{padding:0 22px;} .bulletin .wrap{padding:9px 22px;}
  h1{font-size:46px;} .stats{grid-template-columns:repeat(2,1fr);padding:0 22px;}
  .stat{border-right:0;border-bottom:1px solid var(--rule);padding-left:0;}
  .row{grid-template-columns:38px 1fr;gap:14px;}
  .heat{grid-column:2;text-align:left;}
  .toolbar{flex-direction:column;align-items:flex-start;gap:14px;}
  .foot .wrap{grid-template-columns:1fr;}
}
</style>
</head>
<body>

<div class="bulletin"><div class="wrap">
  <span>DMEDIA · Social Pulse</span>
  <span>抖音热榜 · 本地抓取测试</span>
</div></div>

<header class="masthead"><div class="wrap">
  <div class="kicker">Douyin Hot Search · Live Fetch</div>
  <h1>抖音热榜<span class="sub">Local fetch test &nbsp;·&nbsp; raw response preview</span></h1>
  <p class="lede">
    本页由 <code>scripts/dy_preview.py</code> 调用 <code>fetch.fetch_douyin()</code> 实时抓取生成 ——
    与线上调度链路走同一个函数、同一个端点，用于在本地核验抖音热榜是否稳定可取、字段是否完整。
    采集只取文本字段（词条 / 热度 / 徽标 / 计数 / 跳转链接），不涉及图片。
  </p>
  <dl class="meta-grid">
    <div class="meta-cell"><dt>Fetched At</dt><dd>__NOW__</dd></div>
    <div class="meta-cell"><dt>Fetch Latency</dt><dd>__ELAPSED__</dd></div>
    <div class="meta-cell"><dt>Items</dt><dd>__COUNT__ 条 · 接口返回 __RAW_N__ 条（截断 __TRUNC__）</dd></div>
    <div class="meta-cell"><dt>With Badge</dt><dd>__BADGE_N__ 条</dd></div>
    <div class="meta-cell"><dt>Endpoint</dt><dd class="mono">__ENDPOINT__</dd></div>
  </dl>
</div></header>

<section class="stats">
  <div class="stat"><b>__COUNT__</b><span>条词条</span></div>
  <div class="stat"><b>__HOT_MAX_W__</b><span>最高热度 · __HOT_MAX__</span></div>
  <div class="stat"><b>__HOT_MIN_W__</b><span>最低热度 · __HOT_MIN__</span></div>
  <div class="stat"><b>__VIDEO_N__</b><span>关联视频总数</span></div>
</section>

<div class="wrap">
  <div class="toolbar">
    <input id="q" class="search" type="search" placeholder="筛选词条…" autocomplete="off" />
    <div class="tools-right">
      <div class="dist">__DIST__</div>
      <button id="onlyBadge" class="chip">仅带徽标</button>
    </div>
  </div>
  <ol class="list" id="list">
/*ROWS*/
  </ol>
  <div class="empty" id="empty">没有匹配的词条。</div>
  <div class="list-foot">共 __COUNT__ 条 · 词条点击跳转抖音搜索聚合页 · 榜单每轮实时变动，数值仅代表本次抓取时刻</div>
</div>

<footer class="foot"><div class="wrap">
  <div>
    <h3>字段说明</h3>
    <ul>
      <li><code>hot</code> 热度值（接口 <code>hot_value</code>，本页同时给出「万」单位换算）</li>
      <li><code>label_text</code> 徽标文案 —— 接口 <code>label</code> 是数字码，需对照 <code>label_url</code> 图片识读</li>
      <li><code>标签</code> 趋势页字段（analyze.py 写入），抖音侧即取自本站的 <code>label_text</code></li>
      <li><code>video_count</code> / <code>discuss_count</code> 关联视频数与讨论数</li>
      <li><code>gid</code> / <code>position</code> / <code>event_time</code> 供 authors.py 拼话题聚合页</li>
      <li><code>url</code> 落地页点击后的同一跳转目标（搜索聚合页）</li>
    </ul>
  </div>
  <div>
    <h3>徽标对照（实测核对）</h3>
    <ul>
      <li>1 = 新 &nbsp;·&nbsp; 3 = 热 &nbsp;·&nbsp; 5 = 首发</li>
      <li>8 = 独家 &nbsp;·&nbsp; 9 = 挑战 &nbsp;·&nbsp; 16 = 辟谣 &nbsp;·&nbsp; 17 = 热议</li>
      <li>0 / 其他 = 无徽标（未知码一律留空，不猜）</li>
      <li>配色与 trends.html 的 <code>LAB_CLS</code> 同源，非抖音官方原色</li>
      <li>抓取路径：<code>python scripts/fetch.py --platform douyin</code></li>
    </ul>
  </div>
</div></footer>

<script>
(function () {
  var q = document.getElementById('q');
  var only = document.getElementById('onlyBadge');
  var rows = Array.prototype.slice.call(document.querySelectorAll('.row'));
  var empty = document.getElementById('empty');
  var flagOnly = false;
  function apply() {
    var kw = q.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (r) {
      var okKw = !kw || r.getAttribute('data-k').indexOf(kw) > -1;
      var okBadge = !flagOnly || r.getAttribute('data-badge') === '1';
      var show = okKw && okBadge;
      r.setAttribute('data-hide', show ? '0' : '1');
      if (show) shown++;
    });
    empty.style.display = shown ? 'none' : 'block';
  }
  q.addEventListener('input', apply);
  only.addEventListener('click', function () {
    flagOnly = !flagOnly;
    only.classList.toggle('on', flagOnly);
    apply();
  });
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
