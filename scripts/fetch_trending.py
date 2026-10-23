#!/usr/bin/env python3
"""GitHub Trending 抓取（AI / Agent 方向）+ 推送。

抓取 github.com/trending 的 daily / weekly / monthly 榜单，
按 config.json 中的关键词给仓库打分类标签（多标签，一个仓库可同时属于
ai 和 agent），分类归档到 data/日期/ 目录，并推送合并摘要到已配置的
通知渠道：飞书、钉钉、Telegram、Server酱。
"""

import argparse
import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from stats import build_stats, save_stats, trim_stats

BASE_URL = "https://github.com/trending"
PERIODS = ("daily", "weekly", "monthly")
PERIOD_CN = {"daily": "今日", "weekly": "本周", "monthly": "本月"}
GAINED_RE = re.compile(r"([\d,]+)\s+stars\s+(today|this week|this month)")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
GAINED_LABEL = {"daily": "today", "weekly": "this week", "monthly": "this month"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def to_int(text):
    """'12,345' -> 12345，空值返回 None。"""
    text = (text or "").strip().replace(",", "")
    return int(text) if text.isdigit() else None


def fetch_page(period, retries=3):
    """抓取 trending 页面 HTML，失败自动重试。"""
    url = f"{BASE_URL}?since={period}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text, url
        except requests.RequestException as exc:
            print(f"[warn] 第 {attempt} 次抓取失败 {url}: {exc}", file=sys.stderr)
            if attempt == retries:
                raise
            time.sleep(5 * attempt)
    raise RuntimeError("unreachable")


def parse_trending(html_text):
    """解析 trending 页面，返回按榜单顺序排列的仓库列表。"""
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    for rank, article in enumerate(soup.select("article.Box-row"), start=1):
        link = article.select_one("h2 a")
        if link is None or not link.get("href"):
            continue
        href = link["href"].strip().strip("/")
        if href.startswith("https://github.com/"):
            href = href[len("https://github.com/"):]
        full_name = href

        desc_el = article.select_one("p")
        lang_el = article.select_one('span[itemprop="programmingLanguage"]')
        stars_el = article.select_one('a[href$="/stargazers"]')
        forks_el = article.select_one('a[href$="/forks"]')

        gained = None
        for span in article.find_all("span"):
            m = GAINED_RE.search(span.get_text(" ", strip=True))
            if m:
                gained = to_int(m.group(1))
                break

        items.append({
            "trend_rank": rank,
            "full_name": full_name,
            "url": f"https://github.com/{full_name}",
            "description": desc_el.get_text(" ", strip=True) if desc_el else "",
            "language": lang_el.get_text(strip=True) if lang_el else None,
            "stars": to_int(stars_el.get_text()) if stars_el else None,
            "forks": to_int(forks_el.get_text()) if forks_el else None,
            "stars_gained": gained,
            "categories": [],
        })
    return items


def load_categories(config_path):
    """读取关键词配置并编译成正则。匹配范围：仓库名 + 描述（大小写不敏感，词边界）。"""
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    categories = {}
    for cat, spec in cfg["categories"].items():
        keywords = [k.strip().lower() for k in spec["keywords"] if k.strip()]
        if not keywords:
            continue
        pattern = re.compile("|".join(rf"\b{re.escape(k)}\b" for k in keywords))
        categories[cat] = {"label": spec.get("label", cat), "pattern": pattern}
    if not categories:
        raise SystemExit(f"配置文件 {config_path} 中没有任何分类关键词")
    return categories


def tag_items(items, categories):
    """给每个仓库打多标签。"""
    for it in items:
        haystack = f"{it['full_name']} {it['description']}".lower()
        it["categories"] = [cat for cat, c in categories.items()
                            if c["pattern"].search(haystack)]
    return items


def load_translation_cache(out_root):
    """读取本地翻译缓存（英文描述 -> 中文），避免重复请求。"""
    path = out_root / "cache" / "translations.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[warn] 翻译缓存读取失败，忽略: {exc}", file=sys.stderr)
        return {}


def save_translation_cache(out_root, cache):
    path = out_root / "cache" / "translations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    print(f"[save] {path}（{len(cache)} 条缓存）")


