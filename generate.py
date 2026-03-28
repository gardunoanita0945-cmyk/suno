#!/usr/bin/env python3
"""
SUNO AUTO GENERATOR v14 — Playwright Browser Automation
Bypass studio-api.suno.ai (suspended) dengan control browser langsung
Setup:
  pip install playwright requests google-genai
  playwright install chromium
Secrets:
  SUNO_COOKIE     = cookie string dari browser (F12 → Network → copy cookie header)
  GEMINI_API_KEY  = optional
  OPENROUTER_KEY  = optional
"""

import json
import os
import re
import sys
import time
import traceback

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    from google import genai
    GENAI_OK = True
except ImportError:
    GENAI_OK = False

# ══════════════════════════════════════════════════════
#  KONSTANTA
# ══════════════════════════════════════════════════════
MAX_PER_ACCOUNT  = 8
OUTPUT_DIR       = "output_audio"
TITLES_FILE      = "titles.txt"
DONE_FILE        = "done.txt"
CONFIG_FILE      = "config.json"
SUNO_CREATE_URL  = "https://suno.com/create"
SUNO_DOMAIN      = "suno.com"
POLL_INTERVAL    = 8    # detik antar cek status di halaman
POLL_MAX         = 90   # max iterasi poll (~12 menit)
BETWEEN_SONGS    = 20   # detik jeda antar lagu

# ══════════════════════════════════════════════════════
#  ENV / SECRETS
# ══════════════════════════════════════════════════════
GEMINI_KEY     = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
SUNO_COOKIE    = os.environ.get("SUNO_COOKIE", "").strip()

def _parse_tokens(raw: str) -> list:
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except Exception:
            pass
    return [t.strip().strip("'\"") for t in raw.replace("\n", ",").split(",") if t.strip()]

SUNO_TOKENS = _parse_tokens(os.environ.get("SUNO_TOKENS", ""))

print(f"  DEBUG: Python {sys.version.split()[0]} | requests {requests.__version__}")
print(f"  DEBUG: SUNO_COOKIE={'set' if SUNO_COOKIE else 'MISSING'}")
print(f"  DEBUG: SUNO_TOKENS={len(SUNO_TOKENS)} token(s)")

# ══════════════════════════════════════════════════════
#  ASCII UTILS
# ══════════════════════════════════════════════════════
def force_ascii(text) -> str:
    if text is None:
        return ""
    return str(text).encode("ascii", errors="ignore").decode("ascii").strip()

# ══════════════════════════════════════════════════════
#  FILE UTILS
# ══════════════════════════════════════════════════════
def load_titles() -> list:
    if not os.path.exists(TITLES_FILE):
        raise FileNotFoundError(f"{TITLES_FILE} not found!")
    with open(TITLES_FILE, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]

def load_done() -> set:
    if not os.path.exists(DONE_FILE):
        return set()
    with open(DONE_FILE, "r", encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip() and not l.startswith("#")}

def mark_done(title: str):
    with open(DONE_FILE, "a", encoding="utf-8") as f:
        f.write(title + "\n")

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"{CONFIG_FILE} not found!")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def safe_filename(title: str) -> str:
    for c in r'\/:*?"<>|':
        title = title.replace(c, "")
    return title.replace(" ", "_")[:80]

# ══════════════════════════════════════════════════════
#  LYRIC PROMPT
# ══════════════════════════════════════════════════════
LYRIC_SYSTEM = (
    "You are a song lyric writer. Write lyrics in English only. "
    "Use ONLY standard ASCII characters. NO arrows, emoji, or unicode. "
    "Output lyrics only, no explanation."
)

