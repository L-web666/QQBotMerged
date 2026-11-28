# -*- coding: utf-8 -*-
"""训练「这条消息该不该回」的判断器。

**思路**（为什么这么做）：
  · 不要微调 bge —— 你只需要一个「向量 → 是/否」的分类器，
    用逻辑回归在这 512 维向量上训，几秒完成，CPU 就行，GTX 1050 也够；
  · bge 保持**冻结**（不训练它），这样通用语义能力全保留；
  · 产物只有一个很小的 `.pkl`（几十 KB）+ 一份配置，便于公开分发。

**产出的两个文件**：
  gate.pkl        —— 逻辑回归权重（不含任何原文）
  gate.json       —— 基座模型名、维度、阈值、指标、预处理规则

**重要：训练数据必须**先**脱敏**（用 tools/anonymize.py），
拿到 <USER_1> / <PATH> 这种占位符之后再喂进来。
否则模型会学到"锋跃"这类专有名词的语义，虽然权重里看不出原文，
但别人拿同一个名字去试探时能测出来（成员推断）。

用法：

    # 1. 先脱敏
    python tools/anonymize.py raw.jsonl -o clean.jsonl

    # 2. 人工标注 should_reply（true / false）

    # 3. 训练
    python tools/train_gate.py clean.jsonl -o gate

    # 4. 看看结果
    python tools/train_gate.py clean.jsonl --dry-run
"""

import argparse
import json
import os
import sys

# 默认基座：bge-base-zh-v1.5（102M 参数 / 409 MB / 768 维）
#
# 为什么用 base 而不是 small（24M / 96 MB / 512 维）：
#   base 在中文语义上更准，判断"这句话该不该接"这种需要读懂语境的任务，
#   向量质量直接决定上限 —— 逻辑回归只是接在后面的薄薄一层，基座弱了它救不回来。
#
# 代价（要心里有数）：
#   · 模型大 4 倍，首次下载 ~400 MB（国内要配 HF_ENDPOINT=https://hf-mirror.com）；
#   · 每次判断多花一点时间（CPU 上 base 大约是 small 的 2.5 倍）；
#   · 内存占用 ~500 MB（懒加载，没开回复门卫不会占）。
#   这些在"每条群消息多一次判断"的场景下可以接受。
#
# 想换回轻量的：--base-model BAAI/bge-small-zh-v1.5
# **但训练与推理必须用同一个基座**，否则向量维度对不上（768 vs 512），模型直接不可用。
DEFAULT_BASE_MODEL = "BAAI/bge-base-zh-v1.5"
MIN_SAMPLES = 30          # 少于这个数不建议训（会过拟合）


# ---------------------------------------------------------------- 读数据
def load_samples(path):
    """读 JSONL；只保留标了 should_reply 的。"""
    rows = []
    skipped = {"not_json": 0, "no_label": 0, "empty": 0}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                skipped["not_json"] += 1
                continue
            label = item.get("should_reply")
            if label is None:
                skipped["no_label"] += 1
                continue
            text = build_text(item)
            if not text:
                skipped["empty"] += 1
                continue
            rows.append({"text": text, "label": 1 if label else 0})
    return rows, skipped


def build_text(item):
    """把「上下文 + 当前消息」拼成一段给 bge 编码的文本。

    **为什么要拼上下文**：单看「怎么办」「对」「卡住」根本判不了，
    必须把前面几句一起给模型。上下文用换行分隔，让 bge 能看到整体语境。
    """
    parts = []
    for ctx in (item.get("context") or []):
        if isinstance(ctx, str) and ctx.strip():
            parts.append(ctx.strip())
    message = item.get("message") or ""
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())
    return "\n".join(parts)


# ---------------------------------------------------------------- 编码
def encode_texts(texts, model_name, progress=None):
    """用 bge 把文本编码成向量。返回 numpy 数组。"""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("缺少 sentence-transformers。请先装：", file=sys.stderr)
        print("    pip install sentence-transformers", file=sys.stderr)
        print("（它会把 torch 一起装，CPU 版约 200MB）", file=sys.stderr)
        raise SystemExit(2)

    if progress:
        progress("加载基座模型：%s" % model_name)
    model = SentenceTransformer(model_name)

    if progress:
        progress("编码 %d 条样本 …" % len(texts))
    vectors = model.encode(texts, normalize_embeddings=True,
                           show_progress_bar=False, batch_size=32)
    return vectors


