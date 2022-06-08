import discord, asyncio, datetime, time, discord_components, sqlite3, asyncio, functools, itertools, math, random, youtube_dl
from pkg_resources import ResolutionError
from discord import channel, colour, reaction
from discord.ext import commands, tasks
from datetime import date, datetime, timezone
from discord_components import DiscordComponents, Select, SelectOption, Button, ButtonStyle, ComponentsBot
from async_timeout import timeout

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=['bugs '], description = "Bot acadêmico UNIFAL - Turma de 2022", intents = intents)
DiscordComponents(bot)
client = discord.Client(intents=intents)

banco = sqlite3.connect('db.sqlite')
cursor = banco.cursor()

youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass

class YTDLError(Exception):
    pass

class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
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

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)

class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Tocando agora',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.orange())
                 .add_field(name='Duração', value=self.source.duration)
                 .add_field(name='A pedido de', value=self.requester.mention)
                 .add_field(name='Autor', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))
        return embed

class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]

class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('Esse tipo de comando não pode ser utilizado no privado.')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('Um erro aconteceu: {}'.format(str(error)))

    @commands.command(name='conectar', invoke_without_subcommand=True, aliases=['con', 'c', 'entrar'])
    async def _join(self, ctx: commands.Context):
        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='sair', aliases=['desconectar'])
    async def _leave(self, ctx: commands.Context):

        if not ctx.voice_state.voice:
            embedVar = discord.Embed(title="Não estou conectado a nenhum canal de voz!", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            return await ctx.send(embed=embedVar)

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name='agora', aliases=['atual', 'tocando'])
    async def _now(self, ctx: commands.Context):
        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pausar')
    async def _pause(self, ctx: commands.Context):
        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='continuar')
    async def _resume(self, ctx: commands.Context):
        if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='limpar', aliases=['clear'])
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()

        if not ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='skip', aliases=['pular','s','proximo','proxima'])
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            embedVar = discord.Embed(title="Não existe uma música tocando no momento!", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            return await ctx.send(embed=embedVar)
        
        embedVar = discord.Embed(title="Pulando a música!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        
        await ctx.reply(embed=embedVar)
        ctx.voice_state.skip()

    @commands.command(name='fila')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue.

        You can optionally specify the page to show. Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            embedVar = discord.Embed(title="Não existe nenhuma música na fila", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            return await ctx.send(embed=embedVar)

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(title="Fila de músicas",colour = discord.Colour.red(), description='**{} músicas:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Vendo a página {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='embaralhar')
    async def _shuffle(self, ctx: commands.Context):
        """Shuffles the queue."""

        if len(ctx.voice_state.songs) == 0:
            embedVar = discord.Embed(title="Não existem músicas na fila", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            return await ctx.send(embed=embedVar)

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remover')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            embedVar = discord.Embed(title="Não existe nenhuma música na fila", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            return await ctx.send(embed=embedVar)
        
        if (index - 1) > len(ctx.voice_state.songs):
            embedVar = discord.Embed(title="Não existe essa música na fila", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            return await ctx.send(embed=embedVar)

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        if not ctx.voice_state.is_playing:
            embedVar = discord.Embed(title="Não estou tocando nada no momento!", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            return await ctx.send(embed=embedVar)

        ctx.voice_state.loop = not ctx.voice_state.loop
        if ctx.voice_state == False:
            embedVar = discord.Embed(title="O loop de música foi desativado com sucesso!", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            return await ctx.send(embed=embedVar)
        if ctx.voice_state == True:
            embedVar = discord.Embed(title="O loop de música foi ativado com sucesso!", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            return await ctx.send(embed=embedVar)

    @commands.command(name='play', aliases=['p', 'tocar'])
    async def _play(self, ctx: commands.Context, *, search: str):
        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send('Um erro aconteceu enquanto eu processava a música: {}'.format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                embedVar = discord.Embed(title="{} foi adicionado na fila com sucesso!".format(str(source)), colour = discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                await ctx.send(embed=embedVar)

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('Você não está conectado em nenhum canal de voz.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('O bot já está em um canal de voz')

bot.add_cog(Music(bot))


@bot.command(name="horario", aliases=["aulas"])
async def horario(ctx):
    embedVar = discord.Embed(title="Qual horário você deseja receber?", colour = discord.Colour.orange())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    question = await ctx.reply(embed=embedVar, components = [
        [Button(label = "Hoje", style = ButtonStyle.red, custom_id = "today"),
        Button(label = "Amanhã", style = ButtonStyle.red, custom_id = "tomorrow"),
        Button(label = "Geral", style = ButtonStyle.red, custom_id = "week")]
    ])
    
    request = await bot.wait_for("button_click", check = lambda i: i.author == ctx.author, timeout=90)

    if request.custom_id == "today":
        await question.delete()
        n = int(date.today().weekday())
        diasDaSemana = ["Segunda-feira","Terça-feira","Quarta-feira","Quinta-feira","Sexta-feira","Sábado","Domingo"]
        horario = discord.Embed(title=f"{diasDaSemana[n]}", description="Mostrando o horário de hoje", colour = discord.Colour.orange())
        horario.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        horario.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\n⇒ O horário foi retirado do Sistema Acadêmico da UNIFAL", icon_url=ctx.author.avatar_url)
    
        if diasDaSemana[n] == "Segunda-feira":
            horario.add_field(name="Geometria Analítica", value="🏠 Sala: 204\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Tiago Arruda")
            horario.add_field(name="AEDs I", value="🏠 Sala: 201\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Paulo Axelandre Bressan")

        if diasDaSemana[n] == "Terça-feira":
            horario.add_field(name="Lógica Digital", value="🏠 Sala: 201\n⌚ Horário: 10:00 - 12:00\n📗 Responsável: Eliseu César Miguel")
            horario.add_field(name="Fundamentos Mat. p/ Cien. da Comp.", value="🏠 Sala: 201\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Nelson José Freitas da Silveira")
            horario.add_field(name="AEDs I", value="🏠 Sala: 201\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Paulo Alexandre Bressan")
        
        if diasDaSemana[n] == "Quarta-feira":
            horario.add_field(name="Lógica Digital", value="🏠 Sala: 201\n⌚ Horário: 10:00 - 12:00\n📗 Responsável: Eliseu César Miguel")
            horario.add_field(name="Geometria Analítica", value="🏠 Sala: 204\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Tiago Arruda")
            horario.add_field(name="AEDs I", value="🏠 Sala: 207\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Luiz Eduardo da Silva")
        
        if diasDaSemana[n] == "Quinta-feira":
            horario.add_field(name="AEDs I", value="🏠 Sala: 207\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Luiz Eduardo da Silva")
            horario.add_field(name="Intro. à Ciência a Computação", value="🏠 Sala: 204\n⌚ Horário: 19:00 - 21:00\n📗 Responsável: Ricardo Menezes Salgado")
        
        if diasDaSemana[n] == "Sexta-feira":
            horario.add_field(name="Fundamentos Mat. p/ Cien. da Comp.", value="🏠 Sala: 201\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Nelson José Freitas da Silveira")
            horario.add_field(name="Intro. à Ciência da Computação", value="🏠 Sala: 207\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Adriana Aparecida de Avila")

        if (diasDaSemana[n] == "Sábado") or (diasDaSemana[n] == "Domingo"): 
            horario.add_field(name=f"Sem horário definido para {diasDaSemana[n]}", value="Aproveite o seu dia!")
        
        await ctx.reply(embed=horario)

    elif request.custom_id == "tomorrow":
        await question.delete()
        n = int(date.today().weekday())

        if n != 6:
            n += 1
        else:
            n = 0

        diasDaSemana = ["Segunda-feira","Terça-feira","Quarta-feira","Quinta-feira","Sexta-feira","Sábado","Domingo"]
        horario = discord.Embed(title=f"{diasDaSemana[n]}", description="Mostrando o horário de hoje", colour = discord.Colour.orange())
        horario.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        horario.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\n⇒ O horário foi retirado do Sistema Acadêmico da UNIFAL", icon_url=ctx.author.avatar_url)
    
        if diasDaSemana[n] == "Segunda-feira":
            horario.add_field(name="Geometria Analítica", value="🏠 Sala: 204\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Tiago Arruda")
            horario.add_field(name="AEDs I", value="🏠 Sala: 201\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Paulo Axelandre Bressan")

        if diasDaSemana[n] == "Terça-feira":
            horario.add_field(name="Lógica Digital", value="🏠 Sala: 201\n⌚ Horário: 10:00 - 12:00\n📗 Responsável: Eliseu César Miguel")
            horario.add_field(name="Fundamentos Mat. p/ Cien. da Comp.", value="🏠 Sala: 201\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Nelson José Freitas da Silveira")
            horario.add_field(name="AEDs I", value="🏠 Sala: 201\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Paulo Alexandre Bressan")
        
        if diasDaSemana[n] == "Quarta-feira":
            horario.add_field(name="Lógica Digital", value="🏠 Sala: 201\n⌚ Horário: 10:00 - 12:00\n📗 Responsável: Eliseu César Miguel")
            horario.add_field(name="Geometria Analítica", value="🏠 Sala: 204\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Tiago Arruda")
            horario.add_field(name="AEDs I", value="🏠 Sala: 207\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Luiz Eduardo da Silva")
        
        if diasDaSemana[n] == "Quinta-feira":
            horario.add_field(name="AEDs I", value="🏠 Sala: 207\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Luiz Eduardo da Silva")
            horario.add_field(name="Intro. à Ciência a Computação", value="🏠 Sala: 204\n⌚ Horário: 19:00 - 21:00\n📗 Responsável: Ricardo Menezes Salgado")
        
        if diasDaSemana[n] == "Sexta-feira":
            horario.add_field(name="Fundamentos Mat. p/ Cien. da Comp.", value="🏠 Sala: 201\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Nelson José Freitas da Silveira")
            horario.add_field(name="Intro. à Ciência da Computação", value="🏠 Sala: 207\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Adriana Aparecida de Avila")

        if (diasDaSemana[n] == "Sábado") or (diasDaSemana[n] == "Domingo"): 
            horario.add_field(name=f"Sem horário definido para {diasDaSemana[n]}", value="Aproveite o seu dia!")
        
        await ctx.reply(embed=horario)

    elif request.custom_id == "week":
        await question.delete()
        pages = 1
        valid_reaction = ['◀️','▶️']

        segunda_feira = discord.Embed(title="Horário Acadêmico", description="Segunda-feira", colour = discord.Colour.orange())
        segunda_feira.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\n⇒ O horário foi retirado do Sistema Acadêmico da UNIFAL", icon_url=ctx.author.avatar_url)
        segunda_feira.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        segunda_feira.add_field(name="Geometria Analítica", value="🏠 Sala: 204\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Tiago Arruda")
        segunda_feira.add_field(name="AEDs I", value="🏠 Sala: 201\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Paulo Axelandre Bressan")

        terca_feira = discord.Embed(title="Horário Acadêmico", description="Terça-feira", colour = discord.Colour.orange())
        terca_feira.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\n⇒ O horário foi retirado do Sistema Acadêmico da UNIFAL", icon_url=ctx.author.avatar_url)
        terca_feira.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        terca_feira.add_field(name="Lógica Digital", value="🏠 Sala: 201\n⌚ Horário: 10:00 - 12:00\n📗 Responsável: Eliseu César Miguel")
        terca_feira.add_field(name="Fundamentos Mat. p/ Cien. da Comp.", value="🏠 Sala: 201\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Nelson José Freitas da Silveira")
        terca_feira.add_field(name="AEDs I", value="🏠 Sala: 201\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Paulo Alexandre Bressan")       

        quarta_feira = discord.Embed(title="Horário Acadêmico", description="Quarta-feira", colour = discord.Colour.orange())
        quarta_feira.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\n⇒ O horário foi retirado do Sistema Acadêmico da UNIFAL", icon_url=ctx.author.avatar_url)
        quarta_feira.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        quarta_feira.add_field(name="Lógica Digital", value="🏠 Sala: 201\n⌚ Horário: 10:00 - 12:00\n📗 Responsável: Eliseu César Miguel")
        quarta_feira.add_field(name="Geometria Analítica", value="🏠 Sala: 204\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Tiago Arruda")
        quarta_feira.add_field(name="AEDs I", value="🏠 Sala: 207\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Luiz Eduardo da Silva")

        quinta_feira = discord.Embed(title="Horário Acadêmico", description="Quinta-feira", colour = discord.Colour.orange())
        quinta_feira.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\n⇒ O horário foi retirado do Sistema Acadêmico da UNIFAL", icon_url=ctx.author.avatar_url)
        quinta_feira.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        quinta_feira.add_field(name="AEDs I", value="🏠 Sala: 207\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Luiz Eduardo da Silva")
        quinta_feira.add_field(name="Intro. à Ciência a Computação", value="🏠 Sala: 204\n⌚ Horário: 19:00 - 21:00\n📗 Responsável: Ricardo Menezes Salgado")

        sexta_feira = discord.Embed(title="Horário Acadêmico", description="Sexta-feira", colour = discord.Colour.orange())
        sexta_feira.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\n⇒ O horário foi retirado do Sistema Acadêmico da UNIFAL", icon_url=ctx.author.avatar_url)
        sexta_feira.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        sexta_feira.add_field(name="Fundamentos Mat. p/ Cien. da Comp.", value="🏠 Sala: 201\n⌚ Horário: 14:00 - 16:00\n📗 Responsável: Nelson José Freitas da Silveira")
        sexta_feira.add_field(name="Intro. à Ciência da Computação", value="🏠 Sala: 207\n⌚ Horário: 16:00 - 18:00\n📗 Responsável: Adriana Aparecida de Avila")

        mensagem = await ctx.reply(embed=segunda_feira)
        await mensagem.add_reaction("◀️")
        await mensagem.add_reaction("▶️")

        def check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in valid_reaction

        while True:
            try:
                reaction, user = await bot.wait_for('reaction_add', check = check, timeout = 20)
                if str(reaction.emoji) == "▶️" and pages != 5:
                    await mensagem.remove_reaction(reaction, user)
                    pages += 1
                elif str(reaction.emoji) == "◀️" and pages != 1:
                    await mensagem.remove_reaction(reaction, user)
                    pages -= 1

                elif str(reaction.emoji) == "▶️"and pages == 5:
                    await mensagem.remove_reaction(reaction, user)
                
                elif str(reaction.emoji) == "◀️" and pages == 1:
                    await mensagem.remove_reaction(reaction, user)

                if pages == 1:
                    await mensagem.edit(embed=segunda_feira)
                elif pages == 2:
                    await mensagem.edit(embed=terca_feira)
                elif pages == 3:
                    await mensagem.edit(embed=quarta_feira)
                elif pages == 4:
                    await mensagem.edit(embed=quinta_feira)
                elif pages == 5:
                    await mensagem.edit(embed=sexta_feira)
            except Exception:
                await mensagem.delete()
                await ctx.message.delete()
                break

@bot.command(name="db_fetchall")
async def db_fetchall(ctx, *, msg: str):

    if ctx.author.id != 963258412676825150:
        await ctx.reply("nope")
        return

    cursor.execute(msg)
    content = cursor.fetchall()
    await ctx.reply(content)

@bot.command(name="db_execute")
async def db_execute(ctx, *, msg: str):
    if ctx.author.id != 963258412676825150:
        await ctx.reply("nope")
        return
    

    cursor.execute(msg)
    banco.commit()

@bot.command(name="db_criar")
async def db_criar(ctx):

    if ctx.author.id != 963258412676825150:
        await ctx.reply("nope")
        return

    cursor.execute("CREATE TABLE provas (id text, materia text, conteudo text, dia text, author text, post_date text)")
    banco.commit()
    cursor.execute("CREATE TABLE linksuteis (id text, linguagem text, link text, motivo text, dia text, author text)")
    banco.commit()

@bot.command(name="add_prova", aliases=["addprova","provadd","provaadd","prova-add","prova_add"])
async def add_prova(ctx):


    embedVar = discord.Embed(title="Adicionar uma prova", description="Por favor, selecione a matéria da prova que você deseja adicionar.",colour = discord.Colour.red())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    message = await ctx.reply(embed=embedVar, components = [Select(placeholder="Matérias",options = [
        SelectOption(label="Intro. p/ Ciên. da Comp.", value = "1", emoji = '📗'),
        SelectOption(label="AEDs I", value = "2", emoji = '📗'),
        SelectOption(label="Mat. p/ Ciên. da Comp.", value = "3", emoji = '📗'),
        SelectOption(label="Geometria Analítica", value = "4", emoji = '📗'),
        SelectOption(label="Lógica Digital", value = "5", emoji = '📗')
    ],
    custom_id = "prova"
    )])

    try:
        interaction = await bot.wait_for('select_option', check=lambda i: i.author == ctx.author and i.custom_id == "prova", timeout = 30)
    except Exception:
        embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        warning = await ctx.reply(embed=embedVar, components = [])
        await ctx.message.delete()
        await message.delete()
        time.sleep(15)
        await warning.delete()
        
    await message.delete()
    if interaction.values[0] == "1":
        materia = "Intro. p/ Ciên. da Comp."
    elif interaction.values[0] == "2":
        materia = "AEDs I"
    elif interaction.values[0] == "3":
        materia = "Mat. p/ Ciên. da Comp."
    elif interaction.values[0] == "4":
        materia = "Geometria Analítica"
    elif interaction.values[0] == "5":
        materia = "Lógica Digital"
    
    embedVar = discord.Embed(title=f"Adicionar uma prova de **{materia}**", description="Faça uma descrição do que vai cair na prova.", colour = discord.Colour.red())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPara cancelar o comando, digite QUIT", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    
    message = await ctx.reply(embed=embedVar)

    try:
        conteudo = await bot.wait_for('message', check = lambda msg: msg.author == ctx.author, timeout=120)
    except Exception:
        embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        warning = await ctx.reply(embed=embedVar, components = [])
        await ctx.message.delete()
        await message.delete()
        time.sleep(15)
        await warning.delete()
    
    await message.delete()
    await conteudo.delete()
    
    if (conteudo.content == "QUIT") or (conteudo.content == "Quit") or (conteudo.content == "quit"):
        embedVar = discord.Embed(title="Comando cancelado com sucesso!", colour=discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPara cancelar o comando, digite QUIT", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        message = await conteudo.reply(embed=embedVar)
        time.sleep(10)
        await message.delete()
        await conteudo.delete()
        await ctx.message.delete()
        return 0
    
    if (conteudo.content in ['"','"']):
        embedVar = discord.Embed(title="Conteúdo recusado", description="O banco de dados não suporta aspas simples ou aspas duplas em seu conteúdo.",colour=discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.delete()
        await conteudo.delete()
        await ctx.message.delete
        return 0

    embedVar = discord.Embed(title="Em qual dia será a avaliação?", description="Qual é a data da prova?\n(Utilize o formato dd/mm/YY. Por exemplo: 02/09/22)", colour = discord.Colour.red())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    message = await ctx.reply(embed=embedVar)

    try:
        data = await bot.wait_for('message', check = lambda msg: msg.author == ctx.author, timeout = 30)
    except Exception:
        embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        warning = await ctx.reply(embed=embedVar, components = [])
        await ctx.message.delete()
        await message.delete()
        time.sleep(15)
        await warning.delete()
    
    await message.delete()
    await data.delete()
    
    if (data.content == "QUIT") or (data.content == "Quit") or (data.content == "quit"):
        embedVar = discord.Embed(title="Comando cancelado com sucesso!", colour=discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPara cancelar o comando, digite QUIT", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        message = await data.reply(embed=embedVar)
        time.sleep(10)
        await message.delete()
        await data.delete()
        await ctx.message.delete()
        return 0

    try:
        dataContent = str(data.content)
        dataContent = dataContent.split('/')
        if len(dataContent) != 3:
            embedVar = discord.Embed(title="Data invalida!", description="Por favor, use o formato dd/mm/YY. Por exemplo: 02/09/22\nNote que é importante usar o número 0 antes de dos números e que é necessário especificar o dia, mês e o ano.", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            message = await data.reply(embed=embedVar)
            return 0
        for data_ in dataContent:
            if len(data_) != 2:
                embedVar = discord.Embed(title="Data invalida!", description="Por favor, use o formato dd/mm/YY. Por exemplo: 02/09/22\nNote que é importante usar o número 0 antes de dos números e que é necessário especificar o dia, mês e o ano.", colour = discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                message = await data.reply(embed=embedVar)
                return 0

    except Exception:
        embedVar = discord.Embed(title="Data invalida!", description="Por favor, use o formato dd/mm/YY. Por exemplo: 02/09/22\nNote que é importante usar o número 0 antes de dos números e que é necessário especificar o dia, mês e o ano.", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        message = await data.reply(embed=embedVar)
        return 0

    dataContent = data.content
    dataContent = dataContent.split('/')
    dia = dataContent[0]
    mes = dataContent[1]
    ano = int("20{}".format((str(dataContent[2]))))

    dayOfTheYear = date(int(ano), int(mes), int(dia)).timetuple().tm_yday
    dayOfTheYear_ = datetime.now().timetuple().tm_yday

    if dayOfTheYear - dayOfTheYear_ < 0:
        embedVar = discord.Embed(title="Data inválida!", description = "Essa data é inválida. Por favor, tente novamente.", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await data.reply(embed=embedVar)
        return 0
    
    cursor.execute(f"SELECT * FROM provas WHERE dia = {dayOfTheYear}")
    provas = cursor.fetchall()

    qtdProvas = len(provas)
    if len(provas) != 0:
        n = 1   
        listaDeProvas = ""
        for prova in provas:
            materia__ = prova[1] 

            listaDeProvas = f"{listaDeProvas}{n}. {materia__}\n"

        embedVar = discord.Embed(title="Aviso!", description=f"Já existe(m) {qtdProvas} prova(s). Antes de prosseguir, cheque na lista abaixo se a sua prova já está marcada:\n{listaDeProvas}")
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        
        confirmation = await ctx.send(embed = embedVar, components=[[Button(label="Adicionar nova prova", style=ButtonStyle.green, custom_id="adicionar"),
                                                                    Button(label="Cancelar comando", style=ButtonStyle.red, custom_id="cancelado")]])
        
        confirmationInteraction = await bot.wait_for("button_click", check = lambda i: i.author == ctx.author, timeout=30)

        if confirmationInteraction.custom_id == "cancelado":
            embedVar = discord.Embed(title="Comando cancelado com sucesso!", colour=discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await confirmation.edit(embed=embedVar, components = [])
            return 0
        
        else:
            await confirmation.delete()

    embedVar = discord.Embed(title="Ficha da prova", description="Por favor, cheque todos os campos antes de confirmar", colour = discord.Colour.red())
    embedVar.add_field(name="Matéria", value=materia)
    embedVar.add_field(name="Conteúdo", value=conteudo.content)
    embedVar.add_field(name="Data da prova", value=data.content)
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    request = await ctx.send(embed=embedVar, components = [[Button(label="Confirmar", style=ButtonStyle.green, custom_id = "confirmado"),
                                                Button(label="Cancelar", style=ButtonStyle.red, custom_id="cancelado")]])
    
    try:
        response = await bot.wait_for('button_click', check = lambda i: i.author == ctx.author, timeout=30)
    except Exception:
        embedVar = discord.Embed(title="Ficha da prova", description="Devido a demora para confirmar, o comando foi automaticamente desativado e cancelado.\nCaso queira adicionar outra prova, use o comando novamente.", colour = discord.Colour.red())
        embedVar.add_field(name="Matéria", value=materia)
        embedVar.add_field(name="Conteúdo", value=conteudo.content)
        embedVar.add_field(name="Data da prova", value=data.content)
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await request.edit(embed=embedVar, components = [[Button(label="Confirmar", style=ButtonStyle.green, custom_id = "confirmado", disabled=True),
                                                Button(label="Cancelar", style=ButtonStyle.red, custom_id="cancelado", disabled=True)]])
        
    if response.custom_id == "cancelado":
        embedVar = discord.Embed(title="Ficha da prova", description="O comando foi cancelado com sucesso!", colour = discord.Colour.red())
        embedVar.add_field(name="Matéria", value=materia)
        embedVar.add_field(name="Conteúdo", value=conteudo.content)
        embedVar.add_field(name="Data da prova", value=data.content)
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await request.edit(embed=embedVar, components = [[Button(label="Confirmar", style=ButtonStyle.green, custom_id = "confirmado", disabled=True),
                                                Button(label="Cancelar", style=ButtonStyle.red, custom_id="cancelado", disabled=True)]])
        return 0
    
    elif response.custom_id == "confirmado":
        cursor.execute("SELECT * FROM provas")
        try:
            ids = cursor.fetchall()
            last_id = ids[-1]
            last_id = last_id[0]
            id = int(last_id) + 1
        except Exception:
            id = '0'
        string = "INSERT INTO provas VALUES('{}', '{}', '{}', '{}', '{}', '{}')".format(id, materia, conteudo.content, dayOfTheYear, ctx.author.id, date.today().strftime('%d/%m/%Y'))
        cursor.execute("{}".format(string))
        banco.commit()

        await request.delete()

        embedVar = discord.Embed(title="Adicionado com sucesso!", colour=discord.Colour.green())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await ctx.reply(embed=embedVar)
            
@bot.command(name="provas", aliases=["provalist","provaslist","prova_list","provas_list","prova-list","provas-list"])
async def provas(ctx):
    dayNow = int(datetime.now().timetuple().tm_yday)
    cursor.execute("SELECT * FROM provas ORDER BY dia")
    dados = cursor.fetchall()

    string = ""
    n = 1

    for prova in dados:
        dayOfYear = prova[3]
        materia = prova[1]
        string = string + f"{n}. {materia}. \nDias até a prova: {int(dayOfYear) - dayNow}\n\n"
        n += 1
    
    embedVar = discord.Embed(title="Provas marcadas", description=f"Uma lista de provas marcadas pela sala\n{string}", colour = discord.Colour.red())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPara mais informações, use bugs provainfo [Número]", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    await ctx.reply(embed=embedVar)

@bot.command(name="provainfo", aliases=["prova_info","prova-info"])
async def provainfo(ctx, n: int):
    dayNow = int(datetime.now().timetuple().tm_yday)
    cursor.execute("SELECT * FROM provas ORDER BY dia")
    dados = cursor.fetchall()

    prova = dados[n - 1]
    materia, conteudo, dia, author, post_date = prova[1], prova[2], prova[3], prova[4], prova[5]
    user = await bot.fetch_user(int(author))

    embedVar = discord.Embed(title=f"Prova de **{materia}**", colour = discord.Colour.red())
    embedVar.add_field(name="📗 Conteúdo", value=conteudo, inline=False)
    embedVar.add_field(name="📅 Dias até a prova", value=str(int(dia) - int(dayNow)), inline=False)
    embedVar.add_field(name="📝 Editado por último por:", value=f"{user.name}#{user.discriminator}, no dia {post_date}", inline=False)
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    await ctx.reply(embed=embedVar)

@bot.command(name="prova_edit", aliases=["prova-edit","provaedit","edit-prova","edit_prova","editar_prova","editar-prova","prova_editar","prova-editar","provaeditar", "editarprova"])
async def prova_edit(ctx, n: int):
    dayNow = int(datetime.now().timetuple().tm_yday)
    cursor.execute("SELECT * FROM provas ORDER BY dia")
    dados = cursor.fetchall()

    prova = dados[n - 1]
    id, materia, conteudo, dia, author, post_date = prova[0], prova[1], prova[2], prova[3], prova[4], prova[5]
    user = await bot.fetch_user(int(author))

    embedVar = discord.Embed(title=f"Editar prova de **{materia}**", description="O que você deseja fazer?", colour = discord.Colour.red())
    embedVar.add_field(name="📗 Conteúdo", value=conteudo, inline=False)
    embedVar.add_field(name="📅 Dias até a prova", value=str(int(dia) - int(dayNow)), inline=False)
    embedVar.add_field(name="📝 Editado por último por:", value=f"{user.name}#{user.discriminator}, no dia {post_date}", inline=False)
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    message = await ctx.reply(embed=embedVar, components = [[Button(label="Editar", style=ButtonStyle.blue, custom_id="edit"),
                                                Button(label="Deletar", style=ButtonStyle.red, custom_id = "deletar")]])

    try:
        request = await bot.wait_for('button_click', check = lambda i: i.author == ctx.author, timeout=20)
    except Exception:
        embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components = [])
        await ctx.message.delete()
        return 0
    
    securityCode = random.randint(1000,9999)
    if request.custom_id == "deletar":
        embedVar = discord.Embed(title="Confirmação", description=f"Por favor, digite o código **{securityCode}** para confirmar. Caso contrário, só ignore.", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components = [])

        try:
            securityCodeAwnser = await bot.wait_for('message', check = lambda msg: msg.author == ctx.author, timeout=20)
        except Exception:
            embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await message.edit(embed=embedVar, components = [])
            await ctx.message.delete()
            return 0
        
        if str(securityCodeAwnser.content) != str(securityCode):
            embedVar = discord.Embed(title="Código inváido. Tente novamente!", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await message.edit(embed=embedVar)
            await ctx.message.delete()
            await securityCodeAwnser.delete()
            return 0
        
        await securityCodeAwnser.delete()

        cursor.execute(f"DELETE FROM provas WHERE id = '{id}'")
        banco.commit()

        embedVar = discord.Embed(title="Deletado com sucesso!", description="A prova foi deletada com sucesso da nossa lista de provas!", colour = discord.Colour.green())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components = [])
        await ctx.message.delete()
        return 0

    elif request.custom_id == "edit":
        embedVar = discord.Embed(title="Edição de provas", description="Selecione o que você deseja editar", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components=[[Button(label="Matéria", style=ButtonStyle.green, custom_id="materia"),
                                                        Button(label="Conteúdo", style=ButtonStyle.green, custom_id="conteudo"),
                                                        Button(label="Data", style=ButtonStyle.green, custom_id="date"),
                                                        Button(label="Cancelar", style=ButtonStyle.red, custom_id="cancel")]])
        try:
            request = await bot.wait_for('button_click', check = lambda i: i.author == ctx.author, timeout = 20)
        except Exception:
            embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await message.edit(embed=embedVar, components = [])
        
        if request.custom_id == "materia":
            embedVar = discord.Embed(title="Editando a matéria", description="Por favor, selecione a nova matéria que você deseja atribuir a essa prova.", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await message.edit(embed=embedVar, components = [Select(
                placeholder="Matérias", options=[
                    SelectOption(label="Intro. p/ Ciên. da Comp.", value = "1", emoji = '📗'),
                    SelectOption(label="AEDs I", value = "2", emoji = '📗'),
                    SelectOption(label="Mat. p/ Ciên. da Comp.", value = "3", emoji = '📗'),
                    SelectOption(label="Geometria Analítica", value = "4", emoji = '📗'),
                    SelectOption(label="Lógica Digital", value = "5", emoji = '📗')
                ],
                custom_id = "prova"
                )])
            
            try:
                request = await bot.wait_for('select_option', check = lambda i: i.author == ctx.author, timeout = 30)
            except Exception:
                embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                await message.edit(embed=embedVar, components = [])
                return 0
            
            if request.values[0] == "1":
                materia = "Intro. p/ Ciên. da Comp."
            elif request.values[0] == "2":
                materia = "AEDs I"
            elif request.values[0] == "3":
                materia = "Mat. p/ Ciên. da Comp."
            elif request.values[0] == "4":
                materia = "Geometria Analítica"
            elif request.values[0] == "5":
                materia = "Lógica Digital"

            cursor.execute(f"UPDATE provas SET materia = '{materia}' WHERE id = '{id}'")
            banco.commit()
            cursor.execute(f"UPDATE provas SET author = '{ctx.author.id}' WHERE id = '{id}'")
            banco.commit()
            cursor.execute("UPDATE provas SET post_date = '{}' WHERE id = '{}'".format((date.today().strftime("%d/%m/%Y")), id))
            banco.commit()
            
            embedVar = discord.Embed(title="Alterado com sucesso!", colour = discord.Colour.green())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await message.edit(embed=embedVar, components = [])
            await ctx.message.delete()
            return 0
        
        elif request.custom_id == "conteudo":
            embedVar = discord.Embed(title="Modificando o conteúdo", description="Escreva abaixo o novo conteúdo que você deseja colocar na prova", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPara cancelar o comando, digite QUIT", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await message.edit(embed=embedVar, components = [])

            try:
                request = await bot.wait_for('message', check = lambda msg: msg.author == ctx.author, timeout=30)
            except Exception:
                embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                await message.edit(embed=embedVar, components = [])
                return 0
            
            if request.content in ["Quit", "QUIT", "quit"]:
                embedVar = discord.Embed(title="Comando cancelado com sucesso!", colour=discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                await message.edit(embed=embedVar, components = [])
                await ctx.message.delete
                return 0
            
            if request.content in ['"','"']:
                embedVar = discord.Embed(title="Conteúdo recusado", description="O banco de dados não suporta aspas simples ou aspas duplas em seu conteúdo.",colour=discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                await message.edit(embed=embedVar, components = [])
                await ctx.message.delete
                return 0

            
            cursor.execute(f"UPDATE provas SET conteudo = '{request.content}' WHERE id = '{id}'")
            banco.commit()
            cursor.execute(f"UPDATE provas SET author = '{ctx.author.id}' WHERE id = '{id}'")
            banco.commit()
            cursor.execute("UPDATE provas SET post_date = '{}' WHERE id = '{}'".format((date.today().strftime("%d/%m/%Y")), id))
            banco.commit()

            embedVar = discord.Embed(title="Conteúdo alterado com sucesso!", description=f'O conteúdo foi alterado para:\n"{request.content}"', colour = discord.Colour.green())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await message.edit(embed=embedVar)
            await request.delete()
        
        elif request.custom_id == "date":
            embedVar = discord.Embed(title="Modificando a data", description="Escreva abaixo a nova data que você deseja colocar na prova\nLembre-se de usar o formato dd/mm/yy. Por exemplo: 02/09/22", colour = discord.Colour.red())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPara cancelar o comando, digite QUIT", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await message.edit(embed=embedVar, components = [])

            try:
                data = await bot.wait_for("message", check = lambda msg: msg.author == ctx.author, timeout=30)
            except Exception:
                embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                await message.edit(embed=embedVar, components = [])
                return 0
            
            if data.content in ["Quit", "QUIT", "quit"]:
                embedVar = discord.Embed(title="Comando cancelado com sucesso!", colour=discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                await message.edit(embed=embedVar, components = [])
                await data.delete()
                await ctx.message.delete()
                return 0

            try:
                dataContent = str(data.content)
                dataContent = dataContent.split('/')
                if len(dataContent) != 3:
                    embedVar = discord.Embed(title="Data invalida!", description="Por favor, use o formato dd/mm/YY. Por exemplo: 02/09/22\nNote que é importante usar o número 0 antes de dos números e que é necessário especificar o dia, mês e o ano.", colour = discord.Colour.red())
                    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                    message = await message.edit(embed=embedVar, components = [])
                    await data.delete()
                    await ctx.message.delete()
                    return 0
                for data_ in dataContent:
                    if len(data_) != 2:
                        embedVar = discord.Embed(title="Data invalida!", description="Por favor, use o formato dd/mm/YY. Por exemplo: 02/09/22\nNote que é importante usar o número 0 antes de dos números e que é necessário especificar o dia, mês e o ano.", colour = discord.Colour.red())
                        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                        message = await message.edit(embed=embedVar, components = [])
                        await data.delete()
                        await ctx.message.delete()
                        return 0

            except Exception:
                embedVar = discord.Embed(title="Data invalida!", description="Por favor, use o formato dd/mm/YY. Por exemplo: 02/09/22\nNote que é importante usar o número 0 antes de dos números e que é necessário especificar o dia, mês e o ano.", colour = discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                message = await message.edit(embed=embedVar, components = [])
                await data.delete()
                await ctx.message.delete()
                return 0
            
            dataContent = data.content
            dataContent = dataContent.split('/')
            dia = dataContent[0]
            mes = dataContent[1]
            ano = int("20{}".format((str(dataContent[2]))))

            dayOfTheYear = date(int(ano), int(mes), int(dia)).timetuple().tm_yday
            dayOfTheYear_ = datetime.now().timetuple().tm_yday

            if int(dayOfTheYear) - int(dayOfTheYear_) < 0:
                embedVar = discord.Embed(title="Data inválida!", description = "Essa data é inválida. Por favor, tente novamente.", colour = discord.Colour.red())
                embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
                embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
                await data.reply(embed=embedVar, components = [])
                return 0
            
            cursor.execute(f"UPDATE provas SET dia = '{dayOfTheYear}' WHERE id = '{id}'")
            banco.commit()
            cursor.execute(f"UPDATE provas SET author = '{ctx.author.id}' WHERE id = '{id}'")
            banco.commit()
            cursor.execute("UPDATE provas SET post_date = '{}' WHERE id = '{}'".format((date.today().strftime("%d/%m/%Y")), id))
            banco.commit()

            embedVar = discord.Embed(title="Data alterada com sucesso!", description=f'A data foi alterada com sucesso!', colour = discord.Colour.green())
            embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
            embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
            await message.edit(embed=embedVar, components = [])
            await data.delete()
            await ctx.message.delete()
            return 0           

@bot.command(name="links", aliases=["Link","link", "Links"])
async def links(ctx):
    c = await ctx.guild.fetch_emoji(981957503988428840)
    cpp = await ctx.guild.fetch_emoji(981957503854198835)
    cs = await ctx.guild.fetch_emoji(981957503686414416)
    java = await ctx.guild.fetch_emoji(981957504110043136)
    javascript = await ctx.guild.fetch_emoji(981957503875154040)
    python = await ctx.guild.fetch_emoji(981957504714014740)
    html = await ctx.guild.fetch_emoji(981957503661248552)
    css = await ctx.guild.fetch_emoji(981957503791276123)
    php = await ctx.guild.fetch_emoji(981957503963250719)
    sql = await ctx.guild.fetch_emoji(981957503690629151)

    embedVar = discord.Embed(title="Selecione a linguagem", description="Selecione o assunto que você quer obter links úteis e interessantes", colour = discord.Colour.red())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    message = await ctx.reply(embed=embedVar, components=[Select(placeholder="Linguagem de programação", options=[
        SelectOption(label="C", value="C", emoji=c),
        SelectOption(label="C++", value="C++", emoji = cpp),
        SelectOption(label="C#", value="C#", emoji=cs),
        SelectOption(label="Java", value="Java", emoji=java),
        SelectOption(label="Python", value="Python", emoji = python),
        SelectOption(label="HTML", value="HTML", emoji = html),
        SelectOption(label="CSS", value="CSS", emoji = css),
        SelectOption(label="JavaScript", value="Javascript", emoji=javascript),
        SelectOption(label="PHP", value="PHP", emoji = php),
        SelectOption(label="SQL", value="SQL", emoji = sql)
    ], custom_id="assunto")])

    try:
        response = await bot.wait_for('select_option', check= lambda i: (i.author == ctx.author) and (i.custom_id == "assunto"), timeout=30)
    except Exception:
        embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components = [])
        return

    cursor.execute(f"SELECT * FROM linksuteis WHERE linguagem = '{response.values[0]}'")
    dados = cursor.fetchall()
    print(dados)
    
    string = ""

    try:
        print('1')
        for dado in dados:

            id, link, motivo = dado[0], dado[2], dado[3]
            string = f"{string} {id}. {motivo} \n ({link})\n\n"
    except Exception:
        pass


    embedVar = discord.Embed(title=f"Links úteis para {response.values[0]}", description=f"Segue abaixo uma lista com links potencialmente úteis para você!\n\n{string}", colour = discord.Colour.red())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    await message.edit(embed=embedVar, components = [])

@bot.command(name="links_add", aliases=["links-add","link-add","linkadd", "linksadd"])
async def links_adicionar(ctx):
    c = await ctx.guild.fetch_emoji(981957503988428840)
    cpp = await ctx.guild.fetch_emoji(981957503854198835)
    cs = await ctx.guild.fetch_emoji(981957503686414416)
    java = await ctx.guild.fetch_emoji(981957504110043136)
    javascript = await ctx.guild.fetch_emoji(981957503875154040)
    python = await ctx.guild.fetch_emoji(981957504714014740)
    html = await ctx.guild.fetch_emoji(981957503661248552)
    css = await ctx.guild.fetch_emoji(981957503791276123)
    php = await ctx.guild.fetch_emoji(981957503963250719)
    sql = await ctx.guild.fetch_emoji(981957503690629151)

    embedVar = discord.Embed(title="Selecione a linguagem", description="Qual linguagem você deseja adicionar uma informação?", colour = discord.Colour.red())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    message = await ctx.reply(embed=embedVar, components=[Select(placeholder="Linguagem de programação", options=[
        SelectOption(label="C", value="C", emoji=c),
        SelectOption(label="C++", value="C++", emoji = cpp),
        SelectOption(label="C#", value="C#", emoji=cs),
        SelectOption(label="Java", value="Java", emoji=java),
        SelectOption(label="Python", value="Python", emoji = python),
        SelectOption(label="HTML", value="HTML", emoji = html),
        SelectOption(label="CSS", value="CSS", emoji = css),
        SelectOption(label="JavaScript", value="JavaScript", emoji=javascript),
        SelectOption(label="PHP", value="PHP", emoji = php),
        SelectOption(label="SQL", value="SQL", emoji = sql)
    ], custom_id="assunto")])

    try:
        linguagem = await bot.wait_for('select_option', check= lambda i: (i.author == ctx.author) and (i.custom_id == "assunto"), timeout=30)
    except Exception:
        embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components = [])
        return

    embedVar = discord.Embed(title="Digite o link/Fonte de suas informações", colour = discord.Colour.red())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPara cancelar o comando, digite QUIT", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    await message.edit(embed=embedVar, components = [])

    try:
        fonte = await bot.wait_for('message', check = lambda msg: msg.author == ctx.author, timeout = 30)
    except Exception:
        embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components = [])
        return
    
    if fonte.content in ['QUIT', 'Quit', 'quit']:
        embedVar = discord.Embed(title="Comando cancelado!", description="O comando foi cancelado com sucesso!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components = [])
        return
    
    embedVar = discord.Embed(title="Adicionar uma descrição", description="Faça uma breve descrição do conteúdo que você acha interessante adicionar", colour = discord.Colour.red())
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPara cancelar o comando, digite QUIT", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    await ctx.send(embed=embedVar, components = [])

    try:
        description = await bot.wait_for('message', check = lambda msg: msg.author == ctx.author, timeout=30)
    except Exception:
        embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components = [])
        return
    
    if description.content in ['QUIT', 'Quit', 'quit']:
        embedVar = discord.Embed(title="Comando cancelado!", description="O comando foi cancelado com sucesso!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await message.edit(embed=embedVar, components = [])
        return
    
    embedVar = discord.Embed(title="Informações", descriptions="Por favor, cheque se essas informações estão corretas", colour = discord.Colour.red())
    embedVar.add_field(name="Linguagem", value=f"{linguagem.values[0]}", inline=False)
    embedVar.add_field(name="Fonte", value=f"{fonte.content}", inline=False)
    embedVar.add_field(name="Descrição", value=f"{description.content}", inline=False)
    embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
    embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    confirmation = await ctx.send(embed=embedVar, components = [[Button(label="Confirmar", style=ButtonStyle.green, custom_id="confirmado"),
                                                    Button(label="Cancelar", style=ButtonStyle.red, custom_id="cancelado")]])

    try:
        interaction = await bot.wait_for("button_click", check = lambda i: i.author == ctx.author, timeout = 30)
    except Exception:
        embedVar = discord.Embed(title="Tempo esgotado!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await confirmation.edit(embed=embedVar, components = [])
        return
    
    if interaction.custom_id == "cancelado":
        embedVar = discord.Embed(title="Comando cancelado!", description="O comando foi cancelado com sucesso!", colour = discord.Colour.red())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await confirmation.edit(embed=embedVar, components = [])
        return
    elif interaction.custom_id == "confirmado":
        try:
            cursor.execute("SELECT * FROM linksuteis")
            infos = cursor.fetchall()
            last_info = infos[-1]
            last_id = last_info[0]
        except Exception:
            last_id = 0
        
        id = int(last_id) + 1

        cursor.execute(f"INSERT INTO linksuteis VALUES('{id}','{linguagem.values[0]}','{fonte.content}','{description.content}','{date.today().strftime('%d/%m/%Y')}','{ctx.author.id}')")
        banco.commit()

        embedVar = discord.Embed(title="Adicionado com sucesso!", description="Os valores foram adicionados com sucesso no banco de dados!", colour = discord.Colour.green())
        embedVar.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}", icon_url=ctx.author.avatar_url)
        embedVar.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
        await confirmation.edit(embed=embedVar, components = [])
        return

@bot.command(name="ajuda", aliases=["comandos","commands"])
async def ajuda(ctx):
    maxPages = "02"
    pages = 1
    pagina2 = discord.Embed(title="Comandos musicais", description="Uma lista de comandos e suas funções!\nPrefixo para todos os comandos: `bugs`", colour = discord.Colour.red())
    pagina2.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPágina 02/{maxPages}", icon_url=ctx.author.avatar_url)
    pagina2.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    pagina2.add_field(name="`play [URL/Nome da música]`", value="Conecta no seu canal de voz e começa a tocar a música em questão", inline=True)
    pagina2.add_field(name="`skip`", value="Pula a música que está tocando atualmente.", inline=True)
    pagina2.add_field(name="`fila`", value="Mostra a fila de músicas a serem tocadas", inline=True)
    pagina2.add_field(name="`remover [index]`", value="Remove uma música da lista, sendo Index o seu número na fila", inline = True)
    pagina2.add_field(name="`loop`", value="Ativa/Desativa a função de loop da música atualmente tocando", inline=True)
    pagina2.add_field(name="`embaralhar`", value="Embaralha todas as músicas da fila", inline=True)
    pagina2.add_field(name="`agora`", value="Mostra a música que está tocando atualmente", inline=True)
    pagina2.add_field(name="`pausar`", value="Pausa a música que está tocando atualmente", inline=True)
    pagina2.add_field(name="`continuar`", value="Continua a música anteriormente pausada", inline=True)

    pagina1 = discord.Embed(title="Comandos utilitários", description="Uma lista de comandos e suas funções!\nPrefixo para todos os comandos: `bugs`", colour = discord.Colour.red())
    pagina1.set_footer(text=f"{ctx.author.name}#{ctx.author.discriminator} | {date.today().strftime('%d/%m/%Y')}\nPágina 01/{maxPages}", icon_url=ctx.author.avatar_url)
    pagina1.set_author(name="BUGS - UNIFAL 2022", icon_url="https://cdn.discordapp.com/icons/977360301366337566/18177c0c099cfa608607b36083c8e291.webp?size=96")
    pagina1.add_field(name="`provas`", value="Mostra uma lista de provas que foram marcadas")
    pagina1.add_field(name="`prova_add`", value="Adiciona uma nova prova no registro")
    pagina1.add_field(name="`prova_mod [index]`", value="Modifica uma prova da lista, sendo [index] a sua posição na lista")
    pagina1.add_field(name="`prova_info [index]`", value="Mostra mais inforamações de uma prova marcada, sendo [index] sua posição na lista")
    pagina1.add_field(name="`horario`", value="Mostra o horário de hoje/semanal, junto com a sala e o professor.")
    pagina1.add_field(name="`links`", value="Mostra uma lista de links úteis para determianda linguagem de programação")
    pagina1.add_field(name="`links_add`", value="Adiciona um novo link à lista")

    mensagem = await ctx.reply(embed=pagina1)
    await mensagem.add_reaction('◀️')
    await mensagem.add_reaction('▶️')
    valid_reaction = ['◀️','▶️']

    def check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in valid_reaction

    while True:
        try:
            reaction, user = await bot.wait_for('reaction_add', check = check, timeout = 30)
            if str(reaction.emoji) == "▶️" and pages != 2:
                await mensagem.remove_reaction(reaction, user)
                pages += 1
            elif str(reaction.emoji) == "◀️" and pages != 1:
                await mensagem.remove_reaction(reaction, user)
                pages -= 1
            elif str(reaction.emoji) == "▶️"and pages == 2:
                await mensagem.remove_reaction(reaction, user)
            
            elif str(reaction.emoji) == "◀️" and pages == 1:
                await mensagem.remove_reaction(reaction, user)
            if pages == 1:
                await mensagem.edit(embed=pagina1)
            elif pages == 2:
                await mensagem.edit(embed=pagina2)
        except Exception:
            await mensagem.delete()
            await ctx.message.delete()
            break



@tasks.loop(hours=12)
async def db_check():
    print('foi')
    cursor.execute("SELECT * FROM provas")
    dados = cursor.fetchall()

    today = datetime.now().timetuple().tm_yday

    ids = []

    for dado in dados:
        data = dado[3]
        id = dado[0]

        if int(int(data) - int(today)) < 0:
            ids.append(id)

    if len(ids) != 0:
        for id in ids:
            cursor.execute(f"DELETE FROM provas WHERE id = {id}")
            banco.commit()

@bot.event
async def on_ready():
    print("Bot online")
    db_check.start()

bot.run('OTc5NTQwNzcyOTEzMzExODQ1.Gj0j2v.7QSqSWT65RXRQTiuNflUEUT8OpZdF5t0yKMeZE')

#É necessário colocar um task.loop(hour=24) para verificar se as provas já passaram.
#Caso contrário, ficará gastando memoria no banco de dados além de prejudicar o desempenho
#E a dinâmica do bot. Comandos como o "provasinfo" deixarão de funcionar dinâmicamente.