def _lyrics_prompt(title: str, mood: str) -> str:
    return (
        f'Write song lyrics titled "{title}".\n'
        f"Mood/Genre: {mood}\n\n"
        f"[Verse 1]\n(4 lines)\n\n[Chorus]\n(4 lines)\n\n"
        f"[Verse 2]\n(4 lines)\n\n[Chorus]\n(4 lines)\n\n"
        f"[Outro]\n(2 lines)\n\nMax 250 words. ASCII only."
    )

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — GEMINI
# ══════════════════════════════════════════════════════
def _gemini_lyrics(title: str, mood: str):
    if not GENAI_OK or not GEMINI_KEY:
        return None
    try:
        client = genai.Client(api_key=GEMINI_KEY)
        for model in ["gemini-2.0-flash-lite", "gemini-2.0-flash"]:
            try:
                r = client.models.generate_content(
                    model=model, contents=_lyrics_prompt(title, mood)
                )
                text = r.text.strip()
                if text:
                    print(f"      ✅ Gemini OK ({model})")
                    return text
            except Exception as e:
                err = str(e)
                if any(x in err for x in ["limit: 0", "429", "RESOURCE_EXHAUSTED"]):
                    print(f"      ⚠️  Gemini quota habis, skip")
                    return None
                print(f"      ⚠️  Gemini [{model}]: {e}")
    except Exception as e:
        print(f"      ⚠️  Gemini: {e}")
    return None

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — OPENROUTER
# ══════════════════════════════════════════════════════
def _openrouter_lyrics(title: str, mood: str):
    if not OPENROUTER_KEY:
        return None
    models = [
        "meta-llama/llama-3.3-70b-instruct:free",
        "mistralai/mistral-small-3.1-24b-instruct:free",
        "google/gemma-3-27b-it:free",
        "qwen/qwen3-4b:free",
        "openrouter/auto",
    ]
    hdrs = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/suno-auto",
        "X-Title": "Suno Auto Generator",
    }
    for model in models:
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": LYRIC_SYSTEM},
                    {"role": "user", "content": _lyrics_prompt(title, mood)},
                ],
                "max_tokens": 700,
                "temperature": 0.8,
            }
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=hdrs, json=payload, timeout=60,
            )
            if resp.status_code == 429:
                print(f"      ⏳ [{model}] rate limit 20s...")
                time.sleep(20)
                continue
            if resp.status_code in (402, 404):
                print(f"      ⚠️  [{model}] {resp.status_code}, skip")
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("choices"):
                continue
            text = data["choices"][0]["message"]["content"].strip()
            if text:
                print(f"      ✅ OpenRouter OK ({model})")
                return text
        except Exception as e:
            print(f"      ⚠️  [{model}]: {e}")
            time.sleep(3)
    return None

# ══════════════════════════════════════════════════════
#  LYRIC FALLBACK CHAIN
# ══════════════════════════════════════════════════════
def generate_lyrics(title: str, mood: str):
    raw = None
    if GEMINI_KEY:
        print("      📝 Generating lyrics (Gemini)...")
        raw = _gemini_lyrics(title, mood)
    if raw is None and OPENROUTER_KEY:
        print("      📝 Fallback → OpenRouter...")
        raw = _openrouter_lyrics(title, mood)
    if raw is None:
        print("      🎵 Semua LLM gagal → Suno auto-lyrics")
        return None, False
    clean = force_ascii(raw)
    print(f"      📝 Lyrics ready: {len(clean)} chars")
    return clean, True

# ══════════════════════════════════════════════════════
#  COOKIE STRING → Playwright cookies list
# ══════════════════════════════════════════════════════
def parse_cookie_string(cookie_str: str, domain: str = "suno.com") -> list:
    """Convert 'key=val; key2=val2' → list of Playwright cookie dicts."""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name  = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append({
            "name":   name,
            "value":  value,
            "domain": f".{domain}",
            "path":   "/",
        })
    return cookies

