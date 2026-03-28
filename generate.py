#!/usr/bin/env python3
"""
SUNO AUTO GENERATOR v13
Fix: token sanitize, http.client manual headers, config simple
"""

import http.client
import json
import os
import sys
import time
import traceback
import urllib.parse

import requests
from google import genai

# ══════════════════════════════════════════════════════
#  KONSTANTA
# ══════════════════════════════════════════════════════
MAX_PER_ACCOUNT = 8
OUTPUT_DIR      = "output_audio"
TITLES_FILE     = "titles.txt"
DONE_FILE       = "done.txt"
CONFIG_FILE     = "config.json"
SUNO_HOST       = "studio-api.suno.ai"
SUNO_PATH       = "/api/generate/v2/"
SUNO_FEED_URL   = "https://studio-api.suno.ai/api/feed/"
POLL_INTERVAL   = 15
POLL_MAX_RETRY  = 40

# ══════════════════════════════════════════════════════
#  ENV / SECRETS
# ══════════════════════════════════════════════════════
GEMINI_KEY     = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

def _parse_tokens(raw: str) -> list:
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except Exception:
            pass
    tokens = []
    for t in raw.replace("\n", ",").split(","):
        t = t.strip().strip("'\"")
        if t:
            tokens.append(t)
    return tokens

SUNO_TOKENS = _parse_tokens(os.environ.get("SUNO_TOKENS", ""))

print(f"  DEBUG: {len(SUNO_TOKENS)} token(s) loaded")
print(f"  DEBUG: Python {sys.version.split()[0]} | requests {requests.__version__}")

# ══════════════════════════════════════════════════════
#  ASCII UTILS
# ══════════════════════════════════════════════════════
def force_ascii(text) -> str:
    """Paksa string jadi pure printable ASCII, tidak ada pengecualian."""
    if text is None:
        return ""
    return str(text).encode("ascii", errors="ignore").decode("ascii").strip()

def check_ascii(label: str, text: str):
    """Print karakter non-ASCII yang masih ada (untuk debug)."""
    bad = [(i, c, hex(ord(c))) for i, c in enumerate(text) if ord(c) > 127]
    if bad:
        print(f"      ⚠️  NON-ASCII in [{label}]: {bad[:5]}")
        return False
    return True

def sanitize_token(token: str) -> str:
    """Bersihkan token JWT — hanya A-Z a-z 0-9 . - _ ="""
    import re
    cleaned = re.sub(r"[^A-Za-z0-9.\-_=]", "", token)
    if len(cleaned) != len(token):
        print(f"      ⚠️  Token sanitized: {len(token)} → {len(cleaned)} chars")
    return cleaned

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
#  SUNO POST — http.client FULL MANUAL (no requests)
# ══════════════════════════════════════════════════════
def suno_post(token: str, payload: dict, timeout: int = 60) -> dict:
    """
    POST ke Suno tanpa requests library sama sekali.
    Semua header di-encode manual sebagai bytes ASCII.
    """
    # 1. Sanitize token
    token_safe = sanitize_token(token)

    # 2. Serialize payload → ASCII bytes
    json_str = json.dumps(payload, ensure_ascii=True)
    body     = json_str.encode("ascii")

    # 3. Debug
    check_ascii("JSON body", json_str)
    check_ascii("token",     token_safe)
    print(f"      🔍 Body: {len(body)} bytes | Token: ...{token_safe[-20:]}")

    # 4. Build headers sebagai dict of bytes
    auth_line  = f"Bearer {token_safe}"
    hdrs = {
        b"Authorization":  auth_line.encode("ascii"),
        b"Content-Type":   b"application/json",
        b"Content-Length": str(len(body)).encode("ascii"),
        b"User-Agent":     b"Mozilla/5.0",
        b"Accept":         b"application/json",
    }

    # 5. Kirim via http.client
    conn = http.client.HTTPSConnection(SUNO_HOST, timeout=timeout)
    conn.connect()
    # Kirim request line
    conn.putrequest("POST", SUNO_PATH)
    # Kirim setiap header sebagai bytes
    for k, v in hdrs.items():
        conn._send_output(None)  # flush buffer aman
        conn.putheader(k.decode(), v.decode("ascii"))
    conn.endheaders(body)

    resp      = conn.getresponse()
    status    = resp.status
    resp_body = resp.read()
    conn.close()

    print(f"      📡 Suno response: {status}")

    if status == 401:
        raise Exception("Token expired/invalid. Buat token baru di suno.com.")
    if status == 403:
        raise Exception("Akses ditolak. Cek subscription Suno.")
    if status == 429:
        raise Exception("Suno rate limit. Coba lagi nanti.")
    if status != 200:
        raise Exception(
            f"Suno HTTP {status} → "
            + resp_body[:400].decode("utf-8", errors="replace")
        )

    return json.loads(resp_body.decode("utf-8"))

