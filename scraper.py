import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from urllib.parse import urljoin
from playwright.async_api import async_playwright
import requests

SITES_FILE = "sites.json"
CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

GENERIC_LABELS = {
    "click here", "view", "download", "pdf", "link",
    "details", "apply online", "read more", "apply now"
}

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(message)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=15)

def passes_filters(text, link, cfg):
    combined = f"{text} {link}".lower()
    exclude = [w.lower() for w in cfg.get("exclude_keywords", [])]
    if any(w in combined for w in exclude):
        return False
    if cfg.get("alert_mode", "all") == "filtered":
        include = [w.lower() for w in cfg.get("include_keywords", [])]
        return any(w in combined for w in include)
    return True

async def fetch_once(context, target, timeout_ms, wait_ms, min_len):
    site_name = target["name"]
    page = await context.new_page()
    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in {"image", "stylesheet", "font", "media"}
        else route.continue_()
    )
    try:
        await page.goto(target["url"], wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(wait_ms)

        raw_links = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.getAttribute('href') || '';
                if (!href || href.startsWith('#') || href.startsWith('javascript:') || href.startsWith('mailto:')) return;
                const linkText = (a.innerText || '').trim().replace(/\\s+/g, ' ');
                const container = a.closest('tr, li, .views-row, .card, article, p');
                const contextText = container ? (container.innerText || '').trim().replace(/\\s+/g, ' ') : linkText;
                results.push({ href, linkText, contextText });
            });
            return results;
        }""")

        seen_local = set()
        items = []
        for entry in raw_links:
            full_url = urljoin(target["url"], entry["href"].strip())
            link_text = entry["linkText"]
            context_text = entry["contextText"]

            if link_text.lower() in GENERIC_LABELS or len(link_text) < 15:
                label = context_text[:240]
            else:
                label = link_text[:240]

            if len(label) < min_len:
                continue

            uid = hashlib.sha256(f"{site_name}|{label}|{full_url}".encode()).hexdigest()[:16]
            if uid not in seen_local:
                seen_local.add(uid)
                items.append({"id": uid, "text": label, "link": full_url})

        return items
    finally:
        await page.close()

async def scrape_site(semaphore, context, target, seen_ids, is_first_run, cfg):
    site_name = target["name"]
    base_timeout_ms = cfg.get("page_timeout_seconds", 30) * 1000
    base_wait_ms = cfg.get("js_render_wait_seconds", 2.5) * 1000
    min_len = cfg.get("min_text_length", 10)
    max_retries = max(1, int(cfg.get("max_retries", 2)))

    async with semaphore:
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # Give retries extra time for slow JS rendering or sluggish servers
                timeout_ms = base_timeout_ms * attempt
                wait_ms = base_wait_ms * attempt

                items = await fetch_once(context, target, timeout_ms, wait_ms, min_len)

                if len(items) == 0:
                    last_error = f"⚠️ <b>{site_name}</b> returned 0 links (after {attempt} attempt(s))."
                    if attempt < max_retries:
                        await asyncio.sleep(3 * attempt)
                        continue
                    return site_name, list(seen_ids), [], last_error

                alerts = []
                for item in items:
                    if item["id"] not in seen_ids:
                        seen_ids.add(item["id"])
                        if not is_first_run and passes_filters(item["text"], item["link"], cfg):
                            safe_text = item["text"].replace("<", "&lt;").replace(">", "&gt;")
                            alerts.append(
                                f"<b>[{site_name}]</b>\n{safe_text}\n<a href=\"{item['link']}\">Open Notification</a>"
                            )

                return site_name, list(seen_ids), alerts, None

            except Exception as e:
                last_error = f"❌ <b>{site_name}</b> failed (after {attempt} attempt(s)): {str(e)[:110]}"
                if attempt < max_retries:
                    await asyncio.sleep(3 * attempt)

        return site_name, list(seen_ids), [], last_error

async def run():
    sites = load_json(SITES_FILE, [])
    cfg = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {})

    seen_map = state.get("seen", {})
    is_first_run = len(seen_map) == 0
    active_sites = [s for s in sites if s.get("enabled", True)]

    new_alerts = []
    errors = []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )

        semaphore = asyncio.Semaphore(5)
        tasks = [
            scrape_site(semaphore, context, target, set(seen_map.get(target["name"], [])), is_first_run, cfg)
            for target in active_sites
        ]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for site_name, updated_ids, alerts, err in results:
        seen_map[site_name] = updated_ids
        new_alerts.extend(alerts)
        if err:
            errors.append(err)

    state["seen"] = seen_map

    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    today_str = ist_now.strftime("%Y-%m-%d")
    heartbeat_day = cfg.get("weekly_heartbeat_day", "").strip().lower()
    send_heartbeat = (
        heartbeat_day
        and ist_now.strftime("%A").lower() == heartbeat_day
        and state.get("last_heartbeat") != today_str
    )
    if send_heartbeat:
        state["last_heartbeat"] = today_str

    save_json(STATE_FILE, state)

    if is_first_run:
        send_telegram("✅ <b>Job Notification Alert Bot Initialized</b>\nBaseline saved from sites.json.")
        return

    if new_alerts:
        chunk = "🚨 <b>New Job Notification Alert</b>\n\n"
        for alert in new_alerts:
            if len(chunk) + len(alert) > 3800:
                send_telegram(chunk)
                chunk = ""
            chunk += alert + "\n\n"
        if chunk.strip():
            send_telegram(chunk)

    if errors and cfg.get("send_health_warnings", True):
        send_telegram("🔧 <b>Scraper Health Warning</b>\n\n" + "\n".join(errors))

    if send_heartbeat:
        send_telegram(f"💓 <b>Weekly Bot Heartbeat</b>\nActively tracking <b>{len(active_sites)}</b> portals.")

if __name__ == "__main__":
    asyncio.run(run())