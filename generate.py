#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║         SUNO AUTO GENERATOR — Full Script v8         ║
║  Fix   : ensure_ascii JSON, suno_poll token fix      ║
║  Lyric : Gemini → OpenRouter → Suno Auto             ║
║  Input : titles.txt + config.json                    ║
║  Track : done.txt                                    ║
╚══════════════════════════════════════════════════════╝
"""

import os
import json
import time
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
#  AMBIL ENV / SECRETS
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

# ══════════════════════════════════════════════════════
#  UTILS FILE
# ══════════════════════════════════════════════════════
def load_titles() -> list:
    if not os.path.exists(TITLES_FILE):
        raise FileNotFoundError(f"{TITLES_FILE} not found!")
    with open(TITLES_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]

def load_done() -> set:
    if not os.path.exists(DONE_FILE):
        return set()
    with open(DONE_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return {l.strip() for l in lines if l.strip() and not l.startswith("#")}

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

def clean_text(text: str) -> str:
    """Paksa semua teks jadi pure ASCII — tidak ada pengecualian."""
    return text.encode("ascii", errors="ignore").decode("ascii").strip()

def safe_json_post(url: str, headers: dict, payload: dict, timeout: int = 60):
    """POST dengan JSON yang dijamin pure ASCII (ensure_ascii=True)."""
    body = json.dumps(payload, ensure_ascii=True)
    h = dict(headers)
    h["Content-Type"] = "application/json; charset=utf-8"
    return requests.post(url, data=body.encode("utf-8"), headers=h, timeout=timeout)

# ══════════════════════════════════════════════════════
#  LYRIC PROMPT
# ══════════════════════════════════════════════════════
def _lyrics_prompt(title: str, music_prompt: str) -> str:
    return (
        f'Write song lyrics titled "{title}".\n'
        f"Genre/Mood: {music_prompt}.\n"
        f"Use this exact format:\n\n"
        f"[Verse 1]\n(verse 1 lyrics)\n\n"
        f"[Chorus]\n(chorus lyrics)\n\n"
        f"[Verse 2]\n(verse 2 lyrics)\n\n"
        f"[Chorus]\n(chorus lyrics)\n\n"
        f"[Outro]\n(outro lyrics)\n\n"
        f"Rules:\n"
        f"- Maximum 300 words\n"
        f"- Use ONLY standard English letters and punctuation\n"
        f"- NO special symbols, arrows, or unicode characters\n"
        f"- Write lyrics ONLY, no explanation\n"
        f"- Write in English\n"
    )

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — GEMINI
# ══════════════════════════════════════════════════════
def generate_lyrics_gemini(title: str, music_prompt: str) -> str:
    client = genai.Client(api_key=GEMINI_KEY)
    models = ["gemini-2.0-flash-lite", "gemini-2.0-flash"]
    last_error = None
    for model_name in models:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=_lyrics_prompt(title, music_prompt)
                )
                lyrics = response.text.strip()
                if lyrics:
                    print(f"      ✅ Gemini OK ({model_name})")
                    return lyrics
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "limit: 0" in err_str:
                    print(f"      ⚠️  Gemini quota habis hari ini, skip")
                    raise Exception(f"Gemini quota habis: {e}")
                elif "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    wait = 15 * (attempt + 1)
                    print(f"      ⏳ Rate limit [{model_name}] tunggu {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"      ⚠️  [{model_name}] error: {e}")
                    break
    raise Exception(f"Gemini gagal: {last_error}")

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — OPENROUTER
# ══════════════════════════════════════════════════════
def generate_lyrics_openrouter(title: str, music_prompt: str) -> str:
    free_models = [
        "meta-llama/llama-3.3-70b-instruct:free",
        "mistralai/mistral-small-3.1-24b-instruct:free",
        "google/gemma-3-27b-it:free",
        "qwen/qwen3-4b:free",
        "openrouter/auto",
    ]
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/suno-auto-generator",
        "X-Title": "Suno Auto Generator",
    }
    last_error = None
    for model in free_models:
        try:
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a professional song lyric writer. "
                            "Write lyrics in English only. "
                            "Use ONLY standard ASCII characters (a-z, A-Z, 0-9, basic punctuation). "
                            "Absolutely NO special symbols, arrows, emoji, or unicode. "
                            "Write lyrics only, no explanation."
                        ),
                    },
                    {"role": "user", "content": _lyrics_prompt(title, music_prompt)},
                ],
                "max_tokens": 800,
                "temperature": 0.8,
            }
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            if resp.status_code == 429:
                print(f"      ⏳ OpenRouter [{model}] rate limit, tunggu 20s...")
                time.sleep(20)
                continue
            resp.raise_for_status()
            data = resp.json()
            if "choices" not in data or not data["choices"]:
                print(f"      ⚠️  [{model}] no choices, skip")
                continue
            lyrics = data["choices"][0]["message"]["content"].strip()
            if lyrics:
                print(f"      ✅ OpenRouter OK ({model})")
                return lyrics
        except Exception as e:
            last_error = e
            print(f"      ⚠️  OpenRouter [{model}] gagal → {e}")
            time.sleep(3)
            continue
    raise Exception(f"Semua OpenRouter gagal: {last_error}")

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — AUTO FALLBACK
# ══════════════════════════════════════════════════════
def generate_lyrics(title: str, music_prompt: str):
    """Return (lyrics, use_custom). lyrics=None → Suno auto."""
    if GEMINI_KEY:
        try:
            print("      📝 Generating lyrics (Gemini)...")
            return generate_lyrics_gemini(title, music_prompt), True
        except Exception as e:
            print(f"      ⚠️  Gemini gagal → {e}")

    if OPENROUTER_KEY:
        try:
            print("      📝 Fallback to OpenRouter...")
            return generate_lyrics_openrouter(title, music_prompt), True
        except Exception as e:
            print(f"      ⚠️  OpenRouter gagal → {e}")

    print("      🎵 Fallback: Suno auto-generate lirik")
    return None, False

# ══════════════════════════════════════════════════════
#  SUNO DIRECT API
# ══════════════════════════════════════════════════════
def suno_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

def suno_generate(token: str, title: str, lyrics, style_tags: str, use_custom: bool) -> list:
    if use_custom and lyrics:
        # Paksa ASCII sebelum masuk payload
        lyrics_safe     = clean_text(lyrics)
        title_safe      = clean_text(title)
        style_safe      = clean_text(style_tags)
        payload = {
            "mv": "chirp-v3-5",
            "prompt": lyrics_safe,
            "tags": style_safe,
            "title": title_safe,
            "make_instrumental": False,
            "continue_clip_id": None,
            "continue_at": None,
        }
        print(f"      🎼 Mode: Custom lyrics")
    else:
        title_safe  = clean_text(title)
        style_safe  = clean_text(style_tags)
        payload = {
            "mv": "chirp-v3-5",
            "prompt": f"{title_safe}. {style_safe}",
            "tags": style_safe,
            "title": title_safe,
            "make_instrumental": False,
            "continue_clip_id": None,
            "continue_at": None,
        }
        print(f"      🎼 Mode: Suno auto-lyrics")

    # ← Kunci fix: ensure_ascii=True saat serialize JSON
    resp = safe_json_post(SUNO_GENERATE, suno_headers(token), payload)

    if resp.status_code != 200:
        raise Exception(f"Suno generate gagal: {resp.status_code} → {resp.text[:300]}")

    data  = resp.json()
    clips = data.get("clips", [])
    if not clips:
        raise Exception(f"Suno tidak return clip: {data}")
    ids = [c["id"] for c in clips]
    print(f"      🎬 Clip IDs: {ids}")
    return ids

def suno_poll(token: str, clip_ids: list) -> list:
    ids_str  = ",".join(clip_ids)
    max_menit = POLL_MAX_RETRY * POLL_INTERVAL // 60
    print(f"      ⏳ Polling (max {max_menit} menit)...")

    for attempt in range(POLL_MAX_RETRY):
        try:
            # ← Fix: pakai token yang benar, bukan ""
            resp = requests.get(
                f"{SUNO_FEED}?ids={ids_str}",
                headers=suno_headers(token),
                timeout=30,
            )
            if resp.status_code != 200:
                print(f"      ⚠️  Poll {resp.status_code}, retry...")
                time.sleep(POLL_INTERVAL)
                continue

            clips    = resp.json()
            statuses = [c.get("status", "") for c in clips]
            print(f"      📊 [{attempt+1}/{POLL_MAX_RETRY}] {statuses}")

            if all(s == "complete" for s in statuses):
                print("      ✅ Audio selesai!")
                return clips
            if any(s == "error" for s in statuses):
                raise Exception(f"Suno clip error: {clips}")

        except Exception as e:
            if "clip error" in str(e):
                raise
            print(f"      ⚠️  Poll exception: {e}")

        time.sleep(POLL_INTERVAL)

    raise Exception(f"Polling timeout {max_menit} menit")

def suno_download(clips: list, title: str) -> list:
    saved = []
    for i, clip in enumerate(clips):
        audio_url = clip.get("audio_url", "")
        if not audio_url:
            print(f"      ⚠️  Clip {i+1} tidak ada audio_url, skip")
            continue
        fname = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_{i+1}.mp3")
        r = requests.get(audio_url, timeout=120)
        r.raise_for_status()
        with open(fname, "wb") as f:
            f.write(r.content)
        saved.append(fname)
        print(f"      💾 Saved: {fname} ({len(r.content)//1024} KB)")
    return saved

def generate_audio(token: str, title: str, lyrics, style_tags: str, use_custom: bool) -> list:
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
    for song_idx, title in enumerate(batch, 1):
        print(f"\n    [{song_idx}/{len(batch)}] 🎵 {title}")
        try:
            # Step 1 — Generate lirik
            lyrics, use_custom = generate_lyrics(title, music_prompt)

            # Simpan lirik TXT (pakai utf-8 agar bisa baca unicode)
            if lyrics:
                lyric_path = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_lyrics.txt")
                with open(lyric_path, "w", encoding="utf-8") as lf:
                    lf.write(f"Title  : {title}\n")
                    lf.write(f"Prompt : {music_prompt}\n")
                    lf.write(f"Style  : {style_tags}\n")
                    lf.write("─" * 40 + "\n\n")
                    lf.write(lyrics)
                print(f"      📄 Lyrics saved: {lyric_path}")
            else:
                print(f"      📄 Suno akan auto-generate lirik")

            # Step 2 — Generate & download (clean_text dilakukan di dalam suno_generate)
            generate_audio(token, title, lyrics, style_tags, use_custom)

            # Step 3 — Tandai done
            mark_done(title)
            success.append(title)
            print(f"      ✅ Done: {title}")

        except Exception as e:
            print(f"      ❌ FAILED [{title}]: {e}")

        if song_idx < len(batch):
            print("      ⏳ Waiting 15 seconds...")
            time.sleep(15)

    return success

# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
def main():
    print("\n" + "═" * 55)
    print("  🎵  SUNO AUTO GENERATOR v8")
    print("═" * 55)

    if not SUNO_TOKENS:
        raise RuntimeError("❌ SUNO_TOKENS kosong! Tambahkan ke GitHub Secrets.")

    config       = load_config()
    music_prompt = config["music_prompt"]
    style_tags   = config["style_tags"]

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
        print("\n  ✅ All titles already processed!")
        print("  💡 Add new titles to titles.txt")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    batches     = [to_process[i:i+MAX_PER_ACCOUNT] for i in range(0, len(to_process), MAX_PER_ACCOUNT)]
    all_success = []

    for idx, (batch, token) in enumerate(zip(batches, SUNO_TOKENS)):
        success = process_batch(batch, music_prompt, style_tags, token, idx)
        all_success.extend(success)
        if idx < len(batches) - 1:
            print(f"\n  ⏳ Waiting 30 seconds before next account...")
            time.sleep(30)

    remaining = len(pending) - len(all_success)
    print("\n" + "═" * 55)
    print(f"  ✅ Success   : {len(all_success)} songs")
    print(f"  ❌ Failed    : {len(to_process) - len(all_success)} songs")
    print(f"  📋 Remaining : {remaining} titles")
    print(f"  📁 Output    : ./{OUTPUT_DIR}/")
    print("═" * 55 + "\n")

    if len(all_success) == 0 and len(to_process) > 0:
        raise RuntimeError("No songs generated!")

if __name__ == "__main__":
    main()
