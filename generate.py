#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║         SUNO AUTO GENERATOR — Full Script v4         ║
║  Auth   : JWT Token (window.Clerk.session.getToken)  ║
║  Input  : titles.txt + config.json + accounts.json   ║
║  Output : output_audio/ (MP3 + TXT lyrics)           ║
║  Track  : done.txt                                   ║
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
MAX_PER_ACCOUNT  = 8
OUTPUT_DIR       = "output_audio"
TITLES_FILE      = "titles.txt"
DONE_FILE        = "done.txt"
CONFIG_FILE      = "config.json"
ACCOUNTS_FILE    = "accounts.json"
SUNO_GENERATE    = "https://studio-api.suno.ai/api/generate/v2/"
SUNO_FEED        = "https://studio-api.suno.ai/api/feed/"
POLL_INTERVAL    = 15   # detik antar poll
POLL_MAX_RETRY   = 40   # maks coba (40 x 15s = 10 menit)

# ══════════════════════════════════════════════════════
#  AMBIL ENV / SECRETS
# ══════════════════════════════════════════════════════
GEMINI_KEY     = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

# SUNO_TOKENS: bisa plain token, JSON array, atau dipisah koma
raw_tokens = os.environ.get("SUNO_TOKENS", "").strip()

if not raw_tokens:
    SUNO_TOKENS = []
elif raw_tokens.startswith("["):
    # Format JSON array: ["token1","token2"]
    try:
        SUNO_TOKENS = json.loads(raw_tokens)
    except Exception:
        SUNO_TOKENS = []
else:
    # Format plain: token langsung, atau dipisah koma/baris baru
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

def load_accounts() -> list:
    if not os.path.exists(ACCOUNTS_FILE):
        return [{"label": f"account_{i+1}"} for i in range(len(SUNO_TOKENS))]
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def safe_filename(title: str) -> str:
    for c in r'\/:*?"<>|':
        title = title.replace(c, "")
    return title.replace(" ", "_")

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
        f"- Write lyrics ONLY, no explanation\n"
        f"- Write in English\n"
        f"- Make it emotional and meaningful\n"
    )

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — GEMINI
# ══════════════════════════════════════════════════════
def generate_lyrics_gemini(title: str, music_prompt: str) -> str:
    client   = genai.Client(api_key=GEMINI_KEY)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=_lyrics_prompt(title, music_prompt)
    )
    lyrics = response.text.strip()
    if not lyrics:
        raise ValueError("Gemini returned empty response")
    return lyrics

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — OPENROUTER (cadangan)
# ══════════════════════════════════════════════════════
def generate_lyrics_openrouter(title: str, music_prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/suno-auto-generator",
        "X-Title": "Suno Auto Generator",
    }
    payload = {
        "model": "meta-llama/llama-3.1-8b-instruct:free",
        "messages": [
            {"role": "system", "content": "You are a professional song lyric writer. Write lyrics in English only. No explanation."},
            {"role": "user",   "content": _lyrics_prompt(title, music_prompt)},
        ],
        "max_tokens": 800,
        "temperature": 0.8,
    }
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers, json=payload, timeout=60
    )
    resp.raise_for_status()
    lyrics = resp.json()["choices"][0]["message"]["content"].strip()
    if not lyrics:
        raise ValueError("OpenRouter returned empty response")
    return lyrics

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — AUTO FALLBACK
# ══════════════════════════════════════════════════════
def generate_lyrics(title: str, music_prompt: str) -> str:
    if GEMINI_KEY:
        try:
            print("      📝 Generating lyrics (Gemini)...")
            lyrics = generate_lyrics_gemini(title, music_prompt)
            print("      ✅ Gemini success")
            return lyrics
        except Exception as e:
            print(f"      ⚠️  Gemini failed → {e}")
    if OPENROUTER_KEY:
        try:
            print("      📝 Fallback to OpenRouter...")
            lyrics = generate_lyrics_openrouter(title, music_prompt)
            print("      ✅ OpenRouter success")
            return lyrics
        except Exception as e:
            print(f"      ❌ OpenRouter failed → {e}")
    raise RuntimeError("All LLM failed! Check API keys.")

# ══════════════════════════════════════════════════════
#  SUNO DIRECT API — GENERATE
# ══════════════════════════════════════════════════════
def suno_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

def suno_generate(token: str, title: str, lyrics: str, style_tags: str) -> list:
    """Submit ke Suno, return list clip_id."""
    payload = {
        "mv": "chirp-v3-5",
        "prompt": lyrics,
        "tags": style_tags,
        "title": title,
        "make_instrumental": False,
        "continue_clip_id": None,
        "continue_at": None,
    }
    resp = requests.post(
        SUNO_GENERATE,
        headers=suno_headers(token),
        json=payload,
        timeout=60
    )
    if resp.status_code != 200:
        raise Exception(f"Suno generate failed: {resp.status_code} → {resp.text[:200]}")

    data = resp.json()
    clips = data.get("clips", [])
    if not clips:
        raise Exception(f"Suno returned no clips: {data}")
    ids = [c["id"] for c in clips]
    print(f"      🎬 Clip IDs: {ids}")
    return ids

