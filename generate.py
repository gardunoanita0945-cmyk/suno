#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║         SUNO AUTO GENERATOR — Full Script            ║
║  Input  : titles.txt + config.json                   ║
║  Output : output_audio/ (MP3 + TXT lyrics)           ║
║  Track  : done.txt (auto-update via git commit)      ║
╚══════════════════════════════════════════════════════╝
"""

import os
import json
import time
import requests
from google import genai
from suno import Suno, ModelVersions

# ══════════════════════════════════════════════════════
#  KONSTANTA
# ══════════════════════════════════════════════════════
MAX_PER_ACCOUNT = 8
OUTPUT_DIR      = "output_audio"
TITLES_FILE     = "titles.txt"
DONE_FILE       = "done.txt"
CONFIG_FILE     = "config.json"

# ══════════════════════════════════════════════════════
#  AMBIL ENV / SECRETS
# ══════════════════════════════════════════════════════
GEMINI_KEY     = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

raw_cookies  = os.environ.get("SUNO_COOKIES", "")
SUNO_COOKIES = [c.strip() for c in raw_cookies.split(",") if c.strip()]

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
    chars = r'\/:*?"<>|'
    name = title
    for c in chars:
        name = name.replace(c, "")
    return name.replace(" ", "_")

# ══════════════════════════════════════════════════════
#  LYRIC PROMPT TEMPLATE
# ══════════════════════════════════════════════════════
def _lyrics_prompt(title: str, music_prompt: str) -> str:
    return (
        f'Write song lyrics titled "{title}".\n'
        f"Genre/Mood: {music_prompt}.\n"
        f"Use this exact format:\n\n"
        f"[Verse 1]\n(verse 1 lyrics here)\n\n"
        f"[Chorus]\n(chorus lyrics here)\n\n"
        f"[Verse 2]\n(verse 2 lyrics here)\n\n"
        f"[Chorus]\n(chorus lyrics here)\n\n"
        f"[Outro]\n(outro lyrics here)\n\n"
        f"Rules:\n"
        f"- Maximum 300 words\n"
        f"- Write lyrics ONLY, no explanation or comments\n"
        f"- Write in English\n"
        f"- Make it emotional and meaningful\n"
    )

# ══════════════════════════════════════════════════════
#  LYRIC GENERATOR — GEMINI (utama, SDK baru)
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
            {
                "role": "system",
                "content": (
                    "You are a professional song lyric writer. "
                    "Always write lyrics in English with the requested format. "
                    "Do not add any explanation, only write the lyrics."
                ),
            },
            {
                "role": "user",
                "content": _lyrics_prompt(title, music_prompt),
            },
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

    raise RuntimeError("All LLM failed! Check GEMINI_API_KEY or OPENROUTER_API_KEY.")

# ══════════════════════════════════════════════════════
#  SUNO — GENERATE + DOWNLOAD
# ══════════════════════════════════════════════════════
def generate_audio(client: Suno, title: str, lyrics: str, style_tags: str) -> list:
    print(f"      🎼 Submitting to Suno: '{title}'")
    clips = client.generate(
        prompt=lyrics,
        tags=style_tags,
        title=title,
        is_custom=True,
        wait_audio=True,
    )
    if not clips:
        raise ValueError("Suno returned no audio clips")

    saved = []
    for i, clip in enumerate(clips):
        fname = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_{i+1}.mp3")
        client.download(song=clip, root=fname)
        saved.append(fname)
        print(f"      💾 Saved: {fname}")
    return saved

# ══════════════════════════════════════════════════════
#  PROSES 1 BATCH (1 AKUN, MAKS 8 LAGU)
# ══════════════════════════════════════════════════════
def process_batch(batch, music_prompt, style_tags, cookie, account_idx) -> list:
    print(f"\n  {'─'*50}")
    print(f"  🔑 Account #{account_idx + 1} | {len(batch)} songs")
    print(f"  {'─'*50}")

    client  = Suno(cookie=cookie, model_version=ModelVersions.CHIRP_V3_5)
    success = []

    for song_idx, title in enumerate(batch, 1):
        print(f"\n    [{song_idx}/{len(batch)}] 🎵 {title}")
        try:
            # Step 1 — Generate lyrics
            lyrics = generate_lyrics(title, music_prompt)

            # Save lyrics as TXT
            lyric_path = os.path.join(OUTPUT_DIR, f"{safe_filename(title)}_lyrics.txt")
            with open(lyric_path, "w", encoding="utf-8") as lf:
                lf.write(f"Title  : {title}\n")
                lf.write(f"Prompt : {music_prompt}\n")
                lf.write(f"Style  : {style_tags}\n")
                lf.write("─" * 40 + "\n\n")
                lf.write(lyrics)
            print(f"      📄 Lyrics saved: {lyric_path}")

            # Step 2 — Generate & download audio
            generate_audio(client, title, lyrics, style_tags)

            # Step 3 — Mark done
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
    print("  🎵  SUNO AUTO GENERATOR")
    print("═" * 55)

    if not SUNO_COOKIES:
        raise RuntimeError("❌ SUNO_COOKIES is empty! Add to GitHub Secrets.")
    if not GEMINI_KEY and not OPENROUTER_KEY:
        raise RuntimeError("❌ No LLM API key found!")

    config       = load_config()
    music_prompt = config["music_prompt"]
    style_tags   = config["style_tags"]

    all_titles     = load_titles()
    done_set       = load_done()
    pending        = [t for t in all_titles if t not in done_set]
    total_capacity = len(SUNO_COOKIES) * MAX_PER_ACCOUNT
    to_process     = pending[:total_capacity]

    print(f"\n  Suno accounts  : {len(SUNO_COOKIES)}")
    print(f"  Capacity/day   : {total_capacity} songs")
    print(f"  Total titles   : {len(all_titles)}")
    print(f"  Already done   : {len(done_set)}")
    print(f"  Will process   : {len(to_process)}")

    if not to_process:
        print("\n  ✅ All titles already processed!")
        print("  💡 Add new titles to titles.txt")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    batches     = [to_process[i:i+MAX_PER_ACCOUNT] for i in range(0, len(to_process), MAX_PER_ACCOUNT)]
    all_success = []

    for idx, (batch, cookie) in enumerate(zip(batches, SUNO_COOKIES)):
        success = process_batch(batch, music_prompt, style_tags, cookie, idx)
        all_success.extend(success)
        if idx < len(batches) - 1:
            print(f"\n  ⏳ Waiting 30 seconds before next account...")
            time.sleep(30)

    remaining = len(pending) - len(all_success)
    print("\n" + "═" * 55)
    print(f"  ✅ Success  : {len(all_success)} songs")
    print(f"  ❌ Failed   : {len(to_process) - len(all_success)} songs")
    print(f"  📋 Remaining: {remaining} titles")
    print(f"  📁 Output   : ./{OUTPUT_DIR}/")
    print("═" * 55 + "\n")

    if len(all_success) == 0 and len(to_process) > 0:
        raise RuntimeError("No songs were successfully generated!")

if __name__ == "__main__":
    main()
