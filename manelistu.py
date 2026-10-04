import asyncio
import base64
import os
import re
import time
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

yt_dlp.utils.bug_reports_message = lambda *args, **kwargs: ''

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}

# FĂRĂ filtru de volum aici! Volumul e controlat doar de PCMVolumeTransformer,
# ca să poată fi schimbat în timp real.
ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

MAX_VOLUME = 2.0  # 200%

# ---------------- Spotify ----------------
# Pune cheile ca variabile de mediu (sau înlocuiește os.getenv(...) cu textul direct)
SPOTIFY_CLIENT_ID = os.getenv("0508cdca27fd49a09f27730d81bada98")
SPOTIFY_CLIENT_SECRET = os.getenv("4e1d14677cad4cecacd8c605ee1b9a06")
SPOTIFY_URL_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)")
SPOTIFY_MAX_TRACKS = 100


def _fmt_track(t: dict) -> str:
    artist = t['artists'][0]['name'] if t.get('artists') else ''
    return f"{artist} - {t['name']}".strip(' -')


class SpotifyClient:
    API = "https://api.spotify.com/v1"

    def __init__(self):
        self.token = None
        self.expires = 0

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        if self.token and time.time() < self.expires - 30:
            return self.token
        auth = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {auth}"},
        ) as r:
            r.raise_for_status()
            j = await r.json()
        self.token = j["access_token"]
        self.expires = time.time() + j["expires_in"]
        return self.token

    async def _get(self, session: aiohttp.ClientSession, path: str) -> dict:
        token = await self._get_token(session)
        async with session.get(f"{self.API}{path}", headers={"Authorization": f"Bearer {token}"}) as r:
            r.raise_for_status()
            return await r.json()

    async def search(self, text: str):
        """Caută o melodie pe Spotify și returnează 'Artist - Titlu' (sau None)."""
        async with aiohttp.ClientSession() as session:
            res = await self._get(session, f"/search?q={quote(text)}&type=track&limit=1")
        items = res.get('tracks', {}).get('items', [])
        return _fmt_track(items[0]) if items else None

    async def resolve(self, url: str):
        """Returnează (nume, [căutări 'Artist - Titlu']) pentru un link Spotify."""
        kind, sid = SPOTIFY_URL_RE.search(url).groups()
        async with aiohttp.ClientSession() as session:
            if kind == 'track':
                t = await self._get(session, f"/tracks/{sid}")
                return t['name'], [_fmt_track(t)]

            if kind == 'album':
                a = await self._get(session, f"/albums/{sid}")
                tracks = [_fmt_track(t) for t in a['tracks']['items']]
                return a['name'], tracks[:SPOTIFY_MAX_TRACKS]

            # playlist
            pl = await self._get(session, f"/playlists/{sid}?fields=name,tracks.items(track(name,artists(name)))")
            container = pl.get('tracks') or pl.get('items') or {}
            tracks = []
            for item in container.get('items', []):
                t = item.get('track') or item.get('item')
                if t and t.get('name'):
                    tracks.append(_fmt_track(t))
            return pl['name'], tracks[:SPOTIFY_MAX_TRACKS]


spotify = SpotifyClient()


# Prefixuri de căutare yt-dlp pentru fiecare sursă
SEARCH_PREFIXES = {
    'auto': ['ytsearch', 'scsearch'],   # întâi YouTube, apoi SoundCloud
    'youtube': ['ytsearch'],
    'soundcloud': ['scsearch'],
}