def suno_poll(token: str, clip_ids: list) -> list:
    """Poll sampai semua clip selesai, return list dict clip."""
    ids_str = ",".join(clip_ids)
    print(f"      ⏳ Polling audio status...")
    for attempt in range(POLL_MAX_RETRY):
        resp = requests.get(
            f"{SUNO_FEED}?ids={ids_str}",
            headers=suno_headers(token),
            timeout=30
        )
        if resp.status_code != 200:
            print(f"      ⚠️  Poll error {resp.status_code}, retry...")
            time.sleep(POLL_INTERVAL)
            continue

        clips = resp.json()
        statuses = [c.get("status", "") for c in clips]
        print(f"      📊 Status: {statuses} (attempt {attempt+1}/{POLL_MAX_RETRY})")

        if all(s == "complete" for s in statuses):
            return clips
        if any(s == "error" for s in statuses):
            raise Exception(f"Suno clip error: {clips}")

        time.sleep(POLL_INTERVAL)

    raise Exception("Polling timeout — audio tidak selesai dalam 10 menit")

def suno_download(clips: list, title: str) -> list:
    """Download semua clip ke output_audio/."""
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

def generate_audio(token: str, title: str, lyrics: str, style_tags: str) -> list:
    print(f"      🎼 Submitting to Suno: '{title}'")
    clip_ids = suno_generate(token, title, lyrics, style_tags)
    clips    = suno_poll(token, clip_ids)
    saved    = suno_download(clips, title)
    return saved

# ══════════════════════════════════════════════════════
#  PROSES 1 BATCH (1 AKUN, MAKS 8 LAGU)
# ══════════════════════════════════════════════════════
def process_batch(batch, music_prompt, style_tags, token, account_label, account_idx) -> list:
    print(f"\n  {'─'*52}")
    print(f"  🔑 Account #{account_idx+1}: {account_label} | {len(batch)} songs")
    print(f"  {'─'*52}")

    success = []
    for song_idx, title in enumerate(batch, 1):
        print(f"\n    [{song_idx}/{len(batch)}] 🎵 {title}")
        try:
            # Step 1 — Lirik
            lyrics = generate_lyrics(title, music_prompt)
            lyric_path = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_lyrics.txt")
            with open(lyric_path, "w", encoding="utf-8") as lf:
                lf.write(f"Title  : {title}\n")
                lf.write(f"Prompt : {music_prompt}\n")
                lf.write(f"Style  : {style_tags}\n")
                lf.write("─" * 40 + "\n\n")
                lf.write(lyrics)
            print(f"      📄 Lyrics saved: {lyric_path}")

            # Step 2 — Audio
            generate_audio(token, title, lyrics, style_tags)

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
    print("  🎵  SUNO AUTO GENERATOR v4")
    print("═" * 55)

    if not SUNO_TOKENS:
        raise RuntimeError("❌ SUNO_TOKENS kosong! Tambahkan ke GitHub Secrets.")
    if not GEMINI_KEY and not OPENROUTER_KEY:
        raise RuntimeError("❌ Tidak ada API key LLM!")

    config       = load_config()
    music_prompt = config["music_prompt"]
    style_tags   = config["style_tags"]
    accounts     = load_accounts()

    all_titles     = load_titles()
    done_set       = load_done()
    pending        = [t for t in all_titles if t not in done_set]
    total_capacity = len(SUNO_TOKENS) * MAX_PER_ACCOUNT
    to_process     = pending[:total_capacity]

    print(f"\n  Accounts loaded : {len(SUNO_TOKENS)}")
    for i, acc in enumerate(accounts[:len(SUNO_TOKENS)]):
        print(f"    #{i+1} → {acc.get('label','?')}")
    print(f"\n  Capacity/day    : {total_capacity} songs")
    print(f"  Total titles    : {len(all_titles)}")
    print(f"  Already done    : {len(done_set)}")
    print(f"  Will process    : {len(to_process)}")

    if not to_process:
        print("\n  ✅ All titles already processed!")
        print("  💡 Add new titles to titles.txt")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    batches     = [to_process[i:i+MAX_PER_ACCOUNT] for i in range(0, len(to_process), MAX_PER_ACCOUNT)]
    all_success = []

    for idx, (batch, token) in enumerate(zip(batches, SUNO_TOKENS)):
        label   = accounts[idx].get("label", f"account_{idx+1}") if idx < len(accounts) else f"account_{idx+1}"
        success = process_batch(batch, music_prompt, style_tags, token, label, idx)
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
