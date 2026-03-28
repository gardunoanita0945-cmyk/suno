#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║         SUNO AUTO GENERATOR — Full Script v10        ║
║  Fix   : encoding hard-fix, simple fallback          ║
║  Lyric : Gemini → OpenRouter → Suno Auto (fast)      ║
║  Input : titles.txt + config.json                    ║
║  Track : done.txt                                    ║
╚══════════════════════════════════════════════════════╝
"""

import os
import json
import re
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


# ══════════════════════════════════════════════════════
#  TEXT SANITIZATION — NUCLEAR OPTION
# ══════════════════════════════════════════════════════

# Regex: match SEMUA karakter yang BUKAN printable ASCII
#   32 = spasi, 10 = newline, 13 = carriage return
_NON_ASCII_RE = re.compile(r"[^\x20-\x7E\n\r\t]")


def force_ascii(text: str) -> str:
    """
    Paksa teks jadi pure printable ASCII.
    Tidak ada karakter non-ASCII yang bisa lolos.
    """
    if not text:
        return ""

    # Langkah 1: encode → decode sebagai ASCII, buang yang bukan ASCII
    text = text.encode("ascii", errors="ignore").decode("ascii")

    # Langkah 2: Double-check — regex sweep untuk yang mungkin lolos
    text = _NON_ASCII_RE.sub("", text)

    # Langkah 3: Bersihkan whitespace berlebih per baris
    lines = text.split("\n")
    lines = [" ".join(line.split()) for line in lines]
    text = "\n".join(lines)

    # Langkah 4: Hapus baris kosong berturut-turut
    cleaned_lines = []
    prev_empty = False
    for line in text.split("\n"):
        is_empty = line.strip() == ""
        if is_empty and prev_empty:
            continue
        cleaned_lines.append(line)
        prev_empty = is_empty

    return "\n".join(cleaned_lines).strip()


def verify_ascii(text: str, label: str = "text") -> bool:
    """Return True jika 100% ASCII. Print warning jika ada yang lolos."""
    bad = [(i, ch) for i, ch in enumerate(text) if ord(ch) > 126]
    if bad:
        print(f"      ⚠️  NON-ASCII in {label}:")
        for pos, ch in bad[:10]:
            print(f"         pos={pos} char={repr(ch)} ord={ord(ch)}")
        return False
    return True


# ══════════════════════════════════════════════════════
#  LYRIC PROMPT
# ══════════════════════════════════════════════════════
LYRIC_SYSTEM = (
    "You are a professional song lyric writer.\n"
    "Write lyrics in English ONLY.\n"
    "CRITICAL: Use ONLY these characters:\n"
    "  a-z A-Z 0-9 ! ? . , : ; ' \" ( ) - / \\n space\n"
    "NO arrows, emoji, symbols, accented letters, or unicode.\n"
    "NO curly quotes, em dashes, ellipsis, bullets.\n"
    "Write lyrics only, no explanation or commentary."
)


def _lyrics_prompt(title: str, music_prompt: str) -> str:
    return (
        f'Write song lyrics titled "{title}".\n'
        f"Genre/Mood: {music_prompt}.\n"
        f"Format:\n"
        f"[Verse 1]\n(lyrics)\n\n"
        f"[Chorus]\n(lyrics)\n\n"
        f"[Verse 2]\n(lyrics)\n\n"
        f"[Chorus]\n(lyrics)\n\n"
        f"[Outro]\n(lyrics)\n\n"
        f"Max 300 words. Lyrics only."
    )


# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — GEMINI
# ══════════════════════════════════════════════════════
def generate_lyrics_gemini(title: str, music_prompt: str) -> str:
    client = genai.Client(api_key=GEMINI_KEY)
    models = ["gemini-2.0-flash-lite", "gemini-2.0-flash"]
    for model_name in models:
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
                print(f"      ⚠️  Gemini quota/rate limit, skip")
                break
            print(f"      ⚠️  [{model_name}] error: {e}")
    return None


# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — OPENROUTER
# ══════════════════════════════════════════════════════
def generate_lyrics_openrouter(title: str, music_prompt: str) -> str:
    free_models = [
        "meta-llama/llama-3.3-70b-instruct:free",
        "mistralai/mistral-small-3.1-24b-instruct:free",
        "google/gemma-3-27b-it:free",
        "qwen/qwen3-4b:free",
    ]
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/suno-auto-generator",
        "X-Title": "Suno Auto Generator",
    }
    for model in free_models:
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": LYRIC_SYSTEM},
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
                print(f"      ⏳ [{model}] rate limit, tunggu 15s...")
                time.sleep(15)
                continue
            if resp.status_code == 402:
                print(f"      ⚠️  [{model}] credit habis, skip")
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
            print(f"      ⚠️  [{model}] gagal: {e}")
            time.sleep(2)
            continue
    return None


# ══════════════════════════════════════════════════════
#  GENERATE LYRICS — SIMPLIFIED FALLBACK
# ══════════════════════════════════════════════════════
def generate_lyrics(title: str, music_prompt: str):
    """
    Return (lyrics, use_custom).
    lyrics = None → Suno auto-generate lirik.
    """
    raw = None

    # ── Coba Gemini ──
    if GEMINI_KEY:
        print("      📝 Generating lyrics (Gemini)...")
        raw = generate_lyrics_gemini(title, music_prompt)
        if raw:
            raw = force_ascii(raw)
            if not verify_ascii(raw, "Gemini output"):
                raw = None  # Ada yang lolos, buang

    # ── Coba OpenRouter ──
    if raw is None and OPENROUTER_KEY:
        print("      📝 Fallback to OpenRouter...")
        raw = generate_lyrics_openrouter(title, music_prompt)
        if raw:
            raw = force_ascii(raw)
            if not verify_ascii(raw, "OpenRouter output"):
                raw = None  # Ada yang lolos, buang

    # ── Semua gagal → Suno auto ──
    if raw is None:
        print("      🎵 Semua AI gagal → Suno auto-generate lirik")
        return None, False

    return raw, True


# ══════════════════════════════════════════════════════
#  SUNO DIRECT API
# ══════════════════════════════════════════════════════
def suno_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36"
        ),
        "Accept": "application/json",
    }


def suno_generate(token, title, lyrics, style_tags, use_custom):
    if use_custom and lyrics:
        # ══ TRIPLE SANITIZE ══
        lyrics_safe = force_ascii(lyrics)
        title_safe  = force_ascii(title)
        style_safe  = force_ascii(style_tags)

        # Final verification
        verify_ascii(lyrics_safe, "FINAL lyrics")
        verify_ascii(title_safe, "FINAL title")
        verify_ascii(style_safe, "FINAL style")

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
        title_safe = force_ascii(title)
        style_safe = force_ascii(style_tags)
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

    resp = requests.post(
        SUNO_GENERATE,
        headers=suno_headers(token),
        json=payload,
        timeout=60,
    )

    if resp.status_code == 401:
        raise Exception("Token expired/invalid. Refresh token.")
    if resp.status_code == 403:
        raise Exception("Akses ditolak. Cek subscription.")
    if resp.status_code == 429:
        raise Exception("Rate limit. Coba lagi nanti.")
    if resp.status_code != 200:
        raise Exception(f"Suno gagal: {resp.status_code} → {resp.text[:300]}")

    data  = resp.json()
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
                headers=suno_headers(token),
                timeout=30,
            )
            if resp.status_code == 401:
                raise Exception("Token expired saat polling.")
            if resp.status_code != 200:
                print(f"      ⚠️  Poll {resp.status_code}, retry...")
                time.sleep(POLL_INTERVAL)
                continue

            clips = resp.json()
            if isinstance(clips, dict):
                clips = clips.get("clips", [clips])
            if not isinstance(clips, list):
                time.sleep(POLL_INTERVAL)
                continue

            statuses = [c.get("status", "") for c in clips]
            print(f"      📊 [{attempt+1}/{POLL_MAX_RETRY}] {statuses}")

            if all(s == "complete" for s in statuses):
                print("      ✅ Audio selesai!")
                return clips
            if any(s == "error" for s in statuses):
                raise Exception(f"Suno clip error: {clips}")

        except Exception as e:
            if "clip error" in str(e) or "Token expired" in str(e):
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
        try:
            r = requests.get(audio_url, timeout=120)
            r.raise_for_status()
            with open(fname, "wb") as f:
                f.write(r.content)
            saved.append(fname)
            print(f"      💾 Saved: {fname} ({len(r.content)//1024} KB)")
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
def process_batch(batch, music_prompt, style_tags, token, account_idx):
    print(f"\n  {'─'*52}")
    print(f"  🔑 Account #{account_idx+1} | {len(batch)} songs")
    print(f"  {'─'*52}")

    success = []
    for song_idx, title in enumerate(batch, 1):
        print(f"\n    [{song_idx}/{len(batch)}] 🎵 {title}")
        try:
            # Step 1 — Generate lirik
            lyrics, use_custom = generate_lyrics(title, music_prompt)

            # Simpan lirik TXT (kalau ada)
            if lyrics:
                lyric_path = os.path.join(
                    OUTPUT_DIR, f"{safe_filename(title)}_lyrics.txt"
                )
                with open(lyric_path, "w", encoding="utf-8") as lf:
                    lf.write(f"Title  : {title}\n")
                    lf.write(f"Prompt : {music_prompt}\n")
                    lf.write(f"Style  : {style_tags}\n")
                    lf.write("─"*40 + "\n\n")
                    lf.write(lyrics)
                print(f"      📄 Lyrics saved: {lyric_path}")
            else:
                print(f"      📄 No lyrics → Suno auto-generate")

            # Step 2 — Generate & download audio
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
    print("\n" + "═"*55)
    print("  🎵  SUNO AUTO GENERATOR v10")
    print("═"*55)

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

    batches = [
        to_process[i:i+MAX_PER_ACCOUNT]
        for i in range(0, len(to_process), MAX_PER_ACCOUNT)
    ]
    all_success = []

    for idx, (batch, token) in enumerate(zip(batches, SUNO_TOKENS)):
        success = process_batch(batch, music_prompt, style_tags, token, idx)
        all_success.extend(success)
        if idx < len(batches) - 1:
            print(f"\n  ⏳ Waiting 30 seconds before next account...")
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