async def extract(query: str, source: str = 'auto', *, loop=None, download=False):
    """Caută/extrage o melodie. Link-urile merg direct (orice site suportat de yt-dlp),
    textul simplu e căutat pe sursa aleasă, cu fallback în modul 'auto'."""
    loop = loop or asyncio.get_running_loop()
    prefixes = SEARCH_PREFIXES.get(source, SEARCH_PREFIXES['auto'])

    async def attempt(candidates):
        last_error = None
        for candidate in candidates:
            try:
                data = await loop.run_in_executor(
                    None, lambda c=candidate: ytdl.extract_info(c, download=download)
                )
                if 'entries' in data:
                    entries = [e for e in data['entries'] if e]
                    if not entries:
                        continue
                    data = entries[0]
                return data, last_error
            except Exception as e:
                last_error = e
        return None, last_error

    # Link direct: nu căutăm nimic, doar îl încercăm
    if query.startswith(('http://', 'https://')):
        data, err = await attempt([query])
        if data:
            return data
        raise err or Exception("Link-ul nu a putut fi redat.")

    # 1) YouTube / SoundCloud cu textul introdus
    data, err = await attempt([f"{p}1:{query}" for p in prefixes])
    if data:
        return data

    # 2) Plan C: Spotify corectează numele (artist + titlu exact), apoi reîncercăm
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            corrected = await spotify.search(query)
        except Exception as e:
            print(f'Eroare căutare Spotify: {e}')
            corrected = None
        if corrected and corrected.lower() != query.lower():
            data, err2 = await attempt([f"{p}1:{corrected}" for p in prefixes])
            if data:
                return data
            err = err2 or err

    raise err or Exception("Nu am găsit nimic pentru căutarea asta.")


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, query, *, source='auto', loop=None, stream=True, volume=0.5):
        data = await extract(query, source, loop=loop, download=not stream)
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data, volume=volume)


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.queues = {}
        self.volumes = {}

    async def setup_hook(self):
        await self.tree.sync()
        print("Comenzile Slash au fost sincronizate global.")

    def set_volume(self, guild: discord.Guild, value: float) -> float:
        """Setează volumul (0.0 - 2.0) și îl aplică imediat melodiei curente."""
        value = round(max(0.0, min(MAX_VOLUME, value)), 2)
        self.volumes[guild.id] = value
        vc = guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = value
        return value


bot = MusicBot()


# Panoul de control cu butoane interactive
class MusicControls(discord.ui.View):
    def __init__(self, bot_instance: MusicBot):
        super().__init__(timeout=None)
        self.bot_instance = bot_instance

    @discord.ui.button(label="Pauză/Play", style=discord.ButtonStyle.blurple, emoji="⏯️")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("❌ Botul nu este conectat.", ephemeral=True)
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Muzica a fost pusă pe pauză.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Muzica a fost reluată.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nu cântă nimic acum.", ephemeral=True)

    @discord.ui.button(label="Volum -", style=discord.ButtonStyle.grey, emoji="🔉")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = self.bot_instance.volumes.get(interaction.guild.id, 0.5)
        new_vol = self.bot_instance.set_volume(interaction.guild, current - 0.1)
        await interaction.response.send_message(f"🔉 Volum setat la: {int(new_vol * 100)}%", ephemeral=True)

    @discord.ui.button(label="Volum +", style=discord.ButtonStyle.grey, emoji="🔊")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = self.bot_instance.volumes.get(interaction.guild.id, 0.5)
        new_vol = self.bot_instance.set_volume(interaction.guild, current + 0.1)
        await interaction.response.send_message(f"🔊 Volum setat la: {int(new_vol * 100)}%", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.red, emoji="⏹️")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        self.bot_instance.queues.get(interaction.guild.id, []).clear()
        if vc:
            await vc.disconnect()
            await interaction.response.send_message("⏹️ Botul a fost oprit și deconectat.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Botul nu este conectat pe niciun canal.", ephemeral=True)


@bot.event
async def on_ready():
    print(f'Autentificat ca {bot.user} (ID: {bot.user.id})')
    print('------')