# ══════════════════════════════════════════════════════
#  LYRIC SYSTEM PROMPT
# ══════════════════════════════════════════════════════
LYRIC_SYSTEM = (
    "You are a song lyric writer. "
    "Write lyrics in English only. "
    "Use ONLY standard ASCII: a-z A-Z 0-9 spaces and basic punctuation . , ! ? ' - ( ) "
    "NO arrows, emoji, accented letters, special symbols, or unicode. "
    "Output lyrics only, no title, no explanation."
)

def _lyrics_prompt(title: str, mood: str) -> str:
    return (
        f'Write song lyrics for a song titled "{title}".\n'
        f"Mood/Genre: {mood}\n\n"
        f"[Verse 1]\n(4 lines)\n\n"
        f"[Chorus]\n(4 lines)\n\n"
        f"[Verse 2]\n(4 lines)\n\n"
        f"[Chorus]\n(4 lines)\n\n"
        f"[Outro]\n(2 lines)\n\n"
        f"Max 250 words. ASCII only. Lyrics only."
    )

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — GEMINI
# ══════════════════════════════════════════════════════
def _gemini_lyrics(title: str, mood: str):
    try:
        client = genai.Client(api_key=GEMINI_KEY)
        for model in ["gemini-2.0-flash-lite", "gemini-2.0-flash"]:
            try:
                r = client.models.generate_content(
                    model=model,
                    contents=_lyrics_prompt(title, mood),
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
        print(f"      ⚠️  Gemini init: {e}")
    return None

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — OPENROUTER
# ══════════════════════════════════════════════════════
def _openrouter_lyrics(title: str, mood: str):
    models = [
        "meta-llama/llama-3.3-70b-instruct:free",
        "mistralai/mistral-small-3.1-24b-instruct:free",
        "google/gemma-3-27b-it:free",
        "qwen/qwen3-4b:free",
        "openrouter/auto",
    ]
    hdrs = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/suno-auto",
        "X-Title":       "Suno Auto Generator",
    }
    for model in models:
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": LYRIC_SYSTEM},
                    {"role": "user",   "content": _lyrics_prompt(title, mood)},
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
                print(f"      ⚠️  [{model}] no choices")
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
    """Return (lyrics_ascii | None, use_custom: bool)"""
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
    check_ascii("lyrics", clean)
    print(f"      📝 Lyrics ready: {len(clean)} chars")
    return clean, True

# ══════════════════════════════════════════════════════
#  SUNO GENERATE
# ══════════════════════════════════════════════════════
def suno_generate(token: str, title: str, lyrics, style_tags: str, use_custom: bool) -> list:
    title_a = force_ascii(title)
    tags_a  = force_ascii(style_tags)

    if use_custom and lyrics:
        lyrics_a = force_ascii(lyrics)
        payload  = {
            "mv":               "chirp-v3-5",
            "prompt":           lyrics_a,
            "tags":             tags_a,
            "title":            title_a,
            "make_instrumental": False,
            "continue_clip_id": None,
            "continue_at":      None,
        }
        print("      🎼 Mode: Custom lyrics")
    else:
        payload = {
            "mv":               "chirp-v3-5",
            "prompt":           f"{title_a}. {tags_a}",
            "tags":             tags_a,
            "title":            title_a,
            "make_instrumental": False,
            "continue_clip_id": None,
            "continue_at":      None,
        }
        print("      🎼 Mode: Suno auto-lyrics")

    data  = suno_post(token, payload)
    clips = data.get("clips", [])
    if not clips:
        raise Exception(f"Suno tidak return clip: {data}")
    ids = [c["id"] for c in clips]
    print(f"      🎬 Clip IDs: {ids}")
    return ids

# ══════════════════════════════════════════════════════
#  SUNO POLL
# ══════════════════════════════════════════════════════
def suno_poll(token: str, clip_ids: list) -> list:
    ids_str = ",".join(clip_ids)
    print(f"      ⏳ Polling (max {POLL_MAX_RETRY * POLL_INTERVAL // 60} menit)...")
    for attempt in range(POLL_MAX_RETRY):
        try:
            resp = requests.get(
                f"{SUNO_FEED_URL}?ids={ids_str}",
                headers={
                    "Authorization": f"Bearer {sanitize_token(token)}",
                    "Accept":        "application/json",
                },
                timeout=30,
            )
            if resp.status_code == 401:
                raise Exception("Token expired saat polling.")
            if resp.status_code != 200:
                print(f"      ⚠️  Poll {resp.status_code}, retry...")
                time.sleep(POLL_INTERVAL)
                continue

            data  = resp.json()
            clips = data if isinstance(data, list) else data.get("clips", [])

            statuses = [c.get("status", "") for c in clips if isinstance(c, dict)]
            print(f"      📊 [{attempt+1}/{POLL_MAX_RETRY}] {statuses}")

            if all(s == "complete" for s in statuses):
                print("      ✅ Audio selesai!")
                return clips
            if any(s == "error" for s in statuses):
                raise Exception(f"Suno clip error: {clips}")

        except Exception as e:
            if any(x in str(e) for x in ["clip error", "Token expired"]):
                raise
            print(f"      ⚠️  Poll exception: {e}")

        time.sleep(POLL_INTERVAL)

    raise Exception(f"Polling timeout {POLL_MAX_RETRY * POLL_INTERVAL // 60} menit")

