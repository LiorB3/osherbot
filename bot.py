import os
import re
import asyncio
import tempfile
import aiohttp
import discord
from discord.ext import commands
from discord import opus
from gtts import gTTS
from langdetect import detect, DetectorFactory

DetectorFactory.seed = 0

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set")

PIPE_PATH = "/tmp/osher_pipe"

# Ollama state
ollama_url: str = os.environ.get("OLLAMA_URL", "").rstrip("/")
ollama_model: str = "aminadaven/dictalm2.0-instruct:q2_k"

# Per-guild state
listening_channels: dict[int, int] = {}
tts_enabled_guilds: set[int] = set()
ai_enabled_guilds: set[int] = set()


def load_opus_lib():
    if opus.is_loaded():
        return True
    candidates = [
        "/nix/store/0py9xncsn0s6vqxhvqblvhs2cqbb30s8-libopus-1.5.2/lib/libopus.so",
        "/usr/lib/libopus.so",
        "/usr/local/lib/libopus.so",
    ]
    for path in candidates:
        try:
            opus.load_opus(path)
            print(f"Loaded opus from {path}", flush=True)
            return True
        except Exception:
            continue
    print("WARNING: Could not load opus library — voice will not work", flush=True)
    return False


load_opus_lib()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="bot ", intents=intents, help_command=None)

HEBREW_RE = re.compile(r"[\u0590-\u05FF\uFB1D-\uFB4F]")

LANG_CONFIG: dict[str, tuple[str, str, str]] = {
    "iw": ("iw", "com", "🇮🇱"),
    "en": ("en", "com", "🇺🇸"),
    "es": ("es", "com", "🇪🇸"),
    "fr": ("fr", "com", "🇫🇷"),
    "de": ("de", "com", "🇩🇪"),
    "it": ("it", "com", "🇮🇹"),
    "ru": ("ru", "com", "🇷🇺"),
    "ar": ("ar", "com", "🇸🇦"),
    "ja": ("ja", "com", "🇯🇵"),
    "ko": ("ko", "com", "🇰🇷"),
    "pt": ("pt", "com", "🇧🇷"),
    "nl": ("nl", "com", "🇳🇱"),
    "tr": ("tr", "com", "🇹🇷"),
    "pl": ("pl", "com", "🇵🇱"),
}

GTTS_LANG_MAP = {"he": "iw"}

# Help text in both languages
HELP_EN = (
    "**🤖 Bot Commands**\n"
    "`bot join` — join your voice channel and start TTS\n"
    "`bot leave` — leave the voice channel\n"
    "`bot stop` — pause TTS (AI stays on)\n"
    "`bot start` — resume TTS\n"
    "`bot say <message>` — speak a message directly\n"
    "`bot ai on/off` — toggle AI replies\n"
    "`bot seturl <url>` — set ngrok/Ollama URL\n"
    "`bot setmodel <model>` — switch Ollama model\n"
    "`bot status` — show current state\n"
    "`bot help` — show this list"
)

HELP_HE = (
    "**🤖 פקודות הבוט**\n"
    "`bot join` — הצטרף לערוץ קולי והפעל TTS\n"
    "`bot leave` — עזוב את הערוץ הקולי\n"
    "`bot stop` — השתק TTS (AI ממשיך לרוץ)\n"
    "`bot start` — חזור ל-TTS\n"
    "`bot say <הודעה>` — אמור הודעה ישירות\n"
    "`bot ai on/off` — הפעל/כבה תגובות AI\n"
    "`bot seturl <url>` — הגדר כתובת ngrok/Ollama\n"
    "`bot setmodel <מודל>` — החלף מודל Ollama\n"
    "`bot status` — הצג מצב נוכחי\n"
    "`bot help` — הצג רשימה זו"
)


def detect_lang_config(text: str) -> tuple[str, str, str]:
    if HEBREW_RE.search(text):
        return LANG_CONFIG["iw"]
    try:
        code = detect(text)
        mapped = GTTS_LANG_MAP.get(code, code)
        return LANG_CONFIG.get(mapped, LANG_CONFIG["en"])
    except Exception:
        return LANG_CONFIG["en"]


def is_hebrew_ctx(ctx: commands.Context) -> bool:
    """Guess language from the command message itself."""
    return bool(HEBREW_RE.search(ctx.message.content))