def play_next(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    queue = bot.queues.get(guild_id)
    if queue:
        next_query, next_source = queue.pop(0)
        asyncio.run_coroutine_threadsafe(play_song(interaction, next_query, next_source), bot.loop)


async def play_song(interaction: discord.Interaction, url: str, source: str = 'auto') -> bool:
    guild_id = interaction.guild.id
    vc = interaction.guild.voice_client

    if not vc or not vc.is_connected():
        return False

    volume = bot.volumes.get(guild_id, 0.5)

    try:
        player = await YTDLSource.from_url(url, source=source, loop=bot.loop, stream=True, volume=volume)
    except Exception as e:
        print(f'Eroare yt-dlp: {e}')
        try:
            await interaction.channel.send(
                f"❌ Nu am găsit/putut reda **{url}** (sar peste ea): `{str(e)[:120]}`"
            )
        except Exception:
            pass
        # trecem la următoarea din coadă
        play_next(interaction)
        return False

    def after_playing(error):
        if error:
            print(f'Eroare la redare: {error}')
        play_next(interaction)

    vc.play(player, after=after_playing)

    try:
        await interaction.channel.send(f"🎶 Acum se cântă: **{player.title}**", view=MusicControls(bot))
    except Exception:
        pass
    return True


@bot.tree.command(name='play', description='Redă o melodie (YouTube, SoundCloud sau orice link suportat)')
@app_commands.describe(
    query='Numele melodiei sau un link (YouTube, SoundCloud, Bandcamp, Mixcloud etc.)',
    sursa='De unde să caute (implicit: Auto = YouTube, apoi SoundCloud)',
)
@app_commands.choices(sursa=[
    app_commands.Choice(name='Auto (YouTube → SoundCloud)', value='auto'),
    app_commands.Choice(name='YouTube', value='youtube'),
    app_commands.Choice(name='SoundCloud', value='soundcloud'),
])
async def play(interaction: discord.Interaction, query: str, sursa: app_commands.Choice[str] = None):
    source = sursa.value if sursa else 'auto'
    await interaction.response.defer()

    if not interaction.user.voice:
        return await interaction.followup.send("❌ Trebuie să fii într-un canal de voce!")

    channel = interaction.user.voice.channel
    guild_id = interaction.guild.id

    vc = interaction.guild.voice_client
    if not vc:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    bot.queues.setdefault(guild_id, [])

    # Link Spotify -> listă de căutări "Artist - Titlu"
    spotify_label = None
    if SPOTIFY_URL_RE.search(query):
        if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
            return await interaction.followup.send(
                "❌ Spotify nu e configurat. Setează SPOTIFY_CLIENT_ID și SPOTIFY_CLIENT_SECRET."
            )
        try:
            spotify_label, tracks = await spotify.resolve(query)
        except Exception as e:
            print(f'Eroare Spotify: {e}')
            return await interaction.followup.send(
                "❌ Nu am putut citi link-ul de Spotify (playlist privat sau cheie greșită?)."
            )
        if not tracks:
            return await interaction.followup.send("❌ Nu am găsit nicio melodie în link-ul de Spotify.")
        items = [(t, 'auto') for t in tracks]
    else:
        items = [(query, source)]

    if vc.is_playing() or vc.is_paused():
        bot.queues[guild_id].extend(items)
        if spotify_label:
            return await interaction.followup.send(
                f"📥 Am adăugat în coadă {len(items)} melodii din Spotify: **{spotify_label}**"
            )
        try:
            data = await extract(query, source, loop=bot.loop, download=False)
            title = data.get('title', query)
        except Exception:
            title = query
        return await interaction.followup.send(f"📥 Adăugat în coadă: **{title}**")

    bot.queues[guild_id].extend(items[1:])
    first_query, first_source = items[0]
    ok = await play_song(interaction, first_query, first_source)
    if ok:
        if spotify_label and len(items) > 1:
            await interaction.followup.send(
                f"▶️ Spotify: **{spotify_label}**, {len(items)} melodii (restul sunt în coadă)"
            )
        else:
            await interaction.followup.send("▶️ Gata, se redă!")
    else:
        await interaction.followup.send("❌ Nu am reușit să pornesc melodia.")


@bot.tree.command(name='skip', description='Treci la următoarea melodie din coadă')
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭️ S-a dat skip la melodie.")
    else:
        await interaction.response.send_message("❌ Nu rulează nicio melodie în acest moment.", ephemeral=True)


@bot.tree.command(name='volume', description='Setează volumul botului (de la 0 la 200)')
@app_commands.describe(valoare='Nivelul volumului (procent între 0 și 200)')
async def volume(interaction: discord.Interaction, valoare: app_commands.Range[int, 0, 200]):
    new_vol = bot.set_volume(interaction.guild, valoare / 100.0)
    await interaction.response.send_message(f"🔊 Volumul a fost setat la **{int(new_vol * 100)}%**!")


@bot.tree.command(name='stop', description='Oprește muzica, șterge coada și deconectează botul')
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    bot.queues.get(interaction.guild.id, []).clear()

    if vc and vc.is_connected():
        await vc.disconnect()
        await interaction.response.send_message("⏹️ Muzica a fost oprită, coada a fost golită, iar botul s-a deconectat.")
    else:
        await interaction.response.send_message("❌ Botul nu este conectat pe niciun canal de voce.", ephemeral=True)


# Introdu aici token-ul botului tău de Discord
bot.run("MTU1NDEzNjU5MDgxMTQwNjQ1Nw.GIQ3Qe.uwVmGHEytDFp9u3pQ6sZgDmbCB1htjKS2MJ_wk")
