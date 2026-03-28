#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║         SUNO AUTO GENERATOR — Full Script v12        ║
║  Fix   : ganti requests.post → http.client (bypass   ║
║          latin-1 encoding bug di urllib3)             ║
║  Lyric : Gemini → OpenRouter → Suno Auto             ║
║  Input : titles.txt + config.json                    ║
║  Track : done.txt                                    ║
╚══════════════════════════════════════════════════════╝
"""

import http.client
import json
import os
import sys
import time
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
SUNO_GENERATE   = "https://studio-api.suno.ai/api/generate/v2/"
SUNO_FEED       = "https://studio-api.suno.ai/api/feed/"
POLL_INTERVAL   = 15
POLL_MAX_RETRY  = 40

# ══════════════════════════════════════════════════════
#  ENV / SECRETS
# ══════════════════════════════════════════════════════
GEMINI_KEY     = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

raw_tokens = os.environ.get("SUNO_TOKENS", "").strip()
if not raw_tokens:
    SUNO_TOKENS = []
elif raw_tokens.startswith("["):
    try:
        SUNO_TOKENS = json.loads(raw_tokens)
    except Exception:
        SUNO_TOKENS = []
else:
    SUNO_TOKENS = [
        t.strip().strip("'\"")
        for t in raw_tokens.replace("\n", ",").split(",")
        if t.strip().strip("'\"")
    ]

print(f"  DEBUG: {len(SUNO_TOKENS)} token(s) loaded")
print(f"  DEBUG: Python {sys.version.split()[0]}, requests {requests.__version__}")

# ══════════════════════════════════════════════════════
#  UTILS FILE
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
    return title.replace(" ", "_")

def force_ascii(text) -> str:
    """Paksa string jadi pure printable ASCII. Tidak ada pengecualian."""
    if text is None:
        return ""
    # Encode ke ASCII, karakter non-ASCII dibuang
    return str(text).encode("ascii", errors="ignore").decode("ascii").strip()

# ══════════════════════════════════════════════════════
#  HTTP CLIENT — BYPASS requests UNTUK SUNO POST
#  Ini fix utama latin-1 encode error
# ══════════════════════════════════════════════════════
def suno_post(token: str, payload: dict, timeout: int = 60) -> dict:
    """
    POST ke Suno menggunakan http.client bawaan Python.
    Tidak pakai requests.post() sehingga tidak ada latin-1 encoding issue.
    """
    # 1. Serialize JSON → pure ASCII string
    json_str = json.dumps(payload, ensure_ascii=True)

    # 2. Pastikan 100% ASCII
    bad = [(i, c) for i, c in enumerate(json_str) if ord(c) > 127]
    if bad:
        raise ValueError(
            f"JSON masih ada non-ASCII setelah ensure_ascii=True: "
            + ", ".join(f"pos={p} char={repr(c)}" for p, c in bad[:5])
        )

    # 3. Encode ke bytes ASCII
    body = json_str.encode("ascii")
    print(f"      🔍 Body: {len(body)} bytes (pure ASCII ✓)")

    # 4. Parse URL
    parsed = urllib.parse.urlparse(SUNO_GENERATE)
    path   = parsed.path
    if parsed.query:
        path += "?" + parsed.query

    # 5. Buat koneksi HTTPS langsung
    conn = http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
    headers_dict = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Content-Length": str(len(body)),
        "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept":        "application/json",
    }

    conn.request("POST", path, body=body, headers=headers_dict)
    resp      = conn.getresponse()
    status    = resp.status
    resp_body = resp.read()
    conn.close()

    # 6. Handle status
    if status == 401:
        raise Exception("Token expired/invalid. Refresh SUNO_TOKENS.")
    if status == 403:
        raise Exception("Akses ditolak. Cek subscription Suno.")
    if status == 429:
        raise Exception("Rate limit Suno.")
    if status != 200:
        raise Exception(
            f"Suno gagal: {status} → "
            + resp_body[:300].decode("utf-8", errors="replace")
        )

    return json.loads(resp_body.decode("utf-8"))

# ══════════════════════════════════════════════════════
#  LYRIC PROMPT
# ══════════════════════════════════════════════════════
LYRIC_SYSTEM = (
    "You are a professional song lyric writer. "
    "Write lyrics in English ONLY. "
    "Use ONLY standard ASCII characters: a-z A-Z 0-9 and basic punctuation. "
    "NO arrows, emoji, accented letters, or any unicode. "
    "Output lyrics only, no explanation."
)

def _lyrics_prompt(title: str, music_prompt: str) -> str:
    return (
        f'Write song lyrics titled "{title}".\n'
        f"Genre/Mood: {music_prompt}.\n\n"
        f"[Verse 1]\n(lyrics)\n\n"
        f"[Chorus]\n(lyrics)\n\n"
        f"[Verse 2]\n(lyrics)\n\n"
        f"[Chorus]\n(lyrics)\n\n"
        f"[Outro]\n(lyrics)\n\n"
        f"Max 300 words. ASCII only."
    )

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — GEMINI
# ══════════════════════════════════════════════════════
def generate_lyrics_gemini(title: str, music_prompt: str):
    try:
        client = genai.Client(api_key=GEMINI_KEY)
        for model_name in ["gemini-2.0-flash-lite", "gemini-2.0-flash"]:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=_lyrics_prompt(title, music_prompt),
                )
                lyrics = response.text.strip()
                if lyrics:
                    print(f"      ✅ Gemini OK ({model_name})")
                    return lyrics
            except Exception as e:
                err = str(e)
                if "limit: 0" in err or "429" in err or "RESOURCE_EXHAUSTED" in err:
                    print(f"      ⚠️  Gemini quota habis, skip")
                    return None
                print(f"      ⚠️  [{model_name}]: {e}")
    except Exception as e:
        print(f"      ⚠️  Gemini init error: {e}")
    return None

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — OPENROUTER
# ══════════════════════════════════════════════════════
def generate_lyrics_openrouter(title: str, music_prompt: str):
    models = [
        "meta-llama/llama-3.3-70b-instruct:free",
        "mistralai/mistral-small-3.1-24b-instruct:free",
        "google/gemma-3-27b-it:free",
        "qwen/qwen3-4b:free",
        "openrouter/auto",
    ]
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/suno-auto-generator",
        "X-Title":       "Suno Auto Generator",
    }
    for model in models:
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": LYRIC_SYSTEM},
                    {"role": "user",   "content": _lyrics_prompt(title, music_prompt)},
                ],
                "max_tokens": 800,
                "temperature": 0.8,
            }
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=60,
            )
            if resp.status_code == 429:
                print(f"      ⏳ [{model}] rate limit, tunggu 20s...")
                time.sleep(20)
                continue
            if resp.status_code in (402, 404):
                print(f"      ⚠️  [{model}] {resp.status_code}, skip")
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("choices"):
                print(f"      ⚠️  [{model}] no choices, skip")
                continue
            lyrics = data["choices"][0]["message"]["content"].strip()
            if lyrics:
                print(f"      ✅ OpenRouter OK ({model})")
                return lyrics
        except Exception as e:
            print(f"      ⚠️  [{model}]: {e}")
            time.sleep(3)
    return None

# ══════════════════════════════════════════════════════
#  GENERATE LYRICS — FALLBACK CHAIN
# ══════════════════════════════════════════════════════
def generate_lyrics(title: str, music_prompt: str):
    """Return (lyrics_ascii, use_custom). lyrics=None → Suno auto."""
    raw = None

    if GEMINI_KEY:
        print("      📝 Generating lyrics (Gemini)...")
        raw = generate_lyrics_gemini(title, music_prompt)

    if raw is None and OPENROUTER_KEY:
        print("      📝 Fallback to OpenRouter...")
        raw = generate_lyrics_openrouter(title, music_prompt)

    if raw is None:
        print("      🎵 Semua LLM gagal → Suno auto-generate")
        return None, False

    # Paksa ASCII sebelum dikembalikan
    clean = force_ascii(raw)
    print(f"      📝 Lyrics: {len(clean)} chars (ASCII ✓)")
    return clean, True

# ══════════════════════════════════════════════════════
#  SUNO API
# ══════════════════════════════════════════════════════
def suno_generate(token: str, title: str, lyrics, style_tags: str, use_custom: bool) -> list:
    # Paksa semua field ASCII
    title_a = force_ascii(title)
    style_a = force_ascii(style_tags)

    if use_custom and lyrics:
        lyrics_a = force_ascii(lyrics)
        payload  = {
            "mv": "chirp-v3-5",
            "prompt": lyrics_a,
            "tags": style_a,
            "title": title_a,
            "make_instrumental": False,
            "continue_clip_id": None,
            "continue_at": None,
        }
        print("      🎼 Mode: Custom lyrics")
    else:
        payload = {
            "mv": "chirp-v3-5",
            "prompt": f"{title_a}. {style_a}",
            "tags": style_a,
            "title": title_a,
            "make_instrumental": False,
            "continue_clip_id": None,
            "continue_at": None,
        }
        print("      🎼 Mode: Suno auto-lyrics")

    # ← PAKAI http.client, BUKAN requests.post
    data  = suno_post(token, payload)
    clips = data.get("clips", [])
    if not clips:
        raise Exception(f"Suno tidak return clip: {data}")
    ids = [c["id"] for c in clips]
    print(f"      🎬 Clip IDs: {ids}")
    return ids

def suno_poll(token: str, clip_ids: list) -> list:
    ids_str   = ",".join(clip_ids)
    max_menit = POLL_MAX_RETRY * POLL_INTERVAL // 60
    print(f"      ⏳ Polling (max {max_menit} menit)...")

    for attempt in range(POLL_MAX_RETRY):
        try:
            resp = requests.get(
                f"{SUNO_FEED}?ids={ids_str}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
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
            clips = data if isinstance(data, list) else data.get("clips", [data])

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

    raise Exception(f"Polling timeout {max_menit} menit")

def suno_download(clips: list, title: str) -> list:
    saved = []
    for i, clip in enumerate(clips):
        if not isinstance(clip, dict):
            continue
        audio_url = clip.get("audio_url", "")
        if not audio_url:
            print(f"      ⚠️  Clip {i+1}: no audio_url")
            continue
        fname = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_{i+1}.mp3")
        try:
            r = requests.get(audio_url, timeout=120)
            r.raise_for_status()
            with open(fname, "wb") as f:
                f.write(r.content)
            saved.append(fname)
            print(f"      💾 {fname} ({len(r.content)//1024} KB)")
        except Exception as e:
            print(f"      ⚠️  Download gagal clip {i+1}: {e}")
    return saved

def generate_audio(token, title, lyrics, style_tags, use_custom):
    clip_ids = suno_generate(token, title, lyrics, style_tags, use_custom)
    clips    = suno_poll(token, clip_ids)
    return suno_download(clips, title)

# ══════════════════════════════════════════════════════
#  PROSES 1 BATCH
# ══════════════════════════════════════════════════════
def process_batch(batch, music_prompt, style_tags, token, account_idx) -> list:
    print(f"\n  {'─'*52}")
    print(f"  🔑 Account #{account_idx+1} | {len(batch)} songs")
    print(f"  {'─'*52}")

    success = []
    for i, title in enumerate(batch, 1):
        print(f"\n    [{i}/{len(batch)}] 🎵 {title}")
        try:
            lyrics, use_custom = generate_lyrics(title, music_prompt)

            if lyrics:
                path = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_lyrics.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"Title : {title}\n")
                    f.write(f"Style : {style_tags}\n")
                    f.write("-" * 40 + "\n\n")
                    f.write(lyrics)
                print(f"      📄 Lyrics: {path}")
            else:
                print("      📄 Suno auto-generate lirik")

            generate_audio(token, title, lyrics, style_tags, use_custom)
            mark_done(title)
            success.append(title)
            print(f"      ✅ Done: {title}")

        except Exception as e:
            print(f"      ❌ FAILED [{title}]: {e}")

        if i < len(batch):
            print("      ⏳ Waiting 15 seconds...")
            time.sleep(15)

    return success

# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
def main():
    print("\n" + "═"*55)
    print("  🎵  SUNO AUTO GENERATOR v12")
    print("═"*55)

    if not SUNO_TOKENS:
        raise RuntimeError("❌ SUNO_TOKENS kosong!")

    cfg          = load_config()
    # ← Force ASCII semua string dari config
    music_prompt = force_ascii(cfg["music_prompt"])
    style_tags   = force_ascii(cfg["style_tags"])

    print(f"\n  music_prompt : {music_prompt[:80]}...")
    print(f"  style_tags   : {style_tags[:80]}...")

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
        success = process_batch(batch, music_prompt, style_tags, token, idx)
        all_success.extend(success)
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