def translate_google(text, session):
    """Google 免费翻译接口。"""
    resp = session.get(TRANSLATE_URL, params={
        "client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text[:1500],
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()


def translate_mymemory(text, session):
    """MyMemory 免费接口（Google 不可达时的备选）。"""
    resp = session.get(MYMEMORY_URL, params={
        "q": text[:480], "langpair": "en|zh-CN",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if str(data.get("responseStatus")) != "200":
        raise RuntimeError(f"MyMemory {data.get('responseStatus')}: {data.get('responseDetails')}")
    zh = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if "MYMEMORY WARNING" in zh.upper():
        raise RuntimeError(zh[:120])
    return zh


TRANSLATORS = (translate_google, translate_mymemory)


def translate_text(text, session, dead=None):
    """依次尝试各翻译源，全部失败时抛异常。连续失败 3 次的源在本次运行内跳过。"""
    dead = dead if dead is not None else {}
    errors = []
    for fn in TRANSLATORS:
        name = fn.__name__
        if dead.get(name, 0) >= 3:
            continue
        try:
            zh = fn(text, session)
        except Exception as exc:  # noqa: BLE001
            dead[name] = dead.get(name, 0) + 1
            errors.append(f"{name}: {exc}")
            continue
        if zh and zh.lower() != text.lower():
            dead[name] = 0
            return zh
        errors.append(f"{name}: 返回空结果")
    raise RuntimeError("; ".join(errors) or "所有翻译源均不可用")


def translate_items(items, cache, session, state):
    """为仓库补 description_zh。接口连续失败 3 次后本次运行放弃翻译。"""
    done = 0
    for it in items:
        text = (it.get("description") or "").strip()
        it["description_zh"] = ""
        if not text or CJK_RE.search(text):
            continue
        if text in cache:
            it["description_zh"] = cache[text]
            continue
        if state["disabled"]:
            continue
        try:
            zh = translate_text(text, session, state["dead"])
        except Exception as exc:  # noqa: BLE001
            state["fails"] += 1
            print(f"[warn] 翻译失败（连续 {state['fails']} 次）: {exc}", file=sys.stderr)
            if state["fails"] >= 3:
                state["disabled"] = True
                print("[warn] 翻译接口不可用，本次仅展示英文原文", file=sys.stderr)
            continue
        state["fails"] = 0
        if zh:
            cache[text] = zh
            it["description_zh"] = zh
            done += 1
        time.sleep(0.2)  # 控制请求频率
    return done


def render_section(items, limit, period):
    """渲染一个分类区块的 Markdown 文本。"""
    lines = []
    for i, it in enumerate(items[:limit], 1):
        gained = ""
        if it["stars_gained"]:
            gained = f" (+{it['stars_gained']:,} {GAINED_LABEL[period]})"
        lines.append(
            f"{i}. [{it['full_name']}]({it['url']})"
            f" ⭐ {it['stars'] or 0:,}{gained} · {it['language'] or 'N/A'}"
        )
        desc = it.get("description_zh") or it["description"]
        if desc:
            lines.append(f"   {desc}")
    return "\n".join(lines) if lines else "（本周期无命中项目）"


def render_html_section(items, limit, period):
    """Telegram 用 HTML 区块。"""
    parts = []
    for i, it in enumerate(items[:limit], 1):
        desc = html_mod.escape(it.get("description_zh") or it["description"])
        line = (
            f"{i}. <a href=\"{it['url']}\"><b>{html_mod.escape(it['full_name'])}</b></a>"
            f" ⭐ {it['stars'] or 0:,}"
        )
        if it["stars_gained"]:
            line += f" (+{it['stars_gained']:,} {GAINED_LABEL[period]})"
        line += f" · {html_mod.escape(it['language'] or 'N/A')}"
        if desc:
            line += f"\n{desc}"
        parts.append(line)
    return "\n".join(parts) if parts else "（本周期无命中项目）"


def build_message(period, date_str, by_cat, categories, push_limit):
    """每周期一条合并消息：AI 专区 + Agent 专区。"""
    title = f"📈 GitHub Trending {PERIOD_CN[period]} · {date_str}"
    md_blocks, html_blocks = [], []
    for cat, meta in categories.items():
        md_blocks.append(f"### {meta['label']} 专区\n{render_section(by_cat[cat], push_limit, period)}")
        html_blocks.append(
            f"<b>{html_mod.escape(meta['label'])} 专区</b>\n"
            f"{render_html_section(by_cat[cat], push_limit, period)}"
        )
    return {"title": title, "markdown": "\n\n".join(md_blocks), "html": "\n\n".join(html_blocks)}


def save_result(out_root, date_str, period, items, by_cat, categories, url, top):
    """归档：全量 + 各分类 JSON（含 latest 副本）+ 人类可读的 Markdown 日报。"""
    folder = out_root / date_str
    folder.mkdir(parents=True, exist_ok=True)
    latest = out_root / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    suffix_files = {}  # 文件名后缀 -> items
    all_payload = {
        "period": period, "category": "all", "fetched_at": fetched_at,
        "source": url, "items": items,
    }
    suffix_files["all"] = all_payload
    for cat in categories:
        cat_items = []
        for i, it in enumerate([x for x in items if cat in x["categories"]][:top], 1):
            entry = dict(it)
            entry["rank"] = i
            cat_items.append(entry)
        suffix_files[cat] = {
            "period": period, "category": cat, "fetched_at": fetched_at,
            "source": url, "items": cat_items,
        }

    for suffix, payload in suffix_files.items():
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        (folder / f"{period}-{suffix}.json").write_text(text, encoding="utf-8")
        (latest / f"{period}-{suffix}.json").write_text(text, encoding="utf-8")
        print(f"[save] {folder / (period + '-' + suffix + '.json')} ({len(payload['items'])} 条)")

    md_lines = [
        f"# 📈 GitHub Trending · {PERIOD_CN[period]} · {date_str}",
        "",
        f"> 来源：{url}　|　全量 {len(items)} 条已归档至 {period}-all.json",
        "",
    ]
    for cat, meta in categories.items():
        md_lines += [f"## {meta['label']} 专区", "", render_section(by_cat[cat], top, period), ""]
    (folder / f"{period}.md").write_text("\n".join(md_lines), encoding="utf-8")
    print(f"[save] {folder / (period + '.md')}")


def build_dashboard(out_root, categories, out_file, stats_data=None):
    """读取 data/latest 下最新数据，生成可离线打开的单文件看板。"""
    template_path = Path(__file__).with_name("dashboard_template.html")
    template = template_path.read_text(encoding="utf-8")
    periods = {}
    for period in PERIODS:
        path = out_root / "latest" / f"{period}-all.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        periods[period] = {
            "fetched_at": payload.get("fetched_at"),
            "items": payload.get("items", []),
        }
    if not periods and not stats_data:
        print("[warn] data/latest 下暂无数据，跳过看板生成", file=sys.stderr)
        return False
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "categories": {cat: meta["label"] for cat, meta in categories.items()},
        "periods": periods,
        "stats": trim_stats(stats_data, 50) if stats_data else {"yearly": [], "quarterly": []},
    }
    html_text = template.replace(
        "/*__DATA__*/null",
        json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
    )
    Path(out_file).write_text(html_text, encoding="utf-8")
    print(f"[save] {out_file}（{len(periods)} 个周期）")
    return True


def post(url, payload=None, data=None):
    resp = requests.post(url, json=payload, data=data, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp


def push_channel(channel, msg):
    """调用对应渠道的推送 API。"""
    if channel == "feishu":
        post(os.environ["FEISHU_WEBHOOK"], payload={
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": msg["title"]},
                           "template": "blue"},
                "elements": [{"tag": "markdown", "content": msg["markdown"]}],
            },
        })
    elif channel == "dingtalk":
        post(os.environ["DINGTALK_WEBHOOK"], payload={
            "msgtype": "markdown",
            "markdown": {"title": msg["title"], "text": f"### {msg['title']}\n\n{msg['markdown']}"},
        })
    elif channel == "telegram":
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
        post(f"https://api.telegram.org/bot{token}/sendMessage", payload={
            "chat_id": chat_id,
            "text": f"<b>{html_mod.escape(msg['title'])}</b>\n\n{msg['html']}",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
    elif channel == "serverchan":
        key = os.environ["SERVERCHAN_SENDKEY"]
        post(f"https://sctapi.ftqq.com/{key}.send", data={
            "title": msg["title"],
            "desp": msg["markdown"],
        })


def configured_channels():
    channels = []
    if os.environ.get("FEISHU_WEBHOOK"):
        channels.append("feishu")
    if os.environ.get("DINGTALK_WEBHOOK"):
        channels.append("dingtalk")
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        channels.append("telegram")
    if os.environ.get("SERVERCHAN_SENDKEY"):
        channels.append("serverchan")
    return channels


def main():
    ap = argparse.ArgumentParser(description="抓取并推送 GitHub Trending（AI/Agent 方向）")
    ap.add_argument("--periods", default="daily,weekly,monthly",
                    help="逗号分隔：daily,weekly,monthly（默认全部）")
    ap.add_argument("--config", default="config.json", help="关键词分类配置文件")
    ap.add_argument("--top", type=int, default=25, help="归档条数上限（默认 25）")
    ap.add_argument("--push-limit", type=int, default=10, help="每专区推送条数（默认 10）")
    ap.add_argument("--out-dir", default="data", help="归档目录（默认 data/）")
    ap.add_argument("--dashboard", default="index.html", help="看板输出路径（默认 index.html）")
    ap.add_argument("--no-translate", action="store_true", help="不翻译描述（仍使用已有缓存）")
    ap.add_argument("--no-dashboard", action="store_true", help="不生成 HTML 看板")
    ap.add_argument("--no-push", action="store_true", help="只归档，不推送")
    args = ap.parse_args()

    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    for p in periods:
        if p not in PERIODS:
            ap.error(f"无效的周期: {p}（可选值 {PERIODS}）")
    categories = load_categories(args.config)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_root = Path(args.out_dir)
    channels = [] if args.no_push else configured_channels()
    if args.no_push or not channels:
        print("[info] 推送已关闭或未配置渠道，仅执行归档")
    else:
        print(f"[info] 推送渠道: {', '.join(channels)}")

    failures, messages = 0, []
    cache = load_translation_cache(out_root)
    session = requests.Session()
    session.headers.update(HEADERS)
    t_state = {"fails": 0, "disabled": args.no_translate, "dead": {}}
    if t_state["disabled"]:
        print("[info] 已关闭翻译，仅使用缓存中的中文描述")
    for i, period in enumerate(periods):
        try:
            html_text, url = fetch_page(period)
            items = tag_items(parse_trending(html_text)[:args.top], categories)
            translated = translate_items(items, cache, session, t_state)
            if translated:
                print(f"[i18n] {period}: 新翻译 {translated} 条描述")
            by_cat = {cat: [x for x in items if cat in x["categories"]] for cat in categories}
            save_result(out_root, date_str, period, items, by_cat, categories, url, args.top)
            messages.append(build_message(period, date_str, by_cat, categories, args.push_limit))
            summary = "、".join(f"{categories[c]['label']} {len(by_cat[c])}" for c in categories)
            print(f"[done] {period}: 全量 {len(items)} 条（{summary}）")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[error] {period} 抓取失败: {exc}", file=sys.stderr)
        if i < len(periods) - 1:
            time.sleep(2)  # 请求间隔，对源站友好

    save_translation_cache(out_root, cache)
    stats_data = build_stats(out_root)
    save_stats(out_root, stats_data)
    if not args.no_dashboard:
        build_dashboard(out_root, categories, args.dashboard, stats_data)

    for msg in messages:
        for channel in channels:
            try:
                push_channel(channel, msg)
                print(f"[push] {channel} <- {msg['title']} ✓")
            except Exception as exc:  # noqa: BLE001
                print(f"[error] 推送 {channel} 失败: {exc}", file=sys.stderr)

    if failures == len(periods):
        sys.exit(1)


if __name__ == "__main__":
    main()
