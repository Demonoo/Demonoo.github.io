# -*- coding: utf-8 -*-
"""
热点词条情绪三分类（云端下载 + 本地推理，不消耗 GLM 额度）

模型：senlou/weibo-sentiment-chinese-bert
      基于 hfl/chinese-bert-wwm-ext 在 10 万条微博情感数据上微调的三分类模型
      id2label = {0: negative(负面), 1: positive(正面), 2: neutral(中性)}
      测试集准确率 87.79%，Macro F1 0.878，Apache-2.0

下载源：hf-mirror.com（国内可直连，规避 HuggingFace 原站不可达）
      —— 仅通过 HF_ENDPOINT 环境变量切换，不写死 huggingface.co
缓存：models/hf-cache/（已 gitignore），由 GitHub Actions 的 actions/cache 持久化，
      缓存命中时不会重复下载那 390MB 权重
      —— 用 snapshot_download(local_dir=...) 落真实文件，不建符号链接，
         Windows 本地与 Linux CI 行为一致

输入 data/raw_hotspots.json → 输出 data/sentiment.json
"""
import os
import sys
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 必须在 import transformers / huggingface_hub 之前设置镜像端点，否则不生效
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

MODEL_ID = os.environ.get("SENTIMENT_MODEL", "senlou/weibo-sentiment-chinese-bert")
CACHE_DIR = os.path.join(BASE, "models", "hf-cache")
RAW_PATH = os.path.join(BASE, "data", "raw_hotspots.json")
OUT_PATH = os.path.join(BASE, "data", "sentiment.json")

MAX_LEN = 128
BATCH = 32

# 只取推理必需的几类文件，避免以后上游多传 .bin 导致体积翻倍
ALLOW_PATTERNS = ["*.json", "*.txt", "*.safetensors"]

# 模型输出（英文）→ 站点使用的三分类中文标签
LABEL_MAP = {"negative": "负面", "positive": "正面", "neutral": "中性"}


def resolve_model():
    """下载 / 复用缓存的模型目录，返回本地路径"""
    from huggingface_hub import snapshot_download

    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
    print(f"[INFO] 模型 {MODEL_ID}，下载源 {endpoint}，缓存 {CACHE_DIR}")
    os.makedirs(CACHE_DIR, exist_ok=True)
    md = snapshot_download(
        MODEL_ID,
        local_dir=CACHE_DIR,
        allow_patterns=ALLOW_PATTERNS,
    )
    print(f"[OK] 模型就绪：{md}")
    return md


def main():
    import torch
    from transformers import AutoTokenizer, AutoModelForSequenceClassification

    with open(RAW_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    titles = [t for t in raw.get("titles", []) if t and t.strip()]
    if not titles:
        print("raw_hotspots.json 中没有 titles", file=sys.stderr)
        sys.exit(1)

    md = resolve_model()
    tok = AutoTokenizer.from_pretrained(md)
    model = AutoModelForSequenceClassification.from_pretrained(md)
    model.eval()
    id2label = model.config.id2label

    results = []
    for i in range(0, len(titles), BATCH):
        chunk = titles[i:i + BATCH]
        enc = tok(chunk, padding=True, truncation=True,
                  max_length=MAX_LEN, return_tensors="pt")
        with torch.no_grad():
            logits = model(**enc).logits
        probs = torch.softmax(logits, dim=-1)
        conf, pred = probs.max(dim=-1)
        for text, p, c in zip(chunk, pred.tolist(), conf.tolist()):
            # transformers 载入后 id2label 的 key 可能是 int 也可能是 str
            en = id2label.get(p) or id2label.get(str(p)) or "neutral"
            results.append({
                "title": text,
                "情感倾向": LABEL_MAP.get(en, "中性"),
                "confidence": round(float(c), 3),
            })

    from collections import Counter
    c = Counter(r["情感倾向"] for r in results)
    out = {"results": results, "分布": dict(c)}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"情感分析完成：{len(results)} 条，分布 {dict(c)}")
    for r in results[:8]:
        print(f"  {r['情感倾向']} ({r['confidence']:.2f})  {r['title']}")


if __name__ == "__main__":
    main()
