#!/usr/bin/env python3
"""按季度 / 年度汇总每日榜单数据。

GitHub 官方没有季度榜、年榜，这里以 data/日期/daily-all.json 为数据源，
累计出每个季度 / 年度内各仓库的上榜天数、累计增星、最佳排名等指标。
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def bucket_keys(date_str):
    """2026-10-22 -> ('2026', '2026-Q4')"""
    year = date_str[:4]
    month = int(date_str[5:7])
    return year, f"{year}-Q{(month - 1) // 3 + 1}"


def label_of(key):
    if "-Q" in key:
        year, q = key.split("-Q")
        return f"{year} 年 Q{q} 季度"
    return f"{key} 年"


def blank_bucket(key):
    return {"key": key, "label": label_of(key), "dates": set(), "repos": {}}


def collect_snapshots(out_root):
    """读取所有日期的 daily-all.json，返回 [(日期, items)]，按日期升序。"""
    snaps = []
    for folder in sorted(out_root.iterdir()):
        if not folder.is_dir() or not DATE_DIR_RE.match(folder.name):
            continue
        path = folder / "daily-all.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] 跳过 {path}: {exc}", file=sys.stderr)
            continue
        snaps.append((folder.name, payload.get("items") or []))
    return snaps


def add_item(bucket, date, it):
    """把一条榜单记录累加进桶里。"""
    bucket["dates"].add(date)
    name = it.get("full_name")
    if not name:
        return
    repo = bucket["repos"].get(name)
    if repo is None:
        repo = {
            "full_name": name,
            "url": it.get("url") or f"https://github.com/{name}",
            "days": 0, "best_rank": None, "gained": 0, "stars": None,
            "language": None, "description": "", "description_zh": "",
            "categories": [], "first_seen": date, "last_seen": date,
            "history": [],
        }
        bucket["repos"][name] = repo

    repo["days"] += 1
    rank = it.get("trend_rank")
    if rank and (repo["best_rank"] is None or rank < repo["best_rank"]):
        repo["best_rank"] = rank
    if it.get("stars_gained"):
        repo["gained"] += it["stars_gained"]
    if it.get("stars") is not None:
        repo["stars"] = it["stars"]
        if not repo["history"] or repo["history"][-1][0] != date:
            repo["history"].append([date, it["stars"]])
    repo["language"] = it.get("language") or repo["language"]
    repo["description"] = it.get("description") or repo["description"]
    repo["description_zh"] = it.get("description_zh") or repo["description_zh"]
    for cat in it.get("categories") or []:
        if cat not in repo["categories"]:
            repo["categories"].append(cat)
    repo["last_seen"] = max(repo["last_seen"], date)


def summarize(bucket):
    repos = list(bucket["repos"].values())
    cat_counts = Counter()
    lang_counts = Counter()
    for repo in repos:
        for cat in repo["categories"]:
            cat_counts[cat] += 1
        if repo["language"]:
            lang_counts[repo["language"]] += 1
    return {
        "days": len(bucket["dates"]),
        "repos": len(repos),
        "gained": sum(r["gained"] for r in repos),
        "categories": dict(cat_counts),
        "top_language": lang_counts.most_common(1)[0][0] if lang_counts else None,
    }


def finalize(buckets):
    """排序并生成可直接序列化的列表，最新的在前。"""
    out = []
    for bucket in buckets.values():
        repos = sorted(
            bucket["repos"].values(),
            key=lambda r: (-r["days"], -(r["gained"] or 0), r["full_name"]),
        )
        for i, repo in enumerate(repos, 1):
            repo["rank"] = i
        out.append({
            "key": bucket["key"],
            "label": bucket["label"],
            "summary": summarize(bucket),
            "repos": repos,
        })
    out.sort(key=lambda b: b["key"], reverse=True)
    return out


def build_stats(out_root):
    """汇总全部历史，返回 {'yearly': [...], 'quarterly': [...]}。"""
    yearly, quarterly = {}, {}
    for date, items in collect_snapshots(out_root):
        year_key, quarter_key = bucket_keys(date)
        year_bucket = yearly.setdefault(year_key, blank_bucket(year_key))
        quarter_bucket = quarterly.setdefault(quarter_key, blank_bucket(quarter_key))
        for it in items:
            add_item(year_bucket, date, it)
            add_item(quarter_bucket, date, it)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "yearly": finalize(yearly),
        "quarterly": finalize(quarterly),
    }


def trim_stats(stats, top=50):
    """裁剪用于看板内嵌的版本（每桶保留前 top 个仓库）。"""
    out = {"generated_at": stats.get("generated_at")}
    for scope in ("yearly", "quarterly"):
        out[scope] = []
        for bucket in stats[scope]:
            item = dict(bucket)
            item["repos"] = bucket["repos"][:top]
            out[scope].append(item)
    return out


def save_stats(out_root, stats):
    """把汇总结果按季度 / 年度分别落盘。"""
    base = out_root / "stats"
    for scope in ("yearly", "quarterly"):
        folder = base / scope
        folder.mkdir(parents=True, exist_ok=True)
        for bucket in stats[scope]:
            path = folder / f"{bucket['key']}.json"
            path.write_text(json.dumps(bucket, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[save] {path}（{len(bucket['repos'])} 个仓库）")


def main():
    ap = argparse.ArgumentParser(description="按季度 / 年度汇总 GitHub Trending 每日归档")
    ap.add_argument("--out-dir", default="data", help="归档目录（默认 data/）")
    args = ap.parse_args()
    out_root = Path(args.out_dir)
    if not out_root.exists():
        raise SystemExit(f"归档目录不存在: {out_root}")
    stats = build_stats(out_root)
    save_stats(out_root, stats)
    for scope in ("quarterly", "yearly"):
        for bucket in stats[scope]:
            s = bucket["summary"]
            print(f"[{scope}] {bucket['label']}: {s['days']} 天，{s['repos']} 个仓库，增星 {s['gained']:,}")


if __name__ == "__main__":
    main()
