# --- OpenAI helpers (put near the other imports/utilities) ---
import os, json, re, logging
from typing import Dict, Any
from datetime import datetime
from openai import OpenAI

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")  # matches your dashboard model
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "800"))

_client = None
def get_openai_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client

def _coalesce_openai_text(resp: Any) -> str:
    """
    Works with the Responses API for any model variant.
    1) use resp.output_text if present
    2) otherwise, join all text-like pieces from resp.output[*].content[*]
    Returns '' if nothing is found.
    """
    # 1) happy path on modern SDKs
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    # 2) walk the object (robust to SDK/schema drifts)
    pieces = []
    try:
        output = getattr(resp, "output", None)
        if isinstance(output, list):
            for block in output:
                if isinstance(block, dict) and block.get("type") == "message":
                    for c in block.get("content", []) or []:
                        if isinstance(c, dict):
                            # Different SDKs may call it "text" or "output_text"
                            t = c.get("text") or c.get("output_text")
                            if isinstance(t, str) and t.strip():
                                pieces.append(t.strip())
    except Exception as e:
        logging.warning("[LLM] Could not coalesce text: %r", e)

    return "\n".join(pieces).strip()

def _strip_code_fences(s: str) -> str:
    s = s.strip()
    # remove ```json ... ``` or ``` ... ```
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()

def _first_json_object(s: str) -> str:
    """
    Returns the substring of the *first complete* JSON object found in s.
    Safer than naive regex; scans braces.
    """
    s = _strip_code_fences(s)
    start = s.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i+1]
        # if we get here, start was a stray '{' – look for next
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
    needed = {"script", "why", "context", "broll", "lower_third"}
    return isinstance(d, dict) and needed.issubset(d.keys())

def _simple_fallback_story(title: str, summary: str, src_domain: str, url: str) -> Dict[str, Any]:
    # A clear, non-vague fallback for NotebookLM
    return {
        "script": (
            f"{title}. {summary if summary else 'Details not specified in the source.'} "
            f"Source: {src_domain}. Full story: {url}"
        ),
        "why": ["High interest in the last 24 hours."],
        "context": [f"Outlet: {src_domain}", "Some details not specified by the source."],
        "broll": ["Stadium exterior", "Training-ground shots", "Press conference podium"],
        "lower_third": title[:60]
    }

# --- Replace your existing function with this one ---
def openai_presenter_blocks(
    *,
    title: str,
    summary: str,
    local_event_time: str,     # string like "2025-09-09 10:31"
    published_local: str,      # string like "2025-09-09 10:31"
    primary_snippet: str,      # a quote or short excerpt; may be empty
    sources: list[tuple[str, str]],  # list of (domain, url)
    logger: logging.Logger
) -> Dict[str, Any]:
    """
    Calls OpenAI once and returns a dict with:
      script, why, context, broll, lower_third
    Falls back to a strong non-vague template if parsing fails.
    """
    client = get_openai_client()

    src_domain = sources[0][0] if sources else "Not specified"
    src_url = sources[0][1] if sources else ""

    # one, clean prompt – ask for compact JSON only
    prompt = f"""
You are a male football news presenter. Write a presenter-ready story for a YouTube roundup.
Be specific—names, teams, competition, dates, numbers. If a detail isn't in the source text, write: "Not specified in the source."

Return a *single compact JSON object* with keys exactly:
script (3–5 sentences, ~90–140 words),
why (1–2 bullets),
context (1–3 bullets),
broll (2–4 items),
lower_third (<=60 chars).

Title: {title}
Summary: {summary or 'Not specified in the source.'}
Local event time: {local_event_time}
Published (local shown from UTC): {published_local}

Primary article snippet (for quotes/details; do NOT invent beyond this):
{primary_snippet if primary_snippet.strip() else 'No snippet available.'}

Sources:
{os.linesep.join(f"- {d} — {u}" for d, u in sources)}
""".strip()

    logger.info("[LLM] OpenAI call → model=%s", OPENAI_MODEL)
    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            max_output_tokens=OPENAI_MAX_TOKENS,
        )
    except Exception as e:
        logger.exception("[LLM] Falling back from OpenAI (request error): %r", e)
        return _simple_fallback_story(title, summary, src_domain, src_url)

    text = _coalesce_openai_text(resp)
    if not text:
        logger.warning("[LLM] OpenAI returned empty text; using fallback.")
        return _simple_fallback_story(title, summary, src_domain, src_url)

    data = _safe_json_parse(text)
    if not data or not _ensure_keys(data):
        logger.warning("[LLM] Non-JSON or missing required keys; using fallback.")
        return _simple_fallback_story(title, summary, src_domain, src_url)

    # final hygiene
    data["lower_third"] = str(data.get("lower_third", title))[:60]
    return data