# ---------------------------------------------------------------- 训练
def train(vectors, labels, test_size=0.25, seed=42):
    """训逻辑回归并做交叉验证 + 留出集评估。"""
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score, train_test_split
        from sklearn.metrics import (accuracy_score, precision_score,
                                     recall_score, f1_score, confusion_matrix)
    except ImportError:
        print("缺少 scikit-learn / numpy。请先装：", file=sys.stderr)
        print("    pip install scikit-learn numpy", file=sys.stderr)
        raise SystemExit(2)

    X = np.asarray(vectors)
    y = np.asarray(labels)

    n_pos = int(y.sum())
    n_neg = len(y) - n_pos

    # 样本太少时不做交叉验证（意义不大）
    clf = LogisticRegression(max_iter=2000, class_weight="balanced")
    clf.fit(X, y)

    result = {
        "samples": len(y),
        "positive": n_pos,
        "negative": n_neg,
    }

    # ---- 交叉验证（反映真实水平，比训练集准确率高）
    if len(y) >= MIN_SAMPLES and n_pos >= 5 and n_neg >= 5:
        folds = min(5, min(n_pos, n_neg))
        try:
            scores = cross_val_score(clf, X, y, cv=folds, scoring="accuracy")
            result["cv_folds"] = folds
            result["cv_accuracy"] = float(scores.mean())
            result["cv_std"] = float(scores.std())
        except Exception as exc:
            result["cv_error"] = str(exc)

    # ---- 留出集（真正没见过的数据）
    if len(y) >= MIN_SAMPLES and n_pos >= 3 and n_neg >= 3:
        try:
            X_tr, X_te, y_tr, y_te = train_test_split(
                X, y, test_size=test_size, random_state=seed, stratify=y)
            probe = LogisticRegression(max_iter=2000, class_weight="balanced")
            probe.fit(X_tr, y_tr)
            pred = probe.predict(X_te)
            result["holdout"] = {
                "test_samples": int(len(y_te)),
                "accuracy": float(accuracy_score(y_te, pred)),
                "precision": float(precision_score(y_te, pred, zero_division=0)),
                "recall": float(recall_score(y_te, pred, zero_division=0)),
                "f1": float(f1_score(y_te, pred, zero_division=0)),
                "confusion": confusion_matrix(y_te, pred).tolist(),
            }
        except Exception as exc:
            result["holdout_error"] = str(exc)

    return clf, result


