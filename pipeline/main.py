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
# Config (env vars override)
# ============================
TIME_WINDOW_HOURS = int(os.getenv("TIME_WINDOW_HOURS", "48"))
MIN_STORIES = int(os.getenv("MIN_STORIES", "5"))
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

# Domain weights for virality scoring
TIER_WEIGHTS = {
    "premierleague.com": 5,
    "bbc.co.uk": 5, "bbc.com": 5,
    "skysports.com": 5,
    "theguardian.com": 4,
    "reuters.com": 4,
    "espn.com": 3,
    # common club sites
    "arsenal.com":4, "chelseafc.com":4, "liverpoolfc.com":4, "manutd.com":4, "mancity.com":4,
    "tottenhamhotspur.com":4, "evertonfc.com":4, "nufc.co.uk":4, "westhamunited.com":4,
    "lcfc.com":4, "cpfc.co.uk":4, "wolves.co.uk":4, "fulhamfc.com":4, "saintsfc.co.uk":4,
    "nottinghamforest.co.uk":4, "brightonandhovealbion.com":4, "avfc.co.uk":4,
    "afcb.co.uk":4, "brentfordfc.com":4, "itfc.co.uk":4,
}

GN_QUERIES = [
    "English Premier League",
    "Premier League injuries",
    "Premier League disciplinary",
    "Premier League transfer",
    "Premier League controversy",
    # some big clubs (add more later if you want)
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

def fetch_meta(link: str, timeout=10):
    info = {"title": "", "description": ""}
    try:
        r = requests.get(link, timeout=timeout, headers=UA_HEADERS)
        if r.status_code != 200:
            return info
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.find("meta", property="og:title") or soup.find("title")
        desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        info["title"] = clean_text(title["content"] if title and title.has_attr("content") else (title.text if title else ""))
        info["description"] = clean_text(desc["content"] if desc and desc.has_attr("content") else (desc.text if desc else ""))
    except Exception:
        pass
    return info

def hours_ago(dt_utc):
    return (utcnow() - dt_utc).total_seconds() / 3600.0

def virality_score(cluster) -> int:
    primary = cluster["primary"]
    hrs = min(48.0, hours_ago(primary["published_utc"]))
    recency = max(0, 100 - int((hrs/48.0) * 70))   # up to 70 pts from recency
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

# ============================
# Step 1: collect items
# ============================
def collect_candidates():
    items = []
    for feed in RSS_FEEDS:
        parsed = feedparser.parse(feed)
        for e in parsed.entries:
            link = e.get("link")
            title = clean_text(e.get("title", ""))
            if not link or not title:
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

            meta = fetch_meta(link)
            items.append({
                "title": title if meta["title"] == "" else meta["title"],
                "summary": meta["description"],
                "link": link,
                "published_utc": published_utc,
                "domain": domain_of(link),
                "norm_title": normalize_title(title),
            })

    # de-dup by link
    uniq = {}
    for it in items:
        uniq[it["link"]] = it
    return list(uniq.values())

# ============================
# Step 2: cluster items
# ============================
def cluster_items(items, sim_thresh=0.68):
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

    # choose primary, score clusters
    for cl in clusters:
        cl["articles"].sort(key=lambda a: (
            -TIER_WEIGHTS.get(a["domain"], 1),
            a["published_utc"]
        ), reverse=True)
        cl["primary"] = cl["articles"][0]
        cl["score"] = virality_score(cl)

    # filter weak ones and sort
    clusters = [c for c in clusters if c["score"] >= 40]
    clusters.sort(key=lambda c: (c["score"], c["primary"]["published_utc"]), reverse=True)
    return clusters

# ============================
# Step 3: writer blocks
# ============================
def openai_presenter_blocks(title, summary, sources, event_time_local, published_local):
    """Use OpenAI Responses API if OPENAI_API_KEY is set."""
    from openai import OpenAI
    client = OpenAI()  # reads key from env

    prompt = f"""
You are a sports broadcast writer. Write a presenter's pack entry about this EPL story.
Audience may not know background; add concise context (1–3 bullets).
Tone: clear, conversational, ~45–75s script. Avoid jargon. Label rumors if unconfirmed.

Story:
Title: {title}
Summary: {summary}
Event time (local): {event_time_local}
Published (local): {published_local}
Sources:
{chr(10).join([f"- {s['domain']} — {s['link']}" for s in sources])}

Return ONLY valid JSON with keys:
script (string),
why (list of 1-2 bullets),
context (list of 1-3 bullets),
broll (list of 2-4 generic, non-infringing suggestions),
lower_third (<=60 chars).
"""
    resp = client.responses.create(
        model="gpt-4o-mini",
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
    """Use Gemini if GEMINI_API_KEY is set (simple JSON extraction)."""
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-1.5-pro")

    prompt = f"""
You are a sports broadcast writer. Write a presenter's pack entry about this EPL story.
Audience may not know context; add concise background (1–3 bullets).
Tone: clear, conversational, ~45–75s script. Avoid jargon. Label rumors if unconfirmed.

Story:
Title: {title}
Summary: {summary}
Event time (local): {event_time_local}
Published (local): {published_local}
Sources:
{chr(10).join([f"- {s['domain']} — {s['link']}" for s in sources])}

Return JSON with keys:
script (string),
why (list of 1-2 bullets),
context (list of 1-3 bullets),
broll (list of 2-4 generic, non-infringing suggestions),
lower_third (<=60 chars).
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
    up = (title + " " + summary).lower()
    if any(k in up for k in ["injur", "hamstring", "ankle", "knee"]):
        why.append("Potential impact on lineup and results.")
    if any(k in up for k in ["ban", "suspend", "red card", "disciplin"]):
        why.append("Disciplinary outcome may affect upcoming fixtures.")
    if any(k in up for k in ["transfer", "sign", "fee", "contract"]):
        why.append("Transfer implications and squad balance.")
    if not why:
        why.append("Relevance to standings or momentum.")
    context = [
        "Story verified across multiple outlets.",
        "Avoid copyrighted match footage; use generic b-roll.",
    ]
    broll = [
        "Stadium exterior, fans arriving",
        "Training ground shots, warm-ups",
        "Press conference podium and microphones",
    ]
    lower_third = (title[:57] + "…") if len(title) > 58 else title
    script = f"{title}. {summary}"
    return {
        "script": script,
        "why": why[:2],
        "context": context[:3],
        "broll": broll[:4],
        "lower_third": lower_third,
    }

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
        sources = c["articles"][:3]

        # pick writer
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
    """Wrap Markdown as a simple, clean HTML page (for NotebookLM ingestion)."""
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
    print(f"Collected {len(items)} items.")

    print("Clustering...")
    clusters = cluster_items(items)
    if len(clusters) < MIN_STORIES:
        print(f"Only {len(clusters)} clusters after filtering; proceeding anyway.")
    print(f"{len(clusters)} clusters after filtering.")

    md = make_markdown(clusters[:max(MIN_STORIES, len(clusters))])

    # file names
    now_local = to_local(utcnow(), REGION_TZ)
    stamp = now_local.strftime("%Y-%m-%d_%H%M")
    archive_md = os.path.join(OUTPUT_DIR, f"epl-viral-news_{stamp}_CT.md")
    latest_md = os.path.join(OUTPUT_DIR, "latest.md")
    archive_html = archive_md.replace(".md", ".html")
    latest_html = os.path.join(OUTPUT_DIR, "latest.html")

    # write MD
    with open(archive_md, "w", encoding="utf-8") as f:
        f.write(md)
    with open(latest_md, "w", encoding="utf-8") as f:
        f.write(md)

    # write HTML
    html = render_html(md)
    with open(archive_html, "w", encoding="utf-8") as f:
        f.write(html)
    with open(latest_html, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote {archive_md}, {latest_md}, {archive_html}, {latest_html}")

if __name__ == "__main__":
    main()
