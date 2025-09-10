import os
import re
import json
import logging
import traceback
from typing import Any, Dict, List, Tuple
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
TIME_WINDOW_HOURS = int(os.getenv("TIME_WINDOW_HOURS", "24"))   # your request: 24h window
MAX_STORIES = int(os.getenv("MAX_STORIES", "10"))
PER_CLUB_LIMIT = int(os.getenv("PER_CLUB_LIMIT", "2"))
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "45"))
INCLUDE_RUMORS = os.getenv("INCLUDE_RUMORS", "false").lower() == "true"
REGION_TZ = os.getenv("REGION_TZ", "America/Winnipeg")          # your request: Winnipeg

# LLM controls
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "1000"))
USE_OPENAI = bool(OPENAI_KEY)
USE_GEMINI = bool(os.getenv("GEMINI_API_KEY"))
PRIMARY_SNIPPET_CHARS = int(os.getenv("PRIMARY_SNIPPET_CHARS", "1000"))

# Output controls
OUTPUT_DIR = "docs"
ARCHIVE_ENABLED = os.getenv("ARCHIVE_ENABLED", "true").lower() == "true"
WRITE_TXT = os.getenv("WRITE_TXT", "true").lower() == "true"

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EPL-Pipeline/2.3)"}
MAX_PER_FEED = 20

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

TIER_WEIGHTS = {
    "premierleague.com": 5,
    "bbc.co.uk": 5, "bbc.com": 5,
    "skysports.com": 5,
    "theguardian.com": 4,
    "reuters.com": 4,
    "apnews.com": 4,
    "espn.com": 3,
    # club sites
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
    "Arsenal Premier League", "Chelsea Premier League", "Liverpool Premier League",
    "Manchester United Premier League", "Manchester City Premier League",
    "Tottenham Premier League", "Newcastle United Premier League",
]