def make_tts_audio(text: str, lang: str, tld: str) -> str:
    try:
        tts = gTTS(text=text, lang=lang, tld=tld)
    except Exception:
        tts = gTTS(text=text, lang="en", tld="com")
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tts.save(tmp.name)
    return tmp.name


async def call_ollama(prompt: str) -> str | None:
    if not ollama_url:
        return None
    payload = {
        "model": ollama_model,
        "prompt": prompt,
        "stream": False,
        "system": "ענה בעברית בלבד. תשובה קצרה וברורה.",
    }
    try:
        headers = {"ngrok-skip-browser-warning": "true"}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ollama_url}/api/generate",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"Ollama error: HTTP {resp.status} — {body[:300]}", flush=True)
                    return None
                data = await resp.json()
                return data.get("response", "").strip()
    except Exception as e:
        print(f"Ollama request failed: {e}", flush=True)
        return None


async def pipe_listener():
    if not os.path.exists(PIPE_PATH):
        os.mkfifo(PIPE_PATH)
    print(f"Shell pipe ready — use:  echo 'your text' > {PIPE_PATH}", flush=True)
    loop = asyncio.get_event_loop()
    while True:
        try:
            raw = await loop.run_in_executor(None, _read_pipe_line)
            text = raw.strip()
            if not text:
                continue
            vc = next((vc for vc in bot.voice_clients if vc.is_connected()), None)
            if vc is None:
                print("Pipe: bot is not in a voice channel, ignoring.", flush=True)
                continue
            lang, tld, flag = detect_lang_config(text)
            print(f"Pipe → speaking ({flag}): {text!r}", flush=True)
            await speak(vc, text, lang, tld)
        except Exception as e:
            print(f"Pipe listener error: {e}", flush=True)
            await asyncio.sleep(1)


def _read_pipe_line() -> str:
    with open(PIPE_PATH, "r") as pipe:
        return pipe.readline()


async def speak(voice_client: discord.VoiceClient, text: str, lang: str, tld: str):
    if voice_client.is_playing():
        voice_client.stop()
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, make_tts_audio, text, lang, tld)

    def after_play(error):
        if error:
            print(f"Audio error: {error}", flush=True)
        try:
            os.unlink(path)
        except OSError:
            pass

    voice_client.play(discord.FFmpegPCMAudio(path), after=after_play)


async def react(message: discord.Message, emoji: str):
    try:
        await message.add_reaction(emoji)
    except Exception:
        pass


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})", flush=True)
    print(f"Opus loaded: {opus.is_loaded()}", flush=True)
    asyncio.create_task(pipe_listener())


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        await react(ctx.message, "❓")
    elif isinstance(error, commands.MissingRequiredArgument):
        await react(ctx.message, "❌")


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name="join")
async def join(ctx: commands.Context):
    if ctx.author.voice is None:
        await react(ctx.message, "❌")
        return
    channel = ctx.author.voice.channel
    if ctx.voice_client is not None:
        await ctx.voice_client.move_to(channel)
    else:
        await channel.connect()
    listening_channels[ctx.guild.id] = ctx.channel.id
    tts_enabled_guilds.add(ctx.guild.id)
    await react(ctx.message, "🔊")


@bot.command(name="stop")
async def stop(ctx: commands.Context):
    tts_enabled_guilds.discard(ctx.guild.id)
    await react(ctx.message, "🔇")


@bot.command(name="start")
async def start(ctx: commands.Context):
    if ctx.guild.id not in listening_channels:
        await react(ctx.message, "❌")
        return
    tts_enabled_guilds.add(ctx.guild.id)
    await react(ctx.message, "🔊")


@bot.command(name="leave")
async def leave(ctx: commands.Context):
    if ctx.voice_client is None:
        await react(ctx.message, "❌")
        return
    listening_channels.pop(ctx.guild.id, None)
    tts_enabled_guilds.discard(ctx.guild.id)
    ai_enabled_guilds.discard(ctx.guild.id)
    await ctx.voice_client.disconnect()
    await react(ctx.message, "👋")


@bot.command(name="say")
async def say_command(ctx: commands.Context, *, message: str):
    if ctx.voice_client is None:
        await react(ctx.message, "❌")
        return
    lang, tld, flag = detect_lang_config(message)
    await speak(ctx.voice_client, message, lang, tld)
    await react(ctx.message, flag)


