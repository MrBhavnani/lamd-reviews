"""
Scrape Google Maps reviews for one business and keep only those that mention a keyword.

Output: reviews.json  (read by the Wix embed)

Config (environment variables):
  MAPS_URL      Google Maps link to the listing (a share.google link works)   [required]
  KEYWORD       Word to match, case-insensitive, whole word                    [default: Rajesh]
  OUTPUT        Output file path                                               [default: reviews.json]
  MAX_SCROLLS   Safety cap on scroll iterations                                [default: 400]
  HEADLESS      "0" to watch the browser locally                               [default: 1]
  CHROME_PATH   Optional path to a Chrome binary (local testing)

Exit codes:
  0  success (file written)
  1  scrape failed or loaded zero reviews -> existing reviews.json is left untouched
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

MAPS_URL = os.environ.get("MAPS_URL", "").strip()
KEYWORD = os.environ.get("KEYWORD", "Rajesh").strip()
OUTPUT = Path(os.environ.get("OUTPUT", "reviews.json"))
MAX_SCROLLS = int(os.environ.get("MAX_SCROLLS", "400"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
CHROME_PATH = os.environ.get("CHROME_PATH") or None
DEBUG_DIR = Path("debug")

KW_RE = re.compile(r"\b" + re.escape(KEYWORD) + r"\b", re.IGNORECASE)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def with_english(url: str) -> str:
    """Force English UI so labels/dates are predictable."""
    if "google." in url and "hl=" not in url:
        url += ("&" if "?" in url else "?") + "hl=en"
    return url


# ---------------------------------------------------------------- page steps

def handle_consent(page):
    """EU-style cookie wall. Usually absent on US runners."""
    if "consent." not in page.url:
        return
    log("Consent page detected - rejecting non-essential cookies")
    for label in ["Reject all", "Accept all", "I agree"]:
        btn = page.get_by_role("button", name=re.compile(label, re.I))
        if btn.count():
            btn.first.click()
            page.wait_for_load_state("domcontentloaded")
            return


CID_PATTERNS = [
    r"ludocid(?:=|\\u003d|%3D)(\d{6,})",
    r"[?&]cid=(\d{6,})",
    r"0x[0-9a-fA-F]+:0x([0-9a-fA-F]{6,})",
]


def resolve_to_maps(page):
    """share.google links often open a Google Search business panel, not Maps.
    Find the listing's CID on that page and jump to its Maps page."""
    if "/maps" in page.url and "/sorry/" not in page.url:
        return

    # Google Search often blocks datacenter IPs (GitHub runners) with a CAPTCHA.
    # The blocked URL still carries the knowledge-graph id + business name, so go to Maps instead.
    from urllib.parse import unquote, parse_qs, urlparse, quote_plus
    target = page.url
    if "/sorry/" in target:
        target = unquote(parse_qs(urlparse(target).query).get("continue", [""])[0])
    qs = parse_qs(urlparse(target).query)
    kgmid = (qs.get("kgmid") or [""])[0]
    name = (qs.get("q") or [""])[0]
    if kgmid or name:
        tries = []
        if kgmid:
            tries.append(f"https://www.google.com/maps?kgmid={quote_plus(kgmid)}&hl=en")
        if name:
            tries.append(f"https://www.google.com/maps/search/{quote_plus(name)}?hl=en")
        for url in tries:
            log(f"Trying Maps directly: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            handle_consent(page)
            page.wait_for_timeout(4000)
            if "/sorry/" in page.url:
                log("  Maps also returned a CAPTCHA")
                continue
            # A search may show a results list instead of the place; open the first result
            if not page.locator("h1").count():
                first = page.locator('a[href*="/maps/place/"]')
                if first.count():
                    first.first.click()
                    page.wait_for_timeout(4000)
            h1 = page.locator("h1")
            if h1.count() and h1.first.inner_text().strip().lower() not in ("this area", "results", ""):
                log(f"Maps listing opened: {page.url[:160]}")
                return
        raise RuntimeError("Google blocked the request with a CAPTCHA on both Search and Maps")
    html = page.content()
    for i, pat in enumerate(CID_PATTERNS):
        m = re.search(pat, html)
        if m:
            cid = m.group(1)
            if i == 2:  # hex feature id -> decimal CID
                cid = str(int(cid, 16))
            url = f"https://www.google.com/maps?cid={cid}&hl=en"
            log(f"Search panel detected - opening Maps listing via CID {cid}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            handle_consent(page)
            page.wait_for_timeout(2500)
            log(f"Now on: {page.url[:160]}")
            return
    # Last resort: click any link into Maps
    link = page.locator('a[href*="/maps/place/"], a[href*="maps.google.com"]')
    if link.count():
        href = link.first.get_attribute("href")
        if href.startswith("/"):
            href = "https://www.google.com" + href
        log(f"Following Maps link: {href[:120]}")
        page.goto(with_english(href), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        return
    log("Could not find a Maps link or CID on the landing page")


def open_reviews_tab(page):
    candidates = [
        page.get_by_role("tab", name=re.compile(r"^Reviews", re.I)),
        page.locator('button[aria-label^="Reviews for"]'),
        page.locator('button[jsaction*="reviewChart"]'),
        page.get_by_role("button", name=re.compile(r"[\d,]+\s+reviews?", re.I)),
    ]
    for loc in candidates:
        try:
            if loc.count():
                loc.first.click(timeout=5000)
                page.wait_for_selector("div[data-review-id]", timeout=15000)
                log("Reviews tab opened")
                return True
        except PWTimeout:
            continue
    return page.locator("div[data-review-id]").count() > 0


def get_past_limited_view(page):
    """Signed-out visitors get a 'limited view' with ~5 reviews.
    1) Try the 'See more reviews' button.  2) Fall back to searching reviews for the keyword.
    Returns True if we ended up in keyword-search mode."""
    if page.evaluate(COUNT_JS) > 10:
        return False
    log("Limited view detected (few reviews visible)")
    start_url = page.url

    more = page.locator('button:has-text("See more reviews"), a:has-text("See more reviews")')
    if more.count():
        try:
            more.first.click(timeout=5000)
            page.wait_for_timeout(4000)
            log(f"  clicked 'See more reviews' -> {page.url[:140]}  ({page.evaluate(COUNT_JS)} reviews visible)")
            if "accounts.google" in page.url or "/sorry/" in page.url:
                log("  that requires sign-in; going back")
                page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                open_reviews_tab(page)
            elif page.evaluate(COUNT_JS) > 10:
                return False
        except Exception as e:
            log(f"  'See more reviews' click failed: {e}")

    try:
        page.locator('button[aria-label="Search reviews"]').first.click(timeout=5000)
        box = page.locator('input[aria-label*="Search reviews" i], input[placeholder*="Search reviews" i]')
        box.first.wait_for(timeout=5000)
        box.first.fill(KEYWORD)
        box.first.press("Enter")
        page.wait_for_timeout(4000)
        n = page.evaluate(COUNT_JS)
        log(f"  searched reviews for {KEYWORD!r}: {n} results visible (url {page.url[:120]})")
        return True
    except Exception as e:
        log(f"  review search failed: {e}")
    return False


def sort_newest(page):
    """Best-effort: sorting by newest makes new reviews load first."""
    try:
        sort_btn = page.locator('button[aria-label*="Sort"], button[data-value="Sort"]')
        if not sort_btn.count():
            return
        sort_btn.first.click(timeout=4000)
        item = page.get_by_role("menuitemradio", name=re.compile("Newest", re.I))
        item.first.click(timeout=4000)
        page.wait_for_timeout(2000)
        log("Sorted by newest")
    except Exception:
        log("Sort control not found - continuing with default order")


SCROLL_JS = """
() => {
  const first = document.querySelector('div[data-review-id]');
  if (!first) return -1;
  let el = first.parentElement;
  while (el && el !== document.body) {
    const s = getComputedStyle(el);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 10) {
      el.scrollTop = el.scrollHeight;
      return el.scrollHeight;
    }
    el = el.parentElement;
  }
  window.scrollTo(0, document.body.scrollHeight);
  return document.body.scrollHeight;
}
"""

COUNT_JS = """
() => new Set([...document.querySelectorAll('div[data-review-id]')].map(e => e.getAttribute('data-review-id'))).size
"""

EXPAND_JS = """
() => {
  let n = 0;
  document.querySelectorAll('button[aria-label="See more"], button.w8nwRe, button[jsaction*="review.expandReview"]').forEach(b => {
    try { b.click(); n++; } catch (e) {}
  });
  return n;
}
"""

EXTRACT_JS = r"""
() => {
  // Group nodes by review id, keep the outermost node for each id.
  const byId = new Map();
  document.querySelectorAll('div[data-review-id]').forEach(el => {
    const id = el.getAttribute('data-review-id');
    const prev = byId.get(id);
    if (!prev || el.contains(prev)) byId.set(id, el);
  });

  const out = [];
  byId.forEach((el, id) => {
    const q = s => el.querySelector(s);

    // Author
    let author = (q('.d4r55') || {}).textContent || el.getAttribute('aria-label') || '';
    author = author.trim();

    // Author profile link + photo
    const contrib = q('[data-href*="/maps/contrib/"]') || q('a[href*="/maps/contrib/"]');
    const authorUrl = contrib ? (contrib.getAttribute('data-href') || contrib.getAttribute('href') || '') : '';
    const img = (contrib && contrib.querySelector('img')) || q('img[src*="googleusercontent"]');
    const photo = img ? img.getAttribute('src') || '' : '';

    // Rating: "5 stars" aria-label, or "5/5" text for some layouts
    let rating = null;
    const starEl = q('[role="img"][aria-label*="star"]');
    if (starEl) {
      const m = starEl.getAttribute('aria-label').match(/(\d(?:\.\d)?)/);
      if (m) rating = parseFloat(m[1]);
    }
    if (rating === null) {
      const m = (el.textContent || '').match(/\b([1-5])\/5\b/);
      if (m) rating = parseInt(m[1], 10);
    }

    // Date (relative text from Google, e.g. "3 months ago")
    const dateEl = q('.rsqaWe') || q('.xRkPPb');
    let date = dateEl ? dateEl.textContent.trim() : '';
    date = date.replace(/\s*on\s*Google\s*$/i, '').trim();

    // Review body: first .wiI7pd that is NOT inside the owner's response
    let text = '';
    for (const t of el.querySelectorAll('.wiI7pd')) {
      if (!t.closest('.CDe7pd')) { text = t.textContent.trim(); break; }
    }

    // Owner response (optional)
    const resp = q('.CDe7pd .wiI7pd');
    const ownerResponse = resp ? resp.textContent.trim() : '';

    out.push({ id, author, author_url: authorUrl, photo, rating, date, text, owner_response: ownerResponse });
  });
  return out;
}
"""

PLACE_JS = r"""
() => {
  const name = (document.querySelector('h1') || {}).textContent || '';
  let rating = null, total = null;
  const header = document.querySelector('div.F7nice') || document.querySelector('[role="main"]');
  if (header) {
    const t = header.textContent || '';
    const r = t.match(/(\d\.\d)/); if (r) rating = parseFloat(r[1]);
    const c = t.match(/\(([\d,]+)\)/) || t.match(/([\d,]+)\s+reviews?/i);
    if (c) total = parseInt(c[1].replace(/,/g, ''), 10);
  }
  return { name: name.trim(), rating, total };
}
"""


def scroll_all(page):
    last, stalls = -1, 0
    for i in range(MAX_SCROLLS):
        page.evaluate(SCROLL_JS)
        # Also scroll with a real mouse wheel over the last review (some layouts only react to wheel events)
        try:
            box = page.locator("div[data-review-id]").last.bounding_box()
            if box:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 50))
                page.mouse.wheel(0, 4000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        n = page.evaluate(COUNT_JS)
        if n == last:
            stalls += 1
            if stalls >= 5:
                break
        else:
            stalls = 0
            last = n
        if i % 10 == 0:
            log(f"  loaded {n} reviews so far")
    return last


# ---------------------------------------------------------------- main

def scrape():
    if not MAPS_URL:
        log("MAPS_URL is not set")
        sys.exit(1)

    with sync_playwright() as p:
        launch_args = {"headless": HEADLESS, "args": ["--lang=en-US"]}
        if CHROME_PATH:
            launch_args["executable_path"] = CHROME_PATH
        browser = p.chromium.launch(**launch_args)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            locale="en-US",
            timezone_id="America/Los_Angeles",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        try:
            log(f"Opening {MAPS_URL}")
            page.goto(with_english(MAPS_URL), wait_until="domcontentloaded", timeout=60000)
            handle_consent(page)
            page.wait_for_timeout(2500)
            log(f"Landed on: {page.url[:160]}  (title: {page.title()[:80]!r})")
            resolve_to_maps(page)
            # make sure we end on a Maps page in English
            if "/maps" in page.url and "hl=en" not in page.url:
                page.goto(with_english(page.url), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("h1", timeout=45000)
            page.wait_for_timeout(2500)
            maps_url = page.url.split("?")[0]

            place = page.evaluate(PLACE_JS)
            log(f"Listing: {place['name']!r}  rating={place['rating']}  total={place['total']}")

            if not open_reviews_tab(page):
                raise RuntimeError("Could not open the Reviews tab (layout may have changed)")
            sort_newest(page)
            place["search_mode"] = get_past_limited_view(page)

            loaded = scroll_all(page)
            log(f"Finished scrolling: {loaded} reviews loaded")
            if place.get("total") and loaded < place["total"] * 0.5:
                # Diagnose why: look for Google's "limited view" notice and dump nearby text
                txt = re.sub(r"\s+", " ", page.inner_text("body"))
                for kw in ["limited", "sign in", "Sign in to", "unusual", "More reviews"]:
                    k = txt.lower().find(kw.lower())
                    if k >= 0:
                        log(f"  page text near {kw!r}: ...{txt[max(0,k-150):k+200]}...")
                log(f"  buttons on page: {page.eval_on_selector_all('button[aria-label]', 'els => els.map(e => e.getAttribute(\"aria-label\")).filter(Boolean).slice(0,40)')}")
            for _ in range(3):
                if page.evaluate(EXPAND_JS) == 0:
                    break
                page.wait_for_timeout(800)

            reviews = page.evaluate(EXTRACT_JS)
        except Exception as e:
            DEBUG_DIR.mkdir(exist_ok=True)
            try:
                log(f"Failure URL: {page.url[:200]}")
                log(f"Failure title: {page.title()[:120]!r}")
                body = re.sub(r"\s+", " ", page.inner_text("body"))[:600]
                log(f"Failure page text: {body}")
            except Exception:
                pass
            try:
                page.screenshot(path=str(DEBUG_DIR / "failure.png"), full_page=True)
                (DEBUG_DIR / "failure.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            log(f"SCRAPE FAILED: {e}")
            sys.exit(1)
        finally:
            browser.close()

    return place, maps_url, reviews


def main():
    place, maps_url, all_reviews = scrape()

    if not all_reviews:
        log("Zero reviews extracted - refusing to overwrite reviews.json")
        sys.exit(1)

    matches = [r for r in all_reviews if r["text"] and KW_RE.search(r["text"])]
    log(f"{len(all_reviews)} reviews scraped, {len(matches)} mention {KEYWORD!r}")

    # Was this a complete load? If yes, replace; if partial, merge with last run
    # so a slow/partial scrape never drops reviews that were shown before.
    search_mode = place.get("search_mode", False)
    if search_mode:
        log("Keyword-search mode: results are merged with previous runs (never shrink)")
        if not matches:
            log("Search returned no matching reviews - leaving reviews.json unchanged")
            sys.exit(1)
    complete = (not search_mode) and place.get("total") is not None and len(all_reviews) >= place["total"] * 0.95
    if not search_mode and place.get("total") and len(all_reviews) < place["total"] * 0.5:
        log(f"Only {len(all_reviews)} of {place['total']} reviews loaded - "
            "treating as a failed scrape and leaving reviews.json unchanged")
        sys.exit(1)
    previous = []
    if OUTPUT.exists():
        try:
            previous = json.loads(OUTPUT.read_text(encoding="utf-8")).get("reviews", [])
        except Exception:
            previous = []

    if complete or not previous:
        final = matches
    else:
        log("Partial load detected - merging with previously saved reviews")
        seen = {r["id"] for r in matches}
        final = matches + [r for r in previous if r.get("id") not in seen]

    payload = {
        "place_name": place.get("name"),
        "place_rating": place.get("rating"),
        "place_total_reviews": place.get("total"),
        "maps_url": maps_url,
        "keyword": KEYWORD,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scraped_review_count": len(all_reviews),
        "complete_load": complete,
        "reviews": final,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Wrote {len(final)} reviews to {OUTPUT}")


if __name__ == "__main__":
    main()
