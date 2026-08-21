# DMEDIA — 量化营销平台

> 媒介即信息，信息创造价值。

一个数据驱动的量化营销工作室展示站。纯静态 HTML，零构建依赖，可直接部署到 GitHub Pages。

## 目录结构

```
dmedia-site/
├── index.html      # 首页（单页）
├── README.md
├── reports/        # 报告页（每篇自包含，可独立分享）
│   ├── 全球社交媒体上的奥德赛时期.html
│   └── 中国电动汽车营销趋势报告.html
├── assets/         # 图片素材
│   ├── 图标.png        # Logo / favicon / og:image
│   ├── 二维码.jpg      # 头部二维码弹层
│   ├── chart1-4.jpg    # EV 报告配图
│   ├── odyssey-preview.jpg / ev-preview.jpg  # 首页报告卡片封面（自动生成）
│   ├── 背景.jpg        # 备用
│   └── 视频号.png      # 备用
└── fonts/          # 自定义字体
    ├── Marcellus-Regular.ttf
    └── Quattrocento-Regular.otf
```

## 首页板块

- **LLM SEARCH** — 基于大语言模型的搜索
- **CONTENT CREATOR** — 内容创作支持
- **MACHINE LEARNING** — 机器学习技术应用
- **KOL VALUE** — 关键意见领袖价值评估

## 技术要点

- **字体**：`Marcellus` 用于 Display（品牌字 / 编号 / 英文标题），`Quattrocento` 用于拉丁正文（中文回退系统字体）；均带 `font-display: swap` 与 `<link rel="preload">`
- **报告预览**：首页卡片使用静态首屏截图（`assets/*-preview.jpg`，由无头 Chrome 生成），替代 iframe 方案——图片仅 ~50-100KB，不加载整份报告；报告大改后用 `.workbuddy/cdp_screenshot.py` 重新生成
- **Ticker**：滚动条内容由 JS 读取 `data-ticker="EN|中文"` 属性生成，修改文案只需改 HTML 属性
- **动效**：滚动 reveal 动画 + ticker 均支持 `prefers-reduced-motion` 降级
- **无障碍/SEO**：favicon、og:image、语义化标签、focus-within 二维码弹层
- **部署友好**：全部使用相对路径（`../assets/`、`reports/`），GitHub Pages 根路径或子路径部署均可用

## 本地预览

```bash
npx http-server -p 8765 -c-1 .
# 或
python -m http.server 8765
```

## 部署到 GitHub Pages

```bash
git init && git add . && git commit -m "feat: dmedia site"
git branch -M main
git remote add origin https://github.com/<your-username>/dmedia.git
git push -u origin main
```

然后在仓库 **Settings → Pages** → Source 选 `main` / `(root)`。

## 自定义

- 颜色 → `:root` 里的 CSS 变量（当前：纯黑 `#000` × 纯白 `#fff`）
- 四大板块内容 → 搜索 `pillar-title`
- Ticker 文案 → 各 `section-ticker` 的 `data-ticker` 属性
- 报告卡片 → 搜索 `hero-report-card`

## 新增一篇报告

1. 把报告 HTML 放入 `reports/`（建议自包含样式，便于独立分享）
2. 用 `.workbuddy/cdp_screenshot.py` 生成首页封面截图到 `assets/<name>-preview.jpg`
3. 在 `index.html` 的 `.hero-reports` 中复制一个 `hero-report-card`，填好 `href` / 图片路径 / 标题与简介

— © 2026 DMEDIA Studio