@bot.command(name="seturl")
async def seturl(ctx: commands.Context, url: str):
    global ollama_url
    ollama_url = url.rstrip("/")
    print(f"Ollama URL updated to: {ollama_url}", flush=True)
    await react(ctx.message, "✅")


@bot.command(name="setmodel")
async def setmodel(ctx: commands.Context, *, model: str):
    global ollama_model
    ollama_model = model.strip()
    print(f"Ollama model updated to: {ollama_model}", flush=True)
    await react(ctx.message, "✅")


@bot.command(name="ai")
async def ai_toggle(ctx: commands.Context, state: str):
    state = state.lower()
    hebrew = is_hebrew_ctx(ctx)
    if state == "on":
        if not ollama_url:
            await react(ctx.message, "❌")
            msg = "לא הוגדרה כתובת Ollama. השתמש ב `bot seturl <url>`" if hebrew else "No Ollama URL set. Use `bot seturl <url>` first."
            await ctx.send(msg)
            return
        if ctx.guild.id not in listening_channels:
            await react(ctx.message, "❌")
            msg = "השתמש ב `bot join` תחילה." if hebrew else "Use `bot join` first."
            await ctx.send(msg)
            return
        ai_enabled_guilds.add(ctx.guild.id)
        await react(ctx.message, "🤖")
    elif state == "off":
        ai_enabled_guilds.discard(ctx.guild.id)
        await react(ctx.message, "💤")
    else:
        await react(ctx.message, "❓")


@bot.command(name="status")
async def status(ctx: commands.Context):
    gid = ctx.guild.id
    hebrew = is_hebrew_ctx(ctx)
    in_vc = ctx.voice_client is not None and ctx.voice_client.is_connected()
    tts = gid in tts_enabled_guilds
    ai = gid in ai_enabled_guilds
    model_short = ollama_model.split("/")[-1] if "/" in ollama_model else ollama_model
    url_display = ollama_url if ollama_url else ("לא מוגדר" if hebrew else "not set")

    if hebrew:
        await ctx.send(
            f"{'✅' if in_vc else '❌'} בערוץ קולי\n"
            f"{'🔊' if tts else '🔇'} TTS — {'פעיל' if tts else 'כבוי'}\n"
            f"{'🤖' if ai else '💤'} AI — {'פעיל' if ai else 'כבוי'}\n"
            f"🧠 מודל: `{model_short}`\n"
            f"🔗 כתובת: `{url_display}`"
        )
    else:
        await ctx.send(
            f"{'✅' if in_vc else '❌'} In voice channel\n"
            f"{'🔊' if tts else '🔇'} TTS — {'on' if tts else 'off'}\n"
            f"{'🤖' if ai else '💤'} AI — {'on' if ai else 'off'}\n"
            f"🧠 Model: `{model_short}`\n"
            f"🔗 URL: `{url_display}`"
        )


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    await ctx.send(HELP_HE if is_hebrew_ctx(ctx) else HELP_EN)


# ── Message handler ────────────────────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if message.author.bot:
        return
    if not message.guild:
        return

    guild_id = message.guild.id

    if guild_id not in listening_channels:
        return
    if message.channel.id != listening_channels[guild_id]:
        return
    if message.content.lower().startswith("bot "):
        return

    voice_client = message.guild.voice_client
    if voice_client is None or not voice_client.is_connected():
        return

    text = message.clean_content
    if not text:
        return
    if len(text) > 300:
        text = text[:300] + "..."

    lang, tld, flag = detect_lang_config(text)

    # TTS
    if guild_id in tts_enabled_guilds:
        await speak(voice_client, text, lang, tld)
        try:
            await message.add_reaction(flag)
        except Exception:
            pass

    # AI
    if guild_id in ai_enabled_guilds:
        async with message.channel.typing():
            reply = await call_ollama(text)
        if reply:
            while voice_client.is_playing():
                await asyncio.sleep(0.3)
            r_lang, r_tld, r_flag = detect_lang_config(reply)
            await speak(voice_client, reply, r_lang, r_tld)
            await message.channel.send(f"🤖 {reply}")
        else:
            await react(message, "⚠️")


bot.run(TOKEN)
