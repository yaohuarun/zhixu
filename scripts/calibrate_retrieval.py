"""Calibrate a sample-specific rerank cutoff and compare labeled retrieval branches."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--output", default="data/retrieval-calibration.json")
    args = parser.parse_args()
    data = json.loads(Path(args.report).read_text(encoding="utf-8"))
    rows = data["questions"]
    assert rows and all(not r["degraded"] for r in rows), "Cannot calibrate a degraded retrieval run"
    positives = [r for r in rows if r["relevant"]]
    negatives = [r for r in rows if not r["relevant"]]
    assert positives and negatives
    metrics = {}
    for branch in ("lexical", "vector", "fused", "reranked"):
        top1 = top3 = 0
        for row in positives:
            names = [row["candidate_names"][identity] for identity, _ in row["branches"][branch]]
            top1 += bool(names and names[0] == row["relevant_document"])
            top3 += row["relevant_document"] in names[:3]
        metrics[branch] = {"top1_hits": top1, "top3_hits": top3, "positive_queries": len(positives)}
    positive_scores = [max(score for identity, score in r["branches"]["reranked"]
                           if r["candidate_names"][identity] == r["relevant_document"]) for r in positives]
    negative_scores = [max((score for _, score in r["branches"]["reranked"]), default=0) for r in negatives]
    lower, upper = max(negative_scores), min(positive_scores)
    threshold = round((lower + upper) / 2, 4) if lower < upper else None
    result = {"status": "calibrated" if threshold is not None else "no_separating_threshold",
              "scope": "small labeled RAG guide corpus only; not a general production threshold",
              "threshold": threshold, "max_negative_score": lower, "min_relevant_score": upper,
              "branch_comparison": metrics, "models": data["models"], "parameters": data["parameters"]}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
