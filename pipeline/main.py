import os
import re
import json
from datetime import datetime, timezone
from urllib.parse import urlparse, quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import tz
from dateutil import parser as dtparser
from unidecode import unidecode
from markdown import markdown as md_to_html

# ============================
# Config (env overrides)
# ============================
TIME_WINDOW_HOURS = int(os.getenv("TIME_WINDOW_HOURS", "48"))
MAX_STORIES = int(os.getenv("MAX_STORIES", "7"))          # HARD CAP
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "45")) # quality bar
INCLUDE_RUMORS = os.getenv("INCLUDE_RUMORS", "false").lower() == "true"
REGION_TZ = os.getenv("REGION_TZ", "America/Chicago")

USE_OPENAI = bool(os.getenv("OPENAI_API_KEY"))
USE_GEMINI = bool(os.getenv("GEMINI_API_KEY"))
OUTPUT_DIR = "docs"

# ============================
# Sources / heuristics
# ============================
EPL_CLUBS = [
    "Arsenal","Aston Villa","Bournemouth","Brentford","Brighton","Chelsea",
    "Crystal Palace","Everton","Fulham","Ipswich","Leicester","Liverpool",
    "Man City","Manchester City","Man United","Manchester United","Newcastle",
    "Nottingham Forest","Southampton","Spurs","Tottenham","West Ham","Wolves","Wolverhampton"
]

# Domains considered aggregators/low-signal (skip as primaries)
AGGREGATOR_DOMAINS = {
    "news.google.com", "consent.google.com", "news.yahoo.com", "flipboard.com",
    "bing.com", "newsnow.co.uk"
}

# Weights for virality & credibility
TIER_WEIGHTS = {
    # top-tier / official
    "premierleague.com": 5,
    "bbc.co.uk": 5, "bbc.com": 5,
    "skysports.com": 5,
    "theguardian.com": 4,
    "reuters.com": 4,
    "apnews.com": 4,
    "espn.com": 3,
    # club sites (treat as high authority for confirmations)
    "arsenal.com":4, "chelseafc.com":4, "liverpoolfc.com":4, "manutd.com":4, "mancity.com":4,
    "tottenhamhotspur.com":4, "evertonfc.com":4, "nufc.co.uk":4, "westhamunited.com":4,
    "lcfc.com":4, "cpfc.co.uk":4, "wolves.co.uk":4, "fulhamfc.com":4, "saintsfc.co.uk":4,
    "nottinghamforest.co.uk":4, "brightonandhovealbion.com":4, "avfc.co.uk":4,
    "afcb.co.uk":4, "brentfordfc.com":4, "itfc.co.uk":4,
}

# Google News queries (small, focused); we cap per-feed to keep volume sane
GN_QUERIES = [
    "English Premier League",
    "Premier League injuries",
    "Premier League disciplinary",
    "Premier League transfer",
    "Premier League controversy",
    # a few big clubs (you can add more later)
    "Arsenal Premier League", "Chelsea Premier League", "Liverpool Premier League",
    "Manchester United Premier League", "Manchester City Premier League",
    "Tottenham Premier League", "Newcastle United Premier League",
]

def google_news_rss(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-GB&gl=GB&ceid=GB:en"

RSS_FEEDS = [google_news_rss(q) for q in GN_QUERIES] + [
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.theguardian.com/football/rss",
]

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EPL-Pipeline/1.0)"}
MAX_PER_FEED = 20  # limit entries we read per feed

# ============================
# Utilities
# ============================
def utcnow():
    return datetime.now(timezone.utc)

def to_local(dt_utc, tzname):
    try:
        return dt_utc.astimezone(tz.gettz(tzname))
    except Exception:
        return dt_utc

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", unidecode(s or "").strip())

def normalize_title(s: str) -> str:
    s = clean_text(s).lower()
    s = re.sub(r"[\-–—:|]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    s = re.sub(r"\b(live|video|breaking)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()

def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "").lower()
    except Exception:
        return ""