# ══════════════════════════════════════════════════════
#  SUNO DOWNLOAD
# ══════════════════════════════════════════════════════
def suno_download(clips: list, title: str) -> list:
    saved = []
    for i, clip in enumerate(clips):
        if not isinstance(clip, dict):
            continue
        url = clip.get("audio_url", "")
        if not url:
            print(f"      ⚠️  Clip {i+1}: no audio_url")
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

def generate_audio(token, title, lyrics, style_tags, use_custom):
    ids   = suno_generate(token, title, lyrics, style_tags, use_custom)
    clips = suno_poll(token, ids)
    return suno_download(clips, title)

# ══════════════════════════════════════════════════════
#  PROSES 1 BATCH
# ══════════════════════════════════════════════════════
def process_batch(batch, mood, style_tags, token, idx) -> list:
    print(f"\n  {'─'*52}")
    print(f"  🔑 Account #{idx+1} | {len(batch)} songs")
    print(f"  {'─'*52}")

    success = []
    for i, title in enumerate(batch, 1):
        print(f"\n    [{i}/{len(batch)}] 🎵 {title}")
        try:
            lyrics, use_custom = generate_lyrics(title, mood)

            if lyrics:
                path = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_lyrics.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"Title : {title}\nStyle : {style_tags}\n")
                    f.write("-" * 40 + "\n\n" + lyrics)
                print(f"      📄 Lyrics: {path}")
            else:
                print("      📄 Suno akan auto-generate lirik")

            generate_audio(token, title, lyrics, style_tags, use_custom)
            mark_done(title)
            success.append(title)
            print(f"      ✅ Done: {title}")

        except Exception as e:
            print(f"      ❌ FAILED [{title}]: {e}")
            traceback.print_exc()   # ← full stack trace untuk debug

        if i < len(batch):
            print("      ⏳ Waiting 15s...")
            time.sleep(15)

    return success

# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
def main():
    print("\n" + "═"*55)
    print("  🎵  SUNO AUTO GENERATOR v13")
    print("═"*55)

    if not SUNO_TOKENS:
        raise RuntimeError("❌ SUNO_TOKENS kosong! Tambahkan ke GitHub Secrets.")

    cfg        = load_config()
    mood       = force_ascii(cfg.get("music_prompt", ""))
    style_tags = force_ascii(cfg.get("style_tags",   ""))

    # Debug cek token karakter
    for ti, tok in enumerate(SUNO_TOKENS):
        tok_s = sanitize_token(tok)
        print(f"  DEBUG: token[{ti}] len={len(tok)} → sanitized={len(tok_s)} chars")
        check_ascii(f"token[{ti}]", tok)

    all_titles     = load_titles()
    done_set       = load_done()
    pending        = [t for t in all_titles if t not in done_set]
    total_capacity = len(SUNO_TOKENS) * MAX_PER_ACCOUNT
    to_process     = pending[:total_capacity]

    print(f"\n  Suno accounts  : {len(SUNO_TOKENS)}")
    print(f"  Capacity/day   : {total_capacity} songs")
    print(f"  Total titles   : {len(all_titles)}")
    print(f"  Already done   : {len(done_set)}")
    print(f"  Will process   : {len(to_process)}")
    print(f"  mood           : {mood[:60]}...")
    print(f"  style_tags     : {style_tags[:60]}...")
    print(f"\n  LLM Gemini     : {'✅ ready' if GEMINI_KEY else '⚠️  skip'}")
    print(f"  LLM OpenRouter : {'✅ ready' if OPENROUTER_KEY else '⚠️  skip'}")
    print(f"  Lyric fallback : ✅ Suno auto-generate")

    if not to_process:
        print("\n  ✅ Semua title sudah diproses!")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    batches     = [to_process[i:i+MAX_PER_ACCOUNT] for i in range(0, len(to_process), MAX_PER_ACCOUNT)]
    all_success = []

    for idx, (batch, token) in enumerate(zip(batches, SUNO_TOKENS)):
        result = process_batch(batch, mood, style_tags, token, idx)
        all_success.extend(result)
        if idx < len(batches) - 1:
            print("\n  ⏳ Waiting 30s before next account...")
            time.sleep(30)

    remaining = len(pending) - len(all_success)
    print("\n" + "═"*55)
    print(f"  ✅ Success   : {len(all_success)} songs")
    print(f"  ❌ Failed    : {len(to_process) - len(all_success)} songs")
    print(f"  📋 Remaining : {remaining} titles")
    print(f"  📁 Output    : ./{OUTPUT_DIR}/")
    print("═"*55 + "\n")

    if len(all_success) == 0 and len(to_process) > 0:
        raise RuntimeError("No songs generated!")

if __name__ == "__main__":
    main()