# ══════════════════════════════════════════════════════
#  PLAYWRIGHT — CREATE 1 LAGU
# ══════════════════════════════════════════════════════
def pw_create_song(page, title: str, lyrics, style_tags: str, use_custom: bool) -> list:
    """
    Navigasi ke suno.com/create, isi form, klik Create,
    tunggu audio selesai, return list audio_url.
    """
    captured_clips = []

    # Intercept response dari endpoint generate/feed untuk ambil audio URL
    def on_response(response):
        url = response.url
        if any(x in url for x in ["/api/generate", "/api/feed", "/api/clip"]):
            try:
                body = response.json()
                # Generate response
                if isinstance(body, dict) and "clips" in body:
                    for clip in body["clips"]:
                        cid = clip.get("id", "")
                        if cid and cid not in [c.get("id") for c in captured_clips]:
                            captured_clips.append(clip)
                            print(f"      📡 Captured clip: {cid}")
                # Feed response (array)
                if isinstance(body, list):
                    for clip in body:
                        if isinstance(clip, dict):
                            cid = clip.get("id", "")
                            aud = clip.get("audio_url", "")
                            if cid and aud:
                                for c in captured_clips:
                                    if c.get("id") == cid:
                                        c["audio_url"]  = aud
                                        c["status"]     = clip.get("status", "")
            except Exception:
                pass

    page.on("response", on_response)

    # Buka halaman create
    print("      🌐 Navigasi ke suno.com/create ...")
    page.goto(SUNO_CREATE_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)

    # Klik tab "Custom"
    try:
        custom_btn = page.locator("button:has-text('Custom'), [data-tab='custom'], label:has-text('Custom')")
        custom_btn.first.click(timeout=10_000)
        page.wait_for_timeout(1000)
        print("      🎛️  Custom mode aktif")
    except PWTimeout:
        print("      ⚠️  Custom button tidak ditemukan, coba selector lain...")
        # Fallback: cari semua button yang ada
        btns = page.locator("button").all_text_contents()
        print(f"      🔍 Buttons: {btns[:10]}")

    title_a = force_ascii(title)
    tags_a  = force_ascii(style_tags)

    # Isi Style of Music
    try:
        style_input = page.locator(
            "input[placeholder*='style' i], input[placeholder*='genre' i], "
            "textarea[placeholder*='style' i], [aria-label*='style' i]"
        ).first
        style_input.click()
        style_input.fill(tags_a)
        page.wait_for_timeout(500)
        print(f"      🎸 Style: {tags_a[:50]}")
    except Exception as e:
        print(f"      ⚠️  Style input: {e}")

    # Isi Title
    try:
        title_input = page.locator(
            "input[placeholder*='title' i], input[name='title'], "
            "[aria-label*='title' i]"
        ).first
        title_input.click()
        title_input.fill(title_a)
        page.wait_for_timeout(500)
        print(f"      🏷️  Title: {title_a}")
    except Exception as e:
        print(f"      ⚠️  Title input: {e}")

    # Isi Lyrics (jika custom)
    if use_custom and lyrics:
        try:
            lyric_input = page.locator(
                "textarea[placeholder*='lyric' i], textarea[placeholder*='Enter your own' i], "
                "textarea[placeholder*='lyrics' i], [aria-label*='lyric' i]"
            ).first
            lyric_input.click()
            lyric_input.fill(force_ascii(lyrics))
            page.wait_for_timeout(500)
            print(f"      📜 Lyrics diisi ({len(lyrics)} chars)")
        except Exception as e:
            print(f"      ⚠️  Lyrics input: {e}")
            use_custom = False

    # Klik Create
    try:
        create_btn = page.locator(
            "button:has-text('Create'), button[type='submit']:has-text('Create'), "
            "button:has-text('Generate')"
        ).first
        create_btn.click(timeout=10_000)
        print("      🎬 Klik Create!")
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"      ⚠️  Create button: {e}")
        raise Exception(f"Gagal klik Create: {e}")

    # Poll sampai audio_url tersedia
    print(f"      ⏳ Menunggu audio (max {POLL_MAX * POLL_INTERVAL // 60} menit)...")
    completed = []

    for attempt in range(POLL_MAX):
        page.wait_for_timeout(POLL_INTERVAL * 1000)

        # Cek captured_clips dari network intercept
        done = [c for c in captured_clips if c.get("audio_url") and c.get("status") == "complete"]
        if done:
            print(f"      ✅ Audio ready via intercept! ({len(done)} clips)")
            completed = done
            break

        # Fallback: cari audio element di DOM
        try:
            audio_els = page.locator("audio[src]").all()
            for el in audio_els:
                src = el.get_attribute("src") or ""
                if src.startswith("http") and src not in [c.get("audio_url") for c in completed]:
                    completed.append({"audio_url": src, "id": f"dom_{len(completed)}"})
            if completed:
                print(f"      ✅ Audio ready via DOM! ({len(completed)} clips)")
                break
        except Exception:
            pass

        # Progress log
        statuses = [c.get("status", "?") for c in captured_clips]
        print(f"      📊 [{attempt+1}/{POLL_MAX}] clips={len(captured_clips)} statuses={statuses}")

        # Cek error di halaman
        try:
            err_el = page.locator("[class*='error' i]:visible, [data-error]:visible").first
            err_txt = err_el.inner_text(timeout=1000)
            if err_txt:
                raise Exception(f"Suno error di halaman: {err_txt}")
        except PWTimeout:
            pass

    page.remove_listener("response", on_response)

    if not completed:
        # Screenshot untuk debug
        ss_path = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_debug.png")
        page.screenshot(path=ss_path)
        print(f"      📸 Screenshot: {ss_path}")
        raise Exception("Timeout: audio tidak muncul setelah polling")

    return completed

# ══════════════════════════════════════════════════════
#  DOWNLOAD AUDIO
# ══════════════════════════════════════════════════════
def download_clips(clips: list, title: str) -> list:
    saved = []
    for i, clip in enumerate(clips):
        url = clip.get("audio_url", "")
        if not url:
            continue
        fname = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_{i+1}.mp3")
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            with open(fname, "wb") as f:
                f.write(r.content)
            saved.append(fname)
            print(f"      💾 {fname} ({len(r.content)//1024} KB)")
        except Exception as e:
            print(f"      ⚠️  Download gagal clip {i+1}: {e}")
    return saved