def google_news_rss(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-GB&gl=GB&ceid=GB:en"

RSS_FEEDS = [google_news_rss(q) for q in GN_QUERIES] + [
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.theguardian.com/football/rss",
    "https://www.skysports.com/rss/12040",
    "https://www.reuters.com/subjects/soccer/rss",
    "https://www.espn.com/espn/rss/soccer/news",
]

# ============================
# Logging
# ============================
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("pipeline").info

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
    elif any(k in up for k in ["MANAGER","COACH","APPOINT","SACK"]): topic = "MANAGERIAL"
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
    recency = max(0, 100 - int((hrs/48.0) * 70))
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
# Step 1: collect candidates
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

            meta = fetch_meta_follow(link)
            final_url = meta["final_url"]
            dom = meta["final_domain"]

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
# Step 2: cluster & select
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
# Step 3: OpenAI writer (JSON schema) → Gemini → fallback
# ============================
def _coalesce_openai_text(resp: Any) -> str:
    """Collect text from Responses API result, robust across SDK variants."""
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    pieces: List[str] = []
    try:
        output = getattr(resp, "output", None)
        if isinstance(output, list):
            for block in output:
                if isinstance(block, dict) and block.get("type") == "message":
                    for c in block.get("content", []) or []:
                        if isinstance(c, dict):
                            t = c.get("text") or c.get("output_text")
                            if isinstance(t, str) and t.strip():
                                pieces.append(t.strip())
    except Exception as e:
        logging.warning("[LLM] Could not coalesce text: %r", e)
    return "\n".join(pieces).strip()

def _first_json_object(s: str) -> str:
    """Find the first complete JSON object in s."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{": depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i+1]
        start = s.find("{", start + 1)
    return ""

def _safe_json_parse(text: str) -> Dict[str, Any] | None:
    block = _first_json_object(text)
    if not block:
        return None
    try:
        return json.loads(block)
    except Exception:
        return None

def _ensure_keys(d: Dict[str, Any]) -> bool:
    req = {"script","why","context","broll","lower_third"}
    return isinstance(d, dict) and req.issubset(d.keys())

def openai_presenter_blocks(story_idx: int, title: str, summary: str, sources: List[Dict[str,Any]],
                            event_time_local: str, published_local: str, primary_snippet: str) -> Dict[str, Any] | None:
    if not USE_OPENAI:
        return None
    try:
        from openai import OpenAI
    except Exception as e:
        log(f"[LLM][story {story_idx}] OpenAI SDK import failed → fallback. Error: {e!r}")
        return None

    client = OpenAI(api_key=OPENAI_KEY)

    src_pairs: List[Tuple[str,str]] = [(s["domain"], s["link"]) for s in sources]
    src_domain = src_pairs[0][0] if src_pairs else "Not specified"
    src_url = src_pairs[0][1] if src_pairs else ""

    base_prompt = f"""
You are a male football news presenter. Write a presenter-ready story for a YouTube roundup.
Be specific—names, teams, competition, dates, numbers. If a detail isn't in the source text, write: "Not specified in the source."

Return a single JSON object with keys exactly:
script (3–5 sentences, ~90–140 words),
why (1–2 bullets),
context (1–3 bullets),
broll (2–4 items),
lower_third (<=60 chars).

Title: {title}
Summary: {summary or 'Not specified in the source.'}
Local event time: {event_time_local}
Published (local shown from UTC): {published_local}

Primary article snippet (for quotes/details; do NOT invent beyond this):
{(primary_snippet or 'No snippet available.')[:PRIMARY_SNIPPET_CHARS]}

Sources:
{os.linesep.join(f"- {d} — {u}" for d, u in src_pairs)}
""".strip()

    schema = {
        "type": "object",
        "properties": {
            "script": {"type": "string"},
            "why": {"type": "array", "items": {"type": "string"}},
            "context": {"type": "array", "items": {"type": "string"}},
            "broll": {"type": "array", "items": {"type": "string"}},
            "lower_third": {"type": "string"}
        },
        "required": ["script","why","context","broll","lower_third"],
        "additionalProperties": False
    }

    def _call(prompt: str):
        log(f"[LLM][story {story_idx}] OpenAI call → model={OPENAI_MODEL}")
        return client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            max_output_tokens=OPENAI_MAX_TOKENS,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "Story", "schema": schema, "strict": True}
            },
        )

    # Attempt 1
    try:
        resp = _call(base_prompt)
    except Exception as e:
        log(f"[LLM][story {story_idx}] OpenAI request error → fallback. {e!r}")
        return None

    text = _coalesce_openai_text(resp)
    data = _safe_json_parse(text)
    if not data or not _ensure_keys(data):
        # Log a short preview for debugging
        preview = (text or "")[:300].replace("\n", " ")
        log(f"[LLM][story {story_idx}] Non-JSON or missing keys (preview): {preview}")

        # Attempt 2: minimal prompt that insists on JSON only
        retry_prompt = base_prompt + "\n\nIMPORTANT: Reply with ONLY valid JSON matching the schema. No prose."
        try:
            resp2 = _call(retry_prompt)
            text2 = _coalesce_openai_text(resp2)
            data = _safe_json_parse(text2)
        except Exception as e:
            log(f"[LLM][story {story_idx}] Retry failed → fallback. {e!r}")
            return None

    if not data or not _ensure_keys(data):
        log(f"[LLM][story {story_idx}] OpenAI JSON still invalid → fallback.")
        return None

    # Final hygiene
    data["script"] = (data.get("script") or "").strip()
    data["why"] = [clean_text(x) for x in data.get("why", [])][:2]
    data["context"] = [clean_text(x) for x in data.get("context", [])][:3]
    data["broll"] = [clean_text(x) for x in data.get("broll", [])][:4]
    data["lower_third"] = clean_text(data.get("lower_third", title))[:60]
    return data

def gemini_presenter_blocks(story_idx, title, summary, sources, event_time_local, published_local, primary_snippet):
    if not USE_GEMINI:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        model = genai.GenerativeModel("gemini-1.5-pro")

        prompt = f"""
You are a male football news presenter. Write a presenter-ready story for a YouTube roundup.
Be specific—names, teams, competition, dates, numbers. If missing, say "Not specified in the source."
Return JSON keys: script, why, context, broll, lower_third.

Title: {title}
Summary: {summary or "Not specified"}
Local event time: {event_time_local}
Published (local shown from UTC): {published_local}

Primary article snippet:
{(primary_snippet or "No snippet available.")[:PRIMARY_SNIPPET_CHARS]}

Sources:
{chr(10).join([f"- {s['domain']} — {s['link']}" for s in sources])}
"""
        log(f"[LLM][story {story_idx}] Gemini call → model=gemini-1.5-pro")
        resp = model.generate_content(prompt)
        txt = getattr(resp, "text", "") or ""
        data = _safe_json_parse(txt)
        if not data or not _ensure_keys(data):
            log(f"[LLM][story {story_idx}] Gemini returned invalid JSON → fallback.")
            return None

        data["script"] = (data.get("script") or "").strip()
        data["why"] = [clean_text(x) for x in data.get("why", [])][:2]
        data["context"] = [clean_text(x) for x in data.get("context", [])][:3]
        data["broll"] = [clean_text(x) for x in data.get("broll", [])][:4]
        data["lower_third"] = clean_text(data.get("lower_third", title))[:60]
        return data
    except Exception as e:
        log(f"[LLM][story {story_idx}] Gemini ERROR → fallback. {e.__class__.__name__}: {e}")
        return None

def simple_presenter_blocks(title, summary, primary_snippet):
    quote = ""
    if primary_snippet:
        m = re.search(r"[“\"]([^”\"]{12,160})[”\"]", primary_snippet)
        if m: quote = f' Quote: "{clean_text(m.group(1))}".'
    s_sum = summary or "Details not specified in the source."
    base = f"{title}. {s_sum}"
    if "injur" in (title+s_sum).lower():
        impact = "Potential impact on the lineup and upcoming fixtures."
    elif any(k in (title+s_sum).lower() for k in ["ban","suspend","disciplin"]):
        impact = "Disciplinary outcome could affect availability."
    elif any(k in (title+s_sum).lower() for k in ["transfer","contract","fee","loan"]):
        impact = "Implications for the squad and table."
    else:
        impact = "Relevance to standings and momentum."
    script = f"{base} {impact}{quote}"
    if script.count(".") < 3:
        script += " We'll watch for official confirmation and update as details emerge."
    lower_third = (title[:57] + "…") if len(title) > 58 else title
    return {
        "script": script.strip(),
        "why": ["Impact on standings or squad."],
        "context": ["Verified across reputable outlets."],
        "broll": ["Stadium exterior and fans","Training ground drills","Press conference backdrop"],
        "lower_third": lower_third,
    }

def estimate_seconds(text: str) -> int:
    words = max(1, len(text.split()))
    return max(20, min(120, int(round(words / 2.75))))

# ============================
# Step 4: compose markdown + HTML
# ============================
CATEGORY_ORDER = ["Managerial Moves", "Transfers", "Injuries", "League & Regulation", "Club Updates"]

def make_markdown(clusters):
    now_utc = utcnow()
    local = to_local(now_utc, REGION_TZ)

    lines: List[str] = []
    lines.append(f"# EPL Viral News — Presenter Pack (Last {TIME_WINDOW_HOURS}h)")
    lines.append(f"**Generated:** {local.strftime('%Y-%m-%d %H:%M')} ({REGION_TZ})  |  {now_utc.strftime('%Y-%m-%d %H:%M')} (UTC)")
    lines.append(f"**Stories in this rundown:** {len(clusters)}  |  **Style:** conversational broadcast\n")

    total_secs = 0
    lines.append("## Host Script Intro")
    lines.append("Hey EPL fans—here are the **biggest stories from the last 24 hours**, ranked by virality. Let’s get into it.\n")

    lines.append("## TL;DR (15–25s)")
    for c in clusters[:3]:
        lines.append(f"- {c['primary']['title']}")
    lines.append("\n---\n")

    sorted_clusters = sorted(
        clusters,
        key=lambda c: (CATEGORY_ORDER.index(c["category"]) if c["category"] in CATEGORY_ORDER else 99, -c["score"])
    )
    current_cat = None

    for i, c in enumerate(sorted_clusters, start=1):
        p = c["primary"]
        event_local = to_local(p["published_utc"], REGION_TZ)
        sources = pick_sources(c, k=3)
        primary_snippet = extract_article_text(p["link"], max_chars=PRIMARY_SNIPPET_CHARS)

        blocks = openai_presenter_blocks(
            i, p["title"], p.get("summary",""), sources,
            event_local.strftime('%Y-%m-%d %H:%M'),
            event_local.strftime('%Y-%m-%d %H:%M'),
            primary_snippet
        ) if USE_OPENAI else None
        if blocks is None and USE_GEMINI:
            blocks = gemini_presenter_blocks(
                i, p["title"], p.get("summary",""), sources,
                event_local.strftime('%Y-%m-%d %H:%M'),
                event_local.strftime('%Y-%m-%d %H:%M'),
                primary_snippet
            )
        if blocks is None:
            log(f"[LLM][story {i}] Using SIMPLE fallback writer.")
            blocks = simple_presenter_blocks(p["title"], p.get("summary",""), primary_snippet)

        if c["category"] != current_cat:
            current_cat = c["category"]
            lines.append(f"### {current_cat}\n")

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

    lines.insert(3, f"**Estimated total video length:** ~{int(round(total_secs/60.0))}m {total_secs%60:02d}s\n")
    lines.append("## Outro\nThat’s your EPL roundup for the last 24 hours. Like and subscribe for daily updates, and drop your takes in the comments.\n")
    return "\n".join(lines)

def render_html(md_text: str, title="EPL Viral News — Presenter Pack") -> str:
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

    log(f"[env] OPENAI_KEY_PRESENT={bool(OPENAI_KEY)} OPENAI_MODEL={OPENAI_MODEL} GEMINI_KEY_PRESENT={USE_GEMINI}")
    log(f"[env] Window={TIME_WINDOW_HOURS}h MaxStories={MAX_STORIES} PerClub={PER_CLUB_LIMIT} Score≥{SCORE_THRESHOLD} SnippetChars={PRIMARY_SNIPPET_CHARS}")
    log(f"[env] ARCHIVE_ENABLED={ARCHIVE_ENABLED} WRITE_TXT={WRITE_TXT}")

    log("Collecting candidates…")
    items = collect_candidates()
    log(f"Collected {len(items)} items after filtering/redirects.")

    log("Clustering…")
    clusters = cluster_items(items)
    log(f"{len(clusters)} clusters pass score/credibility.")

    log("Enforcing diversity & cap…")
    clusters = enforce_diversity(clusters, per_club_limit=PER_CLUB_LIMIT, cap=MAX_STORIES)
    log(f"Selected {len(clusters)} clusters (max {MAX_STORIES}, per-club limit {PER_CLUB_LIMIT}).")

    md = make_markdown(clusters)

    now_local = to_local(utcnow(), REGION_TZ)
    stamp = now_local.strftime("%Y-%m-%d_%H%M")
    archive_md = os.path.join(OUTPUT_DIR, f"epl-viral-news_{stamp}_CT.md")
    latest_md = os.path.join(OUTPUT_DIR, "latest.md")
    archive_html = archive_md.replace(".md", ".html")
    latest_html = os.path.join(OUTPUT_DIR, "latest.html")
    archive_txt = archive_md.replace(".md", ".txt")
    latest_txt = os.path.join(OUTPUT_DIR, "latest.txt")

    # Write latest
    with open(latest_md, "w", encoding="utf-8") as f:
        f.write(md)
    if WRITE_TXT:
        with open(latest_txt, "w", encoding="utf-8") as f:
            f.write(md)
    html = render_html(md)
    with open(latest_html, "w", encoding="utf-8") as f:
        f.write(html)

    # Optionally write archive copies
    if ARCHIVE_ENABLED:
        with open(archive_md, "w", encoding="utf-8") as f:
            f.write(md)
        if WRITE_TXT:
            with open(archive_txt, "w", encoding="utf-8") as f:
                f.write(md)
        with open(archive_html, "w", encoding="utf-8") as f:
            f.write(html)

    log(f"Wrote {latest_md}, {latest_html}" + (f", {latest_txt}" if WRITE_TXT else ""))
    if ARCHIVE_ENABLED:
        log(f"Wrote archives: {archive_md}, {archive_html}" + (f", {archive_txt}" if WRITE_TXT else ""))

if __name__ == "__main__":
    main()