# ---------------------------------------------------------------- 评估报告
def print_report(result, save_path=""):
    print()
    print("=" * 70)
    print("  训练结果")
    print("=" * 70)
    print("  样本总数    %d" % result["samples"])
    print("  该回的      %d" % result["positive"])
    print("  不该回的    %d" % result["negative"])

    if result["samples"] < MIN_SAMPLES:
        print()
        print("  ⚠️ 样本量偏少（建议 %d 条以上）。这次训出来的模型只能当" % MIN_SAMPLES)
        print("     流程验证用，别指望准确率。")

    if "cv_accuracy" in result:
        print()
        print("  交叉验证准确率  %.1f%%  (±%.1f%%，%d 折)"
              % (result["cv_accuracy"] * 100, result["cv_std"] * 100,
                 result["cv_folds"]))
        if result["cv_accuracy"] < 0.75:
            print("     ⚠️ 偏低。多半是数据问题，不是模型问题：")
            print("        · 标注前后不一致（同样的句子标了不同结果）")
            print("        · 样本太单一")
            print("        · 正负样本严重不平衡")

    if "holdout" in result:
        h = result["holdout"]
        print()
        print("  留出集（%d 条没参与训练）" % h["test_samples"])
        print("    准确率 %.1f%%   精确率 %.1f%%   召回率 %.1f%%   F1 %.1f%%"
              % (h["accuracy"] * 100, h["precision"] * 100,
                 h["recall"] * 100, h["f1"] * 100))
        try:
            tn, fp, fn, tp = h["confusion"][0][0], h["confusion"][0][1], \
                             h["confusion"][1][0], h["confusion"][1][1]
            print("    混淆矩阵：真实要回 %d（判对 %d）· 真实不回 %d（判对 %d）"
                  % (tp + fn, tp, tn + fp, tn))
            print("    误判：该回没回 %d 次 · 不该回却回了 %d 次" % (fn, fp))
            if fn > fp:
                print("    ⚠️ 「该回没回」更多 —— 实际上线会把机器人弄得像坏了，")
                print("       建议降低阈值（把 threshold 调小一点）")
        except Exception:
            pass
    for key in ("cv_error", "holdout_error"):
        if key in result:
            print()
            print("  %s: %s" % (key, result[key]))

    if save_path:
        print()
        print("  已保存：")
        print("    %s.pkl   模型权重（不含原文）" % save_path)
        print("    %s.json  配置与指标（可公开）" % save_path)


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="train_gate",
        description="训练「这条消息该不该回」的判断器（bge 冻结 + 逻辑回归）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools/train_gate.py clean.jsonl -o gate\n"
            "  python tools/train_gate.py clean.jsonl --dry-run\n"
            "\n"
            "**训练数据必须先脱敏**（tools/anonymize.py），否则模型会学到真实昵称。\n"
        ))
    parser.add_argument("input", help="脱敏并标注好的 JSONL")
    parser.add_argument("-o", "--output", default="gate",
                        help="输出前缀（会生成 <前缀>.pkl 和 <前缀>.json）")
    parser.add_argument("--base-model", default=DEFAULT_BASE_MODEL,
                        help="基座向量模型（默认 %s）" % DEFAULT_BASE_MODEL)
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计样本，不训练（检查数据质量用）")
    parser.add_argument("--allow-few", action="store_true",
                        help="样本少于 %d 条时也照样训练" % MIN_SAMPLES)
    args = parser.parse_args(argv)

    if not os.path.isfile(args.input):
        print("找不到输入文件：%s" % args.input, file=sys.stderr)
        return 2

    rows, skipped = load_samples(args.input)
    print("=" * 70)
    print("  读取样本：%s" % args.input)
    print("=" * 70)
    print("  可用样本：%d 条" % len(rows))
    if skipped["no_label"]:
        print("  未标注：  %d 条（should_reply 是 null，跳过）" % skipped["no_label"])
    if skipped["not_json"]:
        print("  格式错误：%d 行" % skipped["not_json"])
    if skipped["empty"]:
        print("  空内容：  %d 条" % skipped["empty"])

    if not rows:
        print()
        print("没有可用样本。每条记录需要有 should_reply: true/false。")
        return 1

    n_pos = sum(1 for r in rows if r["label"] == 1)
    n_neg = len(rows) - n_pos
    print()
    print("  该回：  %d 条" % n_pos)
    print("  不该回：%d 条" % n_neg)
    if n_pos == 0 or n_neg == 0:
        print()
        print("只有一种标签，没法训练。需要同时有 true 和 false 的样本。")
        return 1

    if args.dry_run:
        print()
        print("  （--dry-run：只统计，不训练）")
        print()
        print("  样本示例：")
        for row in rows[:3]:
            print("    [%s] %s" % ("该回" if row["label"] else "不回",
                                   row["text"].replace("\n", " ⏎ ")[:70]))
        return 0

    if len(rows) < MIN_SAMPLES and not args.allow_few:
        print()
        print("样本只有 %d 条，少于建议的 %d 条。" % (len(rows), MIN_SAMPLES))
        print("先用 --dry-run 检查数据，攒够了再训。")
        print("确实要现在训（只验证流程）：加 --allow-few")
        return 1

    def progress(msg):
        print("  · %s" % msg)

    texts = [r["text"] for r in rows]
    labels = [r["label"] for r in rows]

    print()
    vectors = encode_texts(texts, args.base_model, progress=progress)

    print()
    print("  训练分类器 …")
    clf, result = train(vectors, labels)

    # ---- 保存
    try:
        import pickle
    except ImportError:
        print("缺少 pickle（标准库，不该发生）", file=sys.stderr)
        return 2

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    pkl_path = args.output + ".pkl"
    json_path = args.output + ".json"

    with open(pkl_path, "wb") as fh:
        pickle.dump(clf, fh)

    config = {
        "format_version": 1,
        "task": "should_reply_gate",
        "base_model": args.base_model,
        "dim": int(vectors.shape[1]),
        "normalize_embeddings": True,
        "threshold": 0.5,
        "how_to_build_text": (
            "把 context 里的每条 + message 用换行拼起来，"
            "再用 base_model 编码（normalize_embeddings=True）"
        ),
        "metrics": result,
        "trained_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "note": "模型权重不含任何原文；训练数据未随模型分发。",
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)

    print_report(result, save_path=args.output)
    print()
    print("  **发布前提醒**：只发 %s.pkl 和 %s.json，"
          % (os.path.basename(args.output), os.path.basename(args.output)))
    print("     不要把训练用的 jsonl 一起发出去。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
