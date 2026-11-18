import asyncio
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
import hashlib
import json
import os
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SITES_FILE = "sites.json"
CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

NOISE_PATTERNS = [
    r"last\s+updated\s*(on|:)?\s*[\d/\-.: ]+(am|pm)?",
    r"visitor(s)?\s*(count|counter)?\s*:?\s*\d+",
    r"page\s+views?\s*:?\s*\d+",
    r"today'?s\s+date\s*:?\s*[\w\s,/\-]+",
    r"screen\s+reader\s+access",
    r"skip\s+to\s+main\s+content",
]

WAF_INDICATORS = [
    "access denied",
    "request blocked",
    "web application firewall",
    "403 forbidden",
    "503 service temporarily unavailable",
    "enable javascript and cookies to continue",
]

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_chat_ids = os.environ.get("TELEGRAM_CHAT_ID", "")
    chat_ids = [cid.strip() for cid in raw_chat_ids.split(",") if cid.strip()]

    if not token or not chat_ids:
        print(message)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for cid in chat_ids:
        try:
            requests.post(
                url,
                json={
                    "chat_id": cid,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
        except Exception as e:
            print(f"[ERROR] Telegram send to {cid} failed: {e}")

def normalize_line(line):
    s = re.sub(r"\s+", " ", line).strip()
    lower = s.lower()
    for pat in NOISE_PATTERNS:
        if re.search(pat, lower):
            return ""
    if re.fullmatch(r"[\d\s/\-.:,|•\-–—]+", s):
        return ""
    return s

def line_hash(clean_line):
    return hashlib.sha256(clean_line.lower().encode("utf-8")).hexdigest()[:16]

def is_minor_edit(candidate, old_texts, threshold):
    cand_low = candidate.lower()
    for old in old_texts:
        if abs(len(cand_low) - len(old)) > max(25, int(len(old) * 0.25)):
            continue
        if SequenceMatcher(None, cand_low, old).ratio() >= threshold:
            return True
    return False

def mentions_old_year(text, ignore_years):
    low = text.lower()
    if "2026" in low:
        return False
    return any(re.search(rf"\b{re.escape(str(yr))}\b", low) for yr in ignore_years)

def clean_extracted_lines(raw_lines):
    cleaned = []
    seen_local = set()
    for raw in raw_lines:
        c = normalize_line(raw)
        if c and c.lower() not in seen_local:
            seen_local.add(c.lower())
            cleaned.append(c)
    return cleaned

def looks_like_block_page(lines):
    joined = " ".join(lines[:15]).lower()
    return any(ind in joined for ind in WAF_INDICATORS)

def same_effective_url(original_url, final_url):
    if not final_url:
        return True
    u1 = urlparse(original_url.rstrip("/"))
    u2 = urlparse(final_url.rstrip("/"))
    return (u1.netloc.lower() == u2.netloc.lower()) and (u1.path.rstrip("/") == u2.path.rstrip("/"))

async def fetch_via_playwright(context, url, timeout_ms, wait_ms):
    page = await context.new_page()
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in {"image", "stylesheet", "font", "media"}
        else route.continue_(),
    )
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(wait_ms)
        final_url = page.url

        raw_lines = await page.evaluate("""() => {
            const clone = document.body.cloneNode(true);
            clone.querySelectorAll(
                'script, style, noscript, nav, header, footer, .navbar, .menu, #header, #footer, .breadcrumb, .sidebar, aside'
            ).forEach(el => el.remove());

            // Force-reveal hidden marquee/accordion text
            clone.querySelectorAll('[hidden], .collapse, .tab-pane, marquee').forEach(el => {
                el.removeAttribute('hidden');
                el.style.display = 'block';
            });

            const root = clone.querySelector('main, #main, .main-content, #content, .content, article') || clone;
            return (root.innerText || '')
                .split('\\n')
                .map(l => l.replace(/\\s+/g, ' ').trim())
                .filter(l => l.length > 0);
        }""")

        return clean_extracted_lines(raw_lines), final_url
    finally:
        await page.close()

def fetch_via_requests_sync(url, timeout_sec):
    """Fallback HTTP engine when headless Chrome is blocked by a portal WAF."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(url, headers=headers, timeout=timeout_sec, verify=False, allow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()
    root = soup.find(["main", "article"]) or soup.body or soup
    raw_lines = [line.strip() for line in root.get_text("\n").splitlines() if line.strip()]
    return clean_extracted_lines(raw_lines), resp.url

async def check_site(semaphore, context, target, site_state, cfg):
    site_name = target["name"]
    url = target["url"]
    base_timeout_ms = cfg.get("page_timeout_seconds", 60) * 1000
    base_wait_ms = cfg.get("js_render_wait_seconds", 4.5) * 1000
    max_retries = max(1, int(cfg.get("max_retries", 3)))
    min_chars = cfg.get("min_added_chars", 8)
    min_lines = cfg.get("min_expected_page_lines", 5)
    threshold = cfg.get("similarity_threshold", 0.90)
    ignore_years = cfg.get("ignore_years", [])

    seen_hashes = set(site_state.get("hashes", []))
    last_texts = [t.lower() for t in site_state.get("recent_texts", [])]
    prev_failures = int(site_state.get("consecutive_failures", 0))

    async with semaphore:
        last_error = "Unknown error"
        current_lines = []
        final_url = url

        for attempt in range(1, max_retries + 1):
            try:
                if attempt < max_retries:
                    current_lines, final_url = await fetch_via_playwright(
                        context, url, base_timeout_ms * attempt, base_wait_ms * attempt
                    )
                else:
                    # Final attempt falls back to direct HTTP request to bypass browser WAF blocks
                    current_lines, final_url = await asyncio.to_thread(
                        fetch_via_requests_sync, url, cfg.get("page_timeout_seconds", 60)
                    )

                if looks_like_block_page(current_lines):
                    last_error = "Blocked by firewall / WAF"
                    current_lines = []
                elif len(current_lines) < min_lines:
                    last_error = f"Incomplete load ({len(current_lines)} lines)"
                    current_lines = []
                else:
                    break
            except Exception as e:
                err_msg = str(e).splitlines()[0][:85]
                last_error = err_msg
                current_lines = []

            if attempt < max_retries:
                await asyncio.sleep(3 * attempt)

        # Handle persistent failure for this run without wiping existing baseline
        if len(current_lines) == 0:
            fail_count = prev_failures + 1
            updated_state = {
                "hashes": list(seen_hashes),
                "recent_texts": site_state.get("recent_texts", []),
                "consecutive_failures": fail_count,
            }
            return {
                "name": site_name,
                "url": url,
                "status": "failed",
                "fail_count": fail_count,
                "error": last_error,
                "state": updated_state,
            }

        redirected_to = None if same_effective_url(url, final_url) else final_url
        site_is_new = len(seen_hashes) == 0
        new_lines = []

        for line in current_lines:
            h = line_hash(line)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            if site_is_new:
                continue
            if len(line) < min_chars:
                continue
            if mentions_old_year(line, ignore_years):
                continue
            if is_minor_edit(line, last_texts, threshold):
                continue

            new_lines.append(line)

        updated_state = {
            "hashes": list(seen_hashes)[-4000:],
            "recent_texts": current_lines[:600],
            "consecutive_failures": 0,
        }

        if site_is_new:
            status = "seeded"
        elif new_lines:
            status = "updated"
        else:
            status = "unchanged"

        return {
            "name": site_name,
            "url": url,
            "status": status,
            "new_lines": new_lines,
            "redirected_to": redirected_to,
            "state": updated_state,
        }

async def run():
    sites = load_json(SITES_FILE, [])
    cfg = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {})

    sites_state = state.get("sites", {})
    active_sites = [s for s in sites if s.get("enabled", True)]
    max_preview = cfg.get("max_preview_lines_per_site", 4)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            ignore_https_errors=True,
            locale="en-IN",
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )

        semaphore = asyncio.Semaphore(5)
        tasks = [
            check_site(semaphore, context, target, sites_state.get(target["name"], {}), cfg)
            for target in active_sites
        ]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for res in results:
        sites_state[res["name"]] = res["state"]

    state["sites"] = sites_state
    save_json(STATE_FILE, state)

    # Build structured twice-daily Telegram report
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    timestamp = ist_now.strftime("%d %b %Y, %I:%M %p IST")

    updated_list = [r for r in results if r["status"] == "updated"]
    unchanged_list = [r for r in results if r["status"] == "unchanged"]
    seeded_list = [r for r in results if r["status"] == "seeded"]
    failed_list = [r for r in results if r["status"] == "failed"]

    sections = []

    if updated_list:
        header = f"🚨 <b>ACTION REQUIRED: NEW UPDATES DETECTED</b>\n🕒 <i>{timestamp}</i>"
        lines = [f"<b>🆕 NEW UPDATES ({len(updated_list)})</b>"]
        for r in updated_list:
            entry = f"• <b>{escape_html(r['name'])}</b> — <a href=\"{r['url']}\">Open Site</a>"
            if r.get("redirected_to"):
                entry += f"\n  ↪️ <i>Redirected to:</i> <a href=\"{r['redirected_to']}\">{escape_html(r['redirected_to'])}</a>"
            for new_line in r["new_lines"][:max_preview]:
                entry += f"\n  ↳ <code>{escape_html(new_line[:160])}</code>"
            extra = len(r["new_lines"]) - max_preview
            if extra > 0:
                entry += f"\n  ↳ <i>(+{extra} more new line(s))</i>"
            lines.append(entry)
        sections.append(header + "\n\n" + "\n\n".join(lines))
    else:
        sections.append(f"📊 <b>Job Portal Status Report</b>\n🕒 <i>{timestamp}</i>")

    if seeded_list:
        lines = [f"<b>📥 BASELINE SAVED ({len(seeded_list)})</b>"]
        for r in seeded_list:
            lines.append(f"• {escape_html(r['name'])}")
        sections.append("\n".join(lines))

    if unchanged_list:
        lines = [f"<b>✅ NOTHING NEW ({len(unchanged_list)})</b>"]
        for r in unchanged_list:
            item = f"• {escape_html(r['name'])}"
            if r.get("redirected_to"):
                item += f" <i>(↪️ redirected to {escape_html(r['redirected_to'])})</i>"
            lines.append(item)
        sections.append("\n".join(lines))

    if failed_list:
        lines = [f"<b>❌ FAILED / UNREACHABLE ({len(failed_list)})</b>"]
        for r in failed_list:
            streak = f" [Failed {r['fail_count']}x in a row]" if r["fail_count"] > 1 else ""
            lines.append(
                f"• <b>{escape_html(r['name'])}</b>{streak} — <i>{escape_html(r['error'])}</i>"
            )
        sections.append("\n".join(lines))

    # Send cleanly in chunks under Telegram's 4096-character limit
    chunk = ""
    for sec in sections:
        if len(chunk) + len(sec) + 2 > 3800:
            send_telegram(chunk.strip())
            chunk = ""
        chunk += sec + "\n\n"
    if chunk.strip():
        send_telegram(chunk.strip())

if __name__ == "__main__":
    asyncio.run(run())