def similarity(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / float(min(len(sa), len(sb)))

def guess_slug(headline: str) -> str:
    up = headline.upper()
    found = [c for c in EPL_CLUBS if c.upper() in up]
    topic = "NEWS"
    if "INJUR" in up: topic = "INJURY"
    elif "TRANSFER" in up: topic = "TRANSFER"
    elif "DISCIPLIN" in up or "BAN" in up: topic = "DISCIPLINE"
    elif "OWNERSHIP" in up or "TAKEOVER" in up: topic = "OWNERSHIP"
    teams = " vs ".join(found[:2]) if found else ""
    slug = (f"{teams} | {topic}" if teams else topic)[:48].upper()
    return slug

def is_epl_relevant(title: str) -> bool:
    t = title.lower()
    if "premier league" in t or "epl" in t:
        return True
    for club in EPL_CLUBS:
        if club.lower() in t:
            return True
    return False

def fetch_meta_follow(link: str, timeout=12):
    """
    Fetch page to (1) follow redirects to publisher, (2) get OG title/description.
    Returns dict with: title, description, final_url, final_domain
    """
    info = {"title":"", "description":"", "final_url":link, "final_domain":domain_of(link)}
    try:
        r = requests.get(link, timeout=timeout, headers=UA_HEADERS, allow_redirects=True)
        if r.status_code != 200:
            return info
        final_url = r.url
        final_domain = domain_of(final_url)
        # If we still landed on aggregator (rare), just return
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.find("meta", property="og:title") or soup.find("title")
        desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name":"description"})
        info["title"] = clean_text(title["content"] if title and title.has_attr("content") else (title.text if title else ""))
        info["description"] = clean_text(desc["content"] if desc and desc.has_attr("content") else (desc.text if desc else ""))
        info["final_url"] = final_url
        info["final_domain"] = final_domain
    except Exception:
        pass
    return info

def hours_ago(dt_utc):
    return (utcnow() - dt_utc).total_seconds() / 3600.0

def virality_score(cluster) -> int:
    primary = cluster["primary"]
    hrs = min(48.0, hours_ago(primary["published_utc"]))
    recency = max(0, 100 - int((hrs/48.0) * 70))  # up to 70 points
    count_bonus = min(20, 5 * (len(cluster["articles"]) - 1))  # duplicates add weight
    weight_bonus = 0
    seen = set()
    for a in cluster["articles"]:
        d = a["domain"]
        if d in seen: 
            continue
        weight_bonus += TIER_WEIGHTS.get(d, 2)
        seen.add(d)
    weight_bonus = min(30, weight_bonus)
    return min(100, recency + count_bonus + weight_bonus)

def in_window(dt_utc) -> bool:
    return hours_ago(dt_utc) <= TIME_WINDOW_HOURS

def credible_cluster(cluster) -> bool:
    """require ≥1 top-tier domain OR ≥2 medium-tier distinct domains"""
    tops = set()
    meds = set()
    for a in cluster["articles"]:
        w = TIER_WEIGHTS.get(a["domain"], 2)
        if w >= 4:
            tops.add(a["domain"])
        if w >= 3:
            meds.add(a["domain"])
    return len(tops) >= 1 or len(meds) >= 2

# ============================
# Step 1: collect items
# ============================
def collect_candidates():
    items = []
    seen_links = set()
    for feed in RSS_FEEDS:
        parsed = feedparser.parse(feed)
        for e in parsed.entries[:MAX_PER_FEED]:
            link = e.get("link")
            title_feed = clean_text(e.get("title", ""))
            if not link or not title_feed:
                continue

            # publish time
            published = None
            for key in ("published", "updated", "pubDate"):
                if e.get(key):
                    try:
                        published = dtparser.parse(e.get(key))
                        break
                    except Exception:
                        pass
            if not published:
                published = utcnow()
            if not published.tzinfo:
                published = published.replace(tzinfo=timezone.utc)
            published_utc = published.astimezone(timezone.utc)
            if not in_window(published_utc):
                continue

            # follow redirects to publisher & extract meta
            meta = fetch_meta_follow(link)
            final_url = meta["final_url"]
            dom = meta["final_domain"]

            # skip aggregators as primaries
            if dom in AGGREGATOR_DOMAINS:
                continue

            # prefer OG title if available
            title = meta["title"] or title_feed
            title = clean_text(title)

            # drop extremely short/blank titles or generic strings
            if len(title) < 12 or title.lower() in {"google news", "news"}:
                continue

            # EPL relevance filter
            if not is_epl_relevant(title):
                continue

            # dedupe by final URL
            if final_url in seen_links:
                continue
            seen_links.add(final_url)

            items.append({
                "title": title,
                "summary": meta["description"],
                "link": final_url,
                "published_utc": published_utc,
                "domain": dom,
                "norm_title": normalize_title(title),
            })

    return items

# ============================
# Step 2: cluster items
# ============================
def cluster_items(items, sim_thresh=0.72):
    items_sorted = sorted(items, key=lambda x: x["published_utc"], reverse=True)
    clusters = []
    for it in items_sorted:
        placed = False
        for cl in clusters:
            if similarity(it["norm_title"], cl["centroid"]) >= sim_thresh:
                cl["articles"].append(it)
                placed = True
                break
        if not placed:
            clusters.append({"centroid": it["norm_title"], "articles": [it]})

    # choose primary & score
    for cl in clusters:
        cl["articles"].sort(
            key=lambda a: (-TIER_WEIGHTS.get(a["domain"], 1), a["published_utc"]),
            reverse=True
        )
        cl["primary"] = cl["articles"][0]
        cl["score"] = virality_score(cl)

    # filter weak clusters & enforce credibility
    clusters = [c for c in clusters if c["score"] >= SCORE_THRESHOLD and credible_cluster(c)]
    clusters.sort(key=lambda c: (c["score"], c["primary"]["published_utc"]), reverse=True)

    # HARD CAP
    return clusters[:MAX_STORIES]

# ============================
# Step 3: writer blocks
# ============================
def openai_presenter_blocks(title, summary, sources, event_time_local, published_local):
    """Use OpenAI Responses API if OPENAI_API_KEY is set."""
    from openai import OpenAI
    client = OpenAI()  # reads key from env

    prompt = f"""
You are a sports broadcast writer. Create a presenter-ready story for a video news rundown.
Assume the viewer may not know the backstory. Provide context succinctly.

Return ONLY valid JSON with keys:
- script (string, ~90–140 words, conversational, no jargon)
- why (list of 1–2 bullets: impact or significance)
- context (list of 1–3 bullets: key background: standings, prior result, injuries, appeals, etc.)
- broll (list of 2–4 generic, non-infringing suggestions)
- lower_third (<=60 chars, punchy)

Story:
Title: {title}
Summary: {summary}
Event time (local): {event_time_local}
Published (local): {published_local}
Sources:
{chr(10).join([f"- {s['domain']} — {s['link']}" for s in sources])}
"""
    resp = client.responses.create(
        model="gpt-5-mini",
        input=prompt,
        response_format={"type": "json_object"}
    )
    data = json.loads(resp.output_text)
    return {
        "script": data.get("script", "").strip(),
        "why": [clean_text(x) for x in data.get("why", [])][:2],
        "context": [clean_text(x) for x in data.get("context", [])][:3],
        "broll": [clean_text(x) for x in data.get("broll", [])][:4],
        "lower_third": clean_text(data.get("lower_third", ""))[:60],
    }

def gemini_presenter_blocks(title, summary, sources, event_time_local, published_local):
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-1.5-pro")

    prompt = f"""
You are a sports broadcast writer. Create a presenter-ready story for a video news rundown.
Assume the viewer may not know the backstory. Provide context succinctly.

Return JSON with keys:
script (~90–140 words),
why (1–2 bullets),
context (1–3 bullets),
broll (2–4 suggestions),
lower_third (<=60 chars).

Story:
Title: {title}
Summary: {summary}
Event time (local): {event_time_local}
Published (local): {published_local}
Sources:
{chr(10).join([f"- {s['domain']} — {s['link']}" for s in sources])}
"""
    resp = model.generate_content(prompt)
    text = resp.text or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return {
            "script": data.get("script", "").strip(),
            "why": [clean_text(x) for x in data.get("why", [])][:2],
            "context": [clean_text(x) for x in data.get("context", [])][:3],
            "broll": [clean_text(x) for x in data.get("broll", [])][:4],
            "lower_third": clean_text(data.get("lower_third", ""))[:60],
        }
    except Exception:
        return None

def simple_presenter_blocks(title, summary, sources):
    why = []
    up = (title + " " + (summary or "")).lower()
    if any(k in up for k in ["injur", "hamstring", "ankle", "knee"]):
        why.append("Potential impact on lineup and results.")
    if any(k in up for k in ["ban", "suspend", "red card", "disciplin"]):
        why.append("Disciplinary outcome may affect upcoming fixtures.")
    if any(k in up for k in ["transfer", "sign", "fee", "contract"]):
        why.append("Transfer implications and squad balance.")
    if not why:
        why.append("Relevance to standings or momentum.")
    context = [
        "Verified across reputable outlets.",
        "Avoid copyrighted match footage; use generic b-roll.",
    ]
    broll = [
        "Stadium exterior and fans",
        "Training ground drills",
        "Press conference backdrop and microphones",
    ]
    lower_third = (title[:57] + "…") if len(title) > 58 else title
    script = (summary and f"{title}. {summary}") or f"{title}."
    return {
        "script": script,
        "why": why[:2],
        "context": context[:3],
        "broll": broll[:4],
        "lower_third": lower_third,
    }

def pick_sources(cluster, k=3):
    """Pick up to k sources from distinct domains, favoring higher tier and recency."""
    out = []
    seen = set()
    for a in sorted(cluster["articles"],
                    key=lambda x: (-TIER_WEIGHTS.get(x["domain"], 1), x["published_utc"]),
                    reverse=True):
        if a["domain"] in seen:
            continue
        out.append(a)
        seen.add(a["domain"])
        if len(out) >= k:
            break
    return out

# ============================
# Step 4: compose markdown + HTML
# ============================
def make_markdown(clusters):
    now_utc = utcnow()
    local = to_local(now_utc, REGION_TZ)

    lines = []
    lines.append(f"# EPL Viral News — Presenter Pack (Last {TIME_WINDOW_HOURS}h)")
    lines.append(f"**Generated:** {local.strftime('%Y-%m-%d %H:%M')} ({REGION_TZ}), {now_utc.strftime('%Y-%m-%d %H:%M')} (UTC)")
    lines.append(f"**Stories:** {len(clusters)}  |  **Style:** conversational broadcast\n")

    # TL;DR
    lines.append("## TL;DR (15–20s)")
    for c in clusters[:3]:
        lines.append(f"- {c['primary']['title']}")
    lines.append("\n---\n")

    for i, c in enumerate(clusters, start=1):
        p = c["primary"]
        event_local = to_local(p["published_utc"], REGION_TZ)
        slug = guess_slug(p["title"])
        sources = pick_sources(c, k=3)

        # writer selection
        blocks = None
        if USE_OPENAI:
            try:
                blocks = openai_presenter_blocks(
                    p["title"], p["summary"], sources,
                    event_local.strftime('%Y-%m-%d %H:%M'),
                    event_local.strftime('%Y-%m-%d %H:%M'),
                )
            except Exception:
                blocks = None
        if blocks is None and USE_GEMINI:
            try:
                blocks = gemini_presenter_blocks(
                    p["title"], p["summary"], sources,
                    event_local.strftime('%Y-%m-%d %H:%M'),
                    event_local.strftime('%Y-%m-%d %H:%M'),
                )
            except Exception:
                blocks = None
        if blocks is None:
            blocks = simple_presenter_blocks(p["title"], p["summary"], sources)

        lines.append(f"## Story {i}: {p['title']}")
        lines.append(f"**Slug (lower-third):** {blocks.get('lower_third') or slug}  ")
        lines.append(f"**When:** {event_local.strftime('%Y-%m-%d %H:%M')} ({REGION_TZ})  •  **Published:** {p['published_utc'].strftime('%Y-%m-%d %H:%M')} (UTC)  ")
        lines.append(f"**Virality score:** {c['score']}\n")

        lines.append("### Presenter Script (≈45–75s)")
        lines.append((blocks.get("script") or "(script unavailable)").strip() + "\n")

        lines.append("### Why It Matters")
        for b in blocks.get("why", []):
            lines.append(f"- {b}")
        if not blocks.get("why"):
            lines.append("- Relevance to standings or fixtures.")

        lines.append("\n### Context")
        for b in blocks.get("context", []):
            lines.append(f"- {b}")

        lines.append("\n### Sources")
        for s in sources:
            title = s.get("title") or "Source"
            lines.append(f"- {s['domain']} — {title}. {s['link']}")

        lines.append("\n### B-roll Suggestions (non-infringing)")
        for b in blocks.get("broll", []):
            lines.append(f"- {b}")

        if INCLUDE_RUMORS:
            lines.append("\n> Notes: Label as *Rumor/Report* if not officially confirmed.")
        lines.append("\n---\n")

    lines.append("## Outro\nA quick wrap: for more, follow and subscribe.\n")
    return "\n".join(lines)

def render_html(md_text: str, title="EPL Viral News — Presenter Pack") -> str:
    """Simple HTML wrapper (NotebookLM-friendly)."""
    body = md_to_html(md_text, extensions=["fenced_code", "tables", "toc"])
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial,sans-serif;line-height:1.6;max-width:900px;margin:40px auto;padding:0 16px}}
  code,pre{{font-family:ui-monospace,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;background:#f6f8fa}}
  pre{{padding:12px;overflow:auto}}
  h1,h2,h3{{line-height:1.25}}
  blockquote{{border-left:4px solid #ddd;padding-left:12px;color:#555}}
  hr{{border:0;border-top:1px solid #eee;margin:24px 0}}
  a{{color:#0366d6;text-decoration:none}}
  a:hover{{text-decoration:underline}}
</style>
<body>
{body}
</body>
</html>"""

# ============================
# Orchestration
# ============================
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Collecting candidates...")
    items = collect_candidates()
    print(f"Collected {len(items)} items after filtering/redirects.")

    print("Clustering...")
    clusters = cluster_items(items)
    print(f"Selected {len(clusters)} clusters (capped at MAX_STORIES={MAX_STORIES}).")

    md = make_markdown(clusters)

    # file names
    now_local = to_local(utcnow(), REGION_TZ)
    stamp = now_local.strftime("%Y-%m-%d_%H%M")
    archive_md = os.path.join(OUTPUT_DIR, f"epl-viral-news_{stamp}_CT.md")
    latest_md = os.path.join(OUTPUT_DIR, "latest.md")
    archive_html = archive_md.replace(".md", ".html")
    latest_html = os.path.join(OUTPUT_DIR, "latest.html")
    archive_txt = archive_md.replace(".md", ".txt")
    latest_txt = os.path.join(OUTPUT_DIR, "latest.txt")

    # write MD
    with open(archive_md, "w", encoding="utf-8") as f:
        f.write(md)
    with open(latest_md, "w", encoding="utf-8") as f:
        f.write(md)

    # write TXT
    with open(archive_txt, "w", encoding="utf-8") as f:
        f.write(md)
    with open(latest_txt, "w", encoding="utf-8") as f:
        f.write(md)

    # write HTML
    html = render_html(md)
    with open(archive_html, "w", encoding="utf-8") as f:
        f.write(html)
    with open(latest_html, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote {archive_md}, {archive_html}, {archive_txt}, {latest_md}, {latest_html}, {latest_txt}")

if __name__ == "__main__":
    main()
