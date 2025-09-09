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
import trafilatura

# ============================
# Config (env overrides)
# ============================
TIME_WINDOW_HOURS = int(os.getenv("TIME_WINDOW_HOURS", "48"))
MAX_STORIES = int(os.getenv("MAX_STORIES", "10"))            # balanced slate size
PER_CLUB_LIMIT = int(os.getenv("PER_CLUB_LIMIT", "2"))       # max per club/topic
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "45"))    # quality bar
INCLUDE_RUMORS = os.getenv("INCLUDE_RUMORS", "false").lower() == "true"
REGION_TZ = os.getenv("REGION_TZ", "America/Chicago")

USE_OPENAI = bool(os.getenv("OPENAI_API_KEY"))
USE_GEMINI = bool(os.getenv("GEMINI_API_KEY"))
OUTPUT_DIR = "docs"

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EPL-Pipeline/2.0)"}
MAX_PER_FEED = 20  # per feed cap to keep volume sane

# ============================
# Sources / heuristics
# ============================
EPL_CLUBS = [
    "Arsenal","Aston Villa","Bournemouth","Brentford","Brighton","Chelsea",
    "Crystal Palace","Everton","Fulham","Ipswich","Leicester","Liverpool",
    "Man City","Manchester City","Man United","Manchester United","Newcastle",
    "Nottingham Forest","Southampton","Spurs","Tottenham","West Ham","Wolves","Wolverhampton"
]

AGGREGATOR_DOMAINS = {
    "news.google.com", "consent.google.com", "news.yahoo.com", "flipboard.com",
    "bing.com", "newsnow.co.uk"
}

# Virality/credibility weights
TIER_WEIGHTS = {
    # top-tier / official
    "premierleague.com": 5,
    "bbc.co.uk": 5, "bbc.com": 5,
    "skysports.com": 5,
    "theguardian.com": 4,
    "reuters.com": 4,
    "apnews.com": 4,
    "espn.com": 3,

    # club sites (authoritative confirmations)
    "arsenal.com":4, "chelseafc.com":4, "liverpoolfc.com":4, "manutd.com":4, "mancity.com":4,
    "tottenhamhotspur.com":4, "evertonfc.com":4, "nufc.co.uk":4, "westhamunited.com":4,
    "lcfc.com":4, "cpfc.co.uk":4, "wolves.co.uk":4, "fulhamfc.com":4, "saintsfc.co.uk":4,
    "nottinghamforest.co.uk":4, "brightonandhovealbion.com":4, "avfc.co.uk":4,
    "afcb.co.uk":4, "brentfordfc.com":4, "itfc.co.uk":4,
}

# Google News queries (concise but broad)
GN_QUERIES = [
    "English Premier League",
    "Premier League injuries",
    "Premier League disciplinary",
    "Premier League transfer",
    "Premier League controversy",
    "Arsenal Premier League", "Chelsea Premier League", "Liverpool Premier League",
    "Manchester United Premier League", "Manchester City Premier League",
    "Tottenham Premier League", "Newcastle United Premier League",
]