# ══════════════════════════════════════════════════════
#  PROSES BATCH — 1 BROWSER SESSION
# ══════════════════════════════════════════════════════
def process_batch(batch, mood, style_tags, cookie_str, account_idx) -> list:
    print(f"\n  {'─'*52}")
    print(f"  🔑 Account #{account_idx+1} | {len(batch)} songs | Playwright")
    print(f"  {'─'*52}")

    success = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        # Inject cookies
        if cookie_str:
            cookies = parse_cookie_string(cookie_str, SUNO_DOMAIN)
            context.add_cookies(cookies)
            print(f"      🍪 {len(cookies)} cookies injected")
        else:
            print("      ⚠️  SUNO_COOKIE kosong! Set di GitHub Secrets.")

        page = context.new_page()

        # Buka halaman awal untuk verifikasi login
        print("      🔐 Cek login status...")
        page.goto("https://suno.com", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)

        # Cek apakah sudah login
        page_content = page.content()
        if "Log in" in page_content and "Log out" not in page_content:
            print("      ⚠️  Mungkin belum login. Lanjut mencoba...")
        else:
            print("      ✅ Login OK")

        # Screenshot home untuk debug
        ss = os.path.join(OUTPUT_DIR, f"account{account_idx+1}_home.png")
        page.screenshot(path=ss)
        print(f"      📸 Home screenshot: {ss}")

        for i, title in enumerate(batch, 1):
            print(f"\n    [{i}/{len(batch)}] 🎵 {title}")
            try:
                # Generate lirik
                lyrics, use_custom = generate_lyrics(title, mood)

                # Simpan lirik
                if lyrics:
                    lpath = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_lyrics.txt")
                    with open(lpath, "w", encoding="utf-8") as f:
                        f.write(f"Title : {title}\nStyle : {style_tags}\n")
                        f.write("-" * 40 + "\n\n" + lyrics)
                    print(f"      📄 Lyrics: {lpath}")

                # Buat lagu via Playwright
                clips = pw_create_song(page, title, lyrics, style_tags, use_custom)

                # Download audio
                saved = download_clips(clips, title)

                if saved:
                    mark_done(title)
                    success.append(title)
                    print(f"      ✅ Done: {title} ({len(saved)} files)")
                else:
                    print(f"      ⚠️  Clip ada tapi download gagal")

            except Exception as e:
                print(f"      ❌ FAILED [{title}]: {e}")
                traceback.print_exc()
                # Screenshot untuk debug
                try:
                    ss = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_error.png")
                    page.screenshot(path=ss)
                    print(f"      📸 Error screenshot: {ss}")
                except Exception:
                    pass

            if i < len(batch):
                print(f"      ⏳ Waiting {BETWEEN_SONGS}s...")
                time.sleep(BETWEEN_SONGS)

        context.close()
        browser.close()

    return success

# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
def main():
    print("\n" + "═"*55)
    print("  🎵  SUNO AUTO GENERATOR v14 (Playwright)")
    print("═"*55)

    # Validasi
    if not SUNO_COOKIE and not SUNO_TOKENS:
        raise RuntimeError(
            "❌ Set SUNO_COOKIE di GitHub Secrets!\n"
            "   Cara: F12 → Network → request ke suno.com → copy header 'Cookie'"
        )

    cfg        = load_config()
    mood       = force_ascii(cfg.get("music_prompt", ""))
    style_tags = force_ascii(cfg.get("style_tags", ""))

    all_titles     = load_titles()
    done_set       = load_done()
    pending        = [t for t in all_titles if t not in done_set]
    total_capacity = MAX_PER_ACCOUNT  # 1 akun = 1 cookie
    to_process     = pending[:total_capacity]

    print(f"\n  Suno accounts  : 1 (browser automation)")
    print(f"  Capacity/day   : {total_capacity} songs")
    print(f"  Total titles   : {len(all_titles)}")
    print(f"  Already done   : {len(done_set)}")
    print(f"  Will process   : {len(to_process)}")
    print(f"  mood           : {mood[:60]}")
    print(f"  style_tags     : {style_tags[:60]}")
    print(f"\n  LLM Gemini     : {'✅ ready' if GEMINI_KEY else '⚠️  skip'}")
    print(f"  LLM OpenRouter : {'✅ ready' if OPENROUTER_KEY else '⚠️  skip'}")
    print(f"  Mode           : 🎭 Playwright browser")

    if not to_process:
        print("\n  ✅ Semua title sudah diproses!")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Gunakan SUNO_COOKIE (prioritas) atau SUNO_TOKENS[0] sebagai cookie fallback
    cookie_str = SUNO_COOKIE or (SUNO_TOKENS[0] if SUNO_TOKENS else "")

    success = process_batch(to_process, mood, style_tags, cookie_str, 0)

    remaining = len(pending) - len(success)
    print("\n" + "═"*55)
    print(f"  ✅ Success   : {len(success)} songs")
    print(f"  ❌ Failed    : {len(to_process) - len(success)} songs")
    print(f"  📋 Remaining : {remaining} titles")
    print(f"  📁 Output    : ./{OUTPUT_DIR}/")
    print("═"*55 + "\n")

    if len(success) == 0 and len(to_process) > 0:
        raise RuntimeError("No songs generated!")

if __name__ == "__main__":
    main()