def google_news_rss(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-GB&gl=GB&ceid=GB:en"

RSS_FEEDS = [google_news_rss(q) for q in GN_QUERIES] + [
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.theguardian.com/football/rss",
    "https://www.skysports.com/rss/12040",         # Sky Sports Football
    "https://www.reuters.com/subjects/soccer/rss", # Reuters Soccer
    "https://www.espn.com/espn/rss/soccer/news",   # ESPN Soccer
]

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

def hours_ago(dt_utc):
    return (utcnow() - dt_utc).total_seconds() / 3600.0

def in_window(dt_utc) -> bool:
    return hours_ago(dt_utc) <= TIME_WINDOW_HOURS

def is_epl_relevant(title: str, summary: str = "") -> bool:
    t = (title + " " + (summary or "")).lower()
    if "premier league" in t or "epl" in t:
        return True
    for club in EPL_CLUBS:
        if club.lower() in t:
            return True
    return False

def guess_slug(headline: str) -> str:
    up = headline.upper()
    found = [c for c in EPL_CLUBS if c.upper() in up]
    topic = "NEWS"
    if "INJUR" in up: topic = "INJURY"
    elif "TRANSFER" in up: topic = "TRANSFER"
    elif "DISCIPLIN" in up or "BAN" in up: topic = "DISCIPLINE"
    elif "OWNERSHIP" in up or "TAKEOVER" in up: topic = "OWNERSHIP"
    elif "MANAGER" in up or "COACH" in up or "APPOINT" in up or "SACK" in up: topic = "MANAGERIAL"
    teams = " vs ".join(found[:2]) if found else ""
    slug = (f"{teams} | {topic}" if teams else topic)[:48].upper()
    return slug

def categorize(title: str, summary: str = "") -> str:
    t = (title + " " + (summary or "")).lower()
    if any(k in t for k in ["appoint", "sack", "manager", "head coach", "caretaker"]):
        return "Managerial Moves"
    if any(k in t for k in ["injury", "hamstring", "ankle", "knee", "ruled out", "return"]):
        return "Injuries"
    if any(k in t for k in ["transfer", "loan", "signs", "fee", "contract"]):
        return "Transfers"
    if any(k in t for k in ["charge", "appeal", "ban", "discipline", "psr", "ffp", "rule", "settlement"]):
        return "League & Regulation"
    return "Club Updates"

def fetch_meta_follow(link: str, timeout=12):
    """
    Follow redirects to the publisher and extract OG title/description.
    """
    info = {"title":"", "description":"", "final_url":link, "final_domain":domain_of(link)}
    try:
        r = requests.get(link, timeout=timeout, headers=UA_HEADERS, allow_redirects=True)
        if r.status_code != 200:
            return info
        final_url = r.url
        final_domain = domain_of(final_url)
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

def extract_article_text(url: str, max_chars=1200) -> str:
    """
    Pull a clean text snippet for richer scripts (quotes/details).
    """
    try:
        downloaded = trafilatura.fetch_url(url, no_ssl=True, user_agent=UA_HEADERS["User-Agent"])
        if not downloaded:
            return ""
        txt = trafilatura.extract(downloaded, include_comments=False, include_tables=False, favor_recall=True)
        if not txt:
            return ""
        txt = clean_text(txt)
        return txt[:max_chars]
    except Exception:
        return ""

def virality_score(cluster) -> int:
    primary = cluster["primary"]
    hrs = min(48.0, hours_ago(primary["published_utc"]))
    recency = max(0, 100 - int((hrs/48.0) * 70))  # up to 70
    count_bonus = min(20, 5 * (len(cluster["articles"]) - 1))
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

def credible_cluster(cluster) -> bool:
    """require ≥1 top-tier domain OR ≥2 medium-tier distinct domains"""
    tops = set()
    meds = set()
    for a in cluster["articles"]:
        w = TIER_WEIGHTS.get(a["domain"], 2)
        if w >= 4: tops.add(a["domain"])
        if w >= 3: meds.add(a["domain"])
    return len(tops) >= 1 or len(meds) >= 2

def first_club_key(title: str, summary: str = "") -> str:
    t = (title + " " + (summary or "")).lower()
    for club in EPL_CLUBS:
        if club.lower() in t:
            return club
    return "OTHER"

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

            # follow to publisher, grab OG meta
            meta = fetch_meta_follow(link)
            final_url = meta["final_url"]
            dom = meta["final_domain"]

            # skip aggregators as primaries
            if dom in AGGREGATOR_DOMAINS:
                continue

            title = meta["title"] or title_feed
            title = clean_text(title)
            if len(title) < 12 or title.lower() in {"google news", "news"}:
                continue

            if not is_epl_relevant(title, meta["description"]):
                continue

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
                "category": categorize(title, meta["description"]),
                "club_key": first_club_key(title, meta["description"]),
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
                cl["club_keys"].add(it["club_key"])
                placed = True
                break
        if not placed:
            clusters.append({
                "centroid": it["norm_title"],
                "articles": [it],
                "club_keys": {it["club_key"]},
            })

    for cl in clusters:
        cl["articles"].sort(
            key=lambda a: (-TIER_WEIGHTS.get(a["domain"], 1), a["published_utc"]),
            reverse=True
        )
        cl["primary"] = cl["articles"][0]
        cl["score"] = virality_score(cl)
        cl["category"] = categorize(cl["primary"]["title"], cl["primary"].get("summary",""))
        cl["club_key"] = cl["primary"]["club_key"]

    # filter weak + enforce credibility
    clusters = [c for c in clusters if c["score"] >= SCORE_THRESHOLD and credible_cluster(c)]
    clusters.sort(key=lambda c: (c["score"], c["primary"]["published_utc"]), reverse=True)
    return clusters

def enforce_diversity(clusters, per_club_limit=2, cap=10):
    counts = {}
    out = []
    for c in clusters:
        key = c.get("club_key") or "OTHER"
        counts.setdefault(key, 0)
        if counts[key] >= per_club_limit:
            continue
        out.append(c)
        counts[key] += 1
        if len(out) >= cap:
            break
    return out

def pick_sources(cluster, k=3):
    out, seen = [], set()
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
# Step 3: writer blocks
# ============================
def openai_presenter_blocks(title, summary, sources, event_time_local, published_local, primary_snippet):
    """
    Use OpenAI Responses API if OPENAI_API_KEY is set.
    Forces 3–5 sentences, specific names/teams/dates/numbers, and quotes if present in snippet.
    """
    from openai import OpenAI
    client = OpenAI()

    prompt = f"""
You are a male football news presenter. Write a presenter-ready story for a YouTube roundup.
Audience loves football but may not know backstory; include necessary context.
STRICT: Be specific—name people, teams, competition, date, scorelines, fees, contract length if present.
If a detail isn't in the source text, write: "Not specified in the source."

Return ONLY valid JSON with keys:
- script (string, 3–5 sentences, ~90–140 words, conversational—no fluff, no jargon)
- why (list of 1–2 bullets: significance/impact)
- context (list of 1–3 bullets: standings, form, prior result, injuries, appeals, etc.)
- broll (list of 2–4 generic, non-infringing suggestions)
- lower_third (<=60 chars, punchy)

Story:
Title: {title}
Summary: {summary or "Not specified"}
Local event time: {event_time_local}
Published (UTC shown local): {published_local}

Primary article snippet (for quotes/details; do NOT invent beyond this):
{primary_snippet[:900] if primary_snippet else "No snippet available."}

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

def gemini_presenter_blocks(title, summary, sources, event_time_local, published_local, primary_snippet):
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-1.5-pro")

    prompt = f"""
You are a male football news presenter. Write a presenter-ready story for a YouTube roundup.
Be specific—names, teams, competition, dates, numbers. If missing, say "Not specified in the source."
Use 3–5 sentences, ~90–140 words.

Return JSON keys: script, why (1–2 bullets), context (1–3 bullets), broll (2–4), lower_third (<=60 chars).

Title: {title}
Summary: {summary or "Not specified"}
Local event time: {event_time_local}
Published (UTC shown local): {published_local}

Primary article snippet:
{primary_snippet[:900] if primary_snippet else "No snippet available."}

Sources:
{chr(10).join([f"- {s['domain']} — {s['link']}" for s in sources])}
"""
    resp = model.generate_content(prompt)
    txt = resp.text or ""
    m = re.search(r"\{.*\}", txt, re.S)
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

def simple_presenter_blocks(title, summary, sources, primary_snippet):
    """Stronger fallback: 4–5 sentences, concrete where possible."""
    s_title = title
    s_sum = summary or ""
    # try to pull a short quote from snippet
    quote = ""
    if primary_snippet:
        m = re.search(r"[“\"]([^”\"]{12,160})[”\"]", primary_snippet)
        if m:
            quote = f' Quote: "{clean_text(m.group(1))}".'
    script = (
        f"{s_title}. "
        f"{s_sum if s_sum else 'Details not specified in the source.'} "
        f"{'This affects squad selection or momentum.' if any(k in (s_title+s_sum).lower() for k in ['injur','ban','suspend']) else 'Implications for the table and upcoming fixtures.'}"
        f"{quote}"
    )
    # ensure 3–5 sentences
    if script.count(".") < 3:
        script += " We'll watch for official confirmation and update as new details arrive."
    why = ["Impact on standings or squad.", "High audience interest."]
    context = ["Verified across reputable outlets.", "Use generic b-roll only—no match footage."]
    broll = ["Stadium exterior and fans", "Training ground drills", "Press conference backdrop"]
    lower_third = (title[:57] + "…") if len(title) > 58 else title
    return {
        "script": script.strip(),
        "why": why[:2],
        "context": context[:3],
        "broll": broll[:4],
        "lower_third": lower_third,
    }

def estimate_seconds(text: str) -> int:
    words = max(1, len(text.split()))
    # ~165 wpm ≈ 2.75 wps
    return max(20, min(120, int(round(words / 2.75))))

# ============================
# Step 4: compose markdown + HTML
# ============================
CATEGORY_ORDER = ["Managerial Moves", "Transfers", "Injuries", "League & Regulation", "Club Updates"]

def make_markdown(clusters):
    now_utc = utcnow()
    local = to_local(now_utc, REGION_TZ)

    # Intro
    lines = []
    lines.append(f"# EPL Viral News — Presenter Pack (Last {TIME_WINDOW_HOURS}h)")
    lines.append(f"**Generated:** {local.strftime('%Y-%m-%d %H:%M')} ({REGION_TZ})  |  {now_utc.strftime('%Y-%m-%d %H:%M')} (UTC)")
    lines.append(f"**Stories in this rundown:** {len(clusters)}  |  **Style:** conversational broadcast\n")

    # We’ll fill total runtime after we build stories
    total_secs = 0

    # TL;DR (top 3)
    lines.append("## Host Script Intro")
    lines.append("Hey EPL fans—here are the **biggest stories from the last 48 hours**, ranked by virality. Let’s get into it.\n")

    lines.append("## TL;DR (15–25s)")
    for c in clusters[:3]:
        lines.append(f"- {c['primary']['title']}")
    lines.append("\n---\n")

    # Group by category
    sorted_clusters = sorted(clusters, key=lambda c: (CATEGORY_ORDER.index(c["category"]) if c["category"] in CATEGORY_ORDER else 99,
                                                      -c["score"]))
    current_cat = None

    for i, c in enumerate(sorted_clusters, start=1):
        p = c["primary"]
        event_local = to_local(p["published_utc"], REGION_TZ)
        sources = pick_sources(c, k=3)
        primary_snippet = extract_article_text(p["link"], max_chars=1000)

        # choose writer
        blocks = None
        if USE_OPENAI:
            try:
                blocks = openai_presenter_blocks(
                    p["title"], p.get("summary",""), sources,
                    event_local.strftime('%Y-%m-%d %H:%M'),
                    event_local.strftime('%Y-%m-%d %H:%M'),
                    primary_snippet
                )
            except Exception:
                blocks = None
        if blocks is None and USE_GEMINI:
            try:
                blocks = gemini_presenter_blocks(
                    p["title"], p.get("summary",""), sources,
                    event_local.strftime('%Y-%m-%d %H:%M'),
                    event_local.strftime('%Y-%m-%d %H:%M'),
                    primary_snippet
                )
            except Exception:
                blocks = None
        if blocks is None:
            blocks = simple_presenter_blocks(p["title"], p.get("summary",""), sources, primary_snippet)

        # Section header per category
        if c["category"] != current_cat:
            current_cat = c["category"]
            lines.append(f"### {current_cat}\n")

        # Runtime estimate for pacing
        secs = estimate_seconds(blocks["script"])
        total_secs += secs
        energy = "High energy" if c["score"] >= 85 else ("Measured energy" if c["score"] < 65 else "Confident, upbeat")

        lines.append(f"## Story {i}: {p['title']}")
        lines.append(f"**Slug (lower-third):** {blocks.get('lower_third') or guess_slug(p['title'])}  ")
        lines.append(f"**When:** {event_local.strftime('%Y-%m-%d %H:%M')} ({REGION_TZ})  •  **Published:** {p['published_utc'].strftime('%Y-%m-%d %H:%M')} (UTC)  ")
        lines.append(f"**Virality score:** {c['score']}  •  **Estimated runtime:** ~{secs}s\n")

        lines.append("### Presenter Script (3–5 sentences)")
        lines.append(blocks["script"] + "\n")

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
            lines.append(f"- {s['domain']} — {(s.get('title') or 'Source')}. {s['link']}")

        lines.append("\n### Host Notes (for NotebookLM)")
        lines.append(f"- Delivery: **{energy}**")
        lines.append("- Emphasize named people, teams, numbers; if a fact isn’t in the sources, say: *“Not specified in the source.”*")
        lines.append("- Suggested b-roll: " + "; ".join(blocks.get("broll", [])))
        if INCLUDE_RUMORS:
            lines.append("- Label rumors clearly as **Rumor/Report** if unconfirmed.")

        lines.append("\n---\n")

    # Outro + total time
    lines.insert(3, f"**Estimated total video length:** ~{int(round(total_secs/60.0))}m {total_secs%60:02d}s\n")
    lines.append("## Outro\nThat’s your EPL roundup for the last 48 hours. Like and subscribe for daily updates, and drop your takes in the comments.\n")
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
  ul{{padding-left:1.1rem}}
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

    print("Collecting candidates…")
    items = collect_candidates()
    print(f"Collected {len(items)} items after filtering/redirects.")

    print("Clustering…")
    clusters = cluster_items(items)
    print(f"{len(clusters)} clusters pass score/credibility.")

    print("Enforcing diversity & cap…")
    clusters = enforce_diversity(clusters, per_club_limit=PER_CLUB_LIMIT, cap=MAX_STORIES)
    print(f"Selected {len(clusters)} clusters (max {MAX_STORIES}, per-club limit {PER_CLUB_LIMIT}).")

    md = make_markdown(clusters)

    now_local = to_local(utcnow(), REGION_TZ)
    stamp = now_local.strftime("%Y-%m-%d_%H%M")
    archive_md = os.path.join(OUTPUT_DIR, f"epl-viral-news_{stamp}_CT.md")
    latest_md = os.path.join(OUTPUT_DIR, "latest.md")
    archive_html = archive_md.replace(".md", ".html")
    latest_html = os.path.join(OUTPUT_DIR, "latest.html")
    archive_txt = archive_md.replace(".md", ".txt")
    latest_txt = os.path.join(OUTPUT_DIR, "latest.txt")

    # write MD/TXT
    for path in (archive_md, latest_md, archive_txt, latest_txt):
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)

    # write HTML
    html = render_html(md)
    for path in (archive_html, latest_html):
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)

    print(f"Wrote {archive_md}, {archive_html}, {archive_txt}, {latest_md}, {latest_html}, {latest_txt}")

if __name__ == "__main__":
    main()
