import os
import math
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING

load_dotenv()

# ================= НАСТРОЙКИ БАЗЫ ДАННЫХ MONGODB И БОТА =================

MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise ValueError("Ошибка: MONGO_URL не найден в файле .env или Variables Railway")

mongo_client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)

try:
    mongo_client.admin.command('ping')
    print("Успешное подключение к MongoDB Atlas!")
except Exception as e:
    print(f"Ошибка подключения к MongoDB: {e}")

db = mongo_client["discord_tickets_db"]

tickets_col = db["tickets"]
counters_col = db["counters"]
deleted_tickets_col = db["deleted_tickets"]
settings_col = db["settings"]
message_stats_col = db["message_stats"]
bump_stats_col = db["bump_stats"]

tickets_col.create_index("transcript_url", unique=True, sparse=True)
deleted_tickets_col.create_index("transcript_url", unique=True, sparse=True)
message_stats_col.create_index([("channel_id", 1), ("day", 1), ("user_id", 1)])
bump_stats_col.create_index([("channel_id", 1), ("day", 1), ("user_id", 1)])


def get_next_sequence_value(sequence_name: str) -> int:
    seq = counters_col.find_one_and_update(
        {"_id": sequence_name},
        {"$inc": {"sequence_value": 1}},
        upsert=True,
        return_document=True
    )
    return seq["sequence_value"]


# ================= ВЛАДЕЛЕЦ И КОНФИГУРАЦИЯ =================
OWNER_ID = 851443344718430210 

HEADER_IMAGES = {
    "help": "",
    "config": "",
    "leaderboard": "",
    "tickets": "",
    "results": "",
}

EMOJI_DEFAULTS = {
    "success": "<a:gif_verify:1522328481956888686>",
    "warning": "<:zzz:1522341702852022412>",
    "error": "<:bruh:1521904409582375174>",
    "permission": "<:imcrine:1543711667647418381>",
    "info": "ℹ️",
    "delete": "🗑️",
}

# Пункт 9: Таблица логируемых команд через True / False
LOGGABLE_COMMANDS_DEFAULT = {
    "addticket": True,
    "deleteticket": True,
    "deletelog": True,
    "resetlogs": True,
    "ticketlogs": False,
    "ticketstats": False,
    "leaderboard": False,
    "sum": False,
    "help": False,
    "config": True,
}

PERMISSION_GROUPS_DEFAULT = {
    "support": {
        "name": "Поддержка",
        "emoji": "🛟",
        "roles": [1501507449860001853, 1322962344040464424],
        "commands": ["help", "ticketstats", "leaderboard", "sum"],
    },
    "transcript": {
        "name": "Транскрипты",
        "emoji": "🧾",
        "roles": [1542601770461569044, 1323348388762226759],
        "commands": ["addticket", "deleteticket", "ticketlogs"],
    },
    "admin": {
        "name": "Администрация",
        "emoji": "🛡️",
        "roles": [1322962317885046844, 1502684875868737796],
        "commands": ["deletelog", "resetlogs"],
    },
}

COMMAND_USAGE_HELP = {
    "addticket": "`.addticket [ID_модератора] [ссылка] [категория]` — Внести новый обработанный тикет в базу.",
    "deleteticket": "`.deleteticket [ID_лога] [ссылка_на_транскрипт]` — Записать удаление тикета.",
    "ticketlogs": "`.ticketlogs [ID / упоминание]` — Просмотреть логи тикетов пользователя.",
    "ticketstats": "`.ticketstats [ID / упоминание]` — Просмотреть статистику тикетов пользователя.",
    "deletelog": "`.deletelog [ID_лога]` — Удалить конкретный лог тикета.",
    "resetlogs": "`.resetlogs [ID / упоминание]` — Очистить все логи модератора.",
}

CONFIG = {}

def apply_config_globals():
    global EMBED_COLOR, FOOTER_TEXT
    EMBED_COLOR = discord.Color(CONFIG["embed_color"])
    FOOTER_TEXT = CONFIG["footer_text"]

def load_config():
    global CONFIG
    defaults = {
        "_id": "config",
        "embed_color": 0x212121,
        "footer_text": "ТУСОВКА ДОРИСТА",
        "log_channel_id": 1543903998677876836,
        "log_toggles": dict(LOGGABLE_COMMANDS_DEFAULT),
        "counting_channel_id": None,
        "bump_channel_id": None,
        "permission_groups": {k: dict(v) for k, v in PERMISSION_GROUPS_DEFAULT.items()},
        "emojis": dict(EMOJI_DEFAULTS),
    }

    doc = settings_col.find_one({"_id": "config"})
    if doc is None:
        settings_col.insert_one(defaults)
        doc = defaults
    else:
        updated = False
        for key, value in defaults.items():
            if key not in doc:
                doc[key] = value
                updated = True
        if updated:
            settings_col.update_one({"_id": "config"}, {"$set": doc}, upsert=True)

    CONFIG = doc
    apply_config_globals()

def update_config(patch: dict):
    global CONFIG
    settings_col.update_one({"_id": "config"}, {"$set": patch}, upsert=True)
    for key, value in patch.items():
        CONFIG[key] = value
    apply_config_globals()

def get_emoji(name: str) -> str:
    return CONFIG.get("emojis", {}).get(name, EMOJI_DEFAULTS.get(name, ""))

# Пункт 4 & 10: Формирование ошибок в виде эмбедов
def make_error_embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{get_emoji('error')} {title}",
        description=description,
        color=discord.Color.red()
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def make_status_embed(title: str, message: str, kind: str = "info") -> discord.Embed:
    emoji = get_emoji(kind)
    embed = discord.Embed(
        title=title,
        description=f"{emoji} {message}" if emoji else message,
        color=EMBED_COLOR,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed

# Логирование действий
async def log_action(guild: discord.Guild, command_name: str, embed: discord.Embed):
    log_toggles = CONFIG.get("log_toggles", LOGGABLE_COMMANDS_DEFAULT)
    if not log_toggles.get(command_name, False):
        return
    log_channel_id = CONFIG.get("log_channel_id")
    if not log_channel_id:
        return
    channel = guild.get_channel(log_channel_id)
    if channel:
        await channel.send(embed=embed)

load_config()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)
LOGS_PER_PAGE = 3

ALLOWED_CHANNEL_IDS = [1322968592202993746, 1537220150267220018]
VALID_CATEGORIES = ["Помощь по серверу", "Получение призов", "Получение роли", "Покупка рекламы"]

def is_owner_user(user: discord.Member | discord.User) -> bool:
    return user.id == OWNER_ID

def check_access(user: discord.Member | discord.User, channel_id: int, command_name: str) -> tuple[bool, str]:
    if not isinstance(user, discord.Member):
        return False, "Команды работают только на сервере."
    if is_owner_user(user) or user.guild_permissions.administrator:
        return True, ""
    if ALLOWED_CHANNEL_IDS and channel_id not in ALLOWED_CHANNEL_IDS:
        channels_mention = ", ".join(f"<#{cid}>" for cid in ALLOWED_CHANNEL_IDS)
        return False, f"Эта команда доступна только в каналах: {channels_mention}"

    matched_group = False
    for group in CONFIG.get("permission_groups", {}).values():
        role_ids = [int(x) for x in group.get("roles", [])]
        if any(role.id in role_ids for role in user.roles):
            matched_group = True
            if command_name in group.get("commands", []):
                return True, ""
    if not matched_group:
        return False, "У вас недостаточно ролей для использования этой команды."
    return False, "Ваша группа не имеет доступа к этой команде."

def check_access_decorator(command_name: str | None = None):
    async def predicate(ctx):
        cname = command_name or ctx.command.name
        ok, msg = check_access(ctx.author, ctx.channel.id, cname)
        if not ok:
            raise commands.CheckFailure(msg)
        return True
    return commands.check(predicate)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot logged in as {bot.user}")

# Пункт 10: Глобальный обработчик ошибок (каждая ошибка выводится в виде эмбеда)
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    
    if isinstance(error, commands.MissingRequiredArgument):
        cmd_help = COMMAND_USAGE_HELP.get(ctx.command.name, f"`.{ctx.command.name}`")
        embed = make_error_embed(
            "Недостаточно аргументов",
            f"Вы не указали обязательный аргумент **{error.param.name}**!\n\n**Использование:**\n{cmd_help}"
        )
        await ctx.send(embed=embed)
        return

    if isinstance(error, commands.BadArgument):
        cmd_help = COMMAND_USAGE_HELP.get(ctx.command.name, f"`.{ctx.command.name}`")
        embed = make_error_embed(
            "Неверный аргумент",
            f"Один из переданных аргументов указан неверно.\n\n**Использование:**\n{cmd_help}"
        )
        await ctx.send(embed=embed)
        return

    if isinstance(error, commands.CheckFailure):
        embed = make_error_embed("Отказ в доступе", str(error))
        await ctx.send(embed=embed)
        return

    embed = make_error_embed("Ошибка при выполнении", str(error))
    await ctx.send(embed=embed)

# ================= СТАТИСТИКА СООБЩЕНИЙ И BUMP =================

def utc_day(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.date().isoformat()

def record_message_stat(message: discord.Message):
    channel_id = CONFIG.get("counting_channel_id")
    if not channel_id or message.channel.id != channel_id or message.author.bot:
        return
    message_stats_col.update_one(
        {"channel_id": channel_id, "user_id": message.author.id, "day": utc_day()},
        {"$inc": {"count": 1}},
        upsert=True,
    )

def record_bump_stat(interaction: discord.Interaction):
    channel_id = CONFIG.get("bump_channel_id")
    if not channel_id or interaction.channel_id != channel_id or interaction.user.bot:
        return
    if interaction.type != discord.InteractionType.application_command:
        return
    command_name = (interaction.data or {}).get("name")
    if not command_name:
        return
    bump_stats_col.update_one(
        {"channel_id": channel_id, "user_id": interaction.user.id, "day": utc_day()},
        {"$inc": {"count": 1}, "$set": {"last_command": command_name}},
        upsert=True,
    )

# Пункт 2: Считалка и бамп (7 дней - 30 дней - Все время)
def process_results():
    now = datetime.now(timezone.utc)
    d7 = (now - timedelta(days=7)).date().isoformat()
    d30 = (now - timedelta(days=30)).date().isoformat()

    def top_stats(collection, start_day, channel_id):
        if not channel_id:
            return []
        pipeline = []
        if start_day:
            pipeline.append({"$match": {"channel_id": channel_id, "day": {"$gte": start_day}}})
        else:
            pipeline.append({"$match": {"channel_id": channel_id}})
        pipeline.extend([
            {"$group": {"_id": "$user_id", "count": {"$sum": "$count"}}},
            {"$sort": {"count": -1}},
            {"$limit": 10},
        ])
        return [(doc["_id"], doc["count"]) for doc in collection.aggregate(pipeline)]

    def fmt(rows):
        if not rows:
            return "— *Нет данных*"
        return "\n".join(f"`{i}.` <@{uid}> — **{count}**" for i, (uid, count) in enumerate(rows, 1))

    embed = discord.Embed(title=f"{get_emoji('info')} Итоги", color=EMBED_COLOR)
    
    # 7 дней
    embed.add_field(name="💬 Сообщения — 7 дней", value=fmt(top_stats(message_stats_col, d7, CONFIG.get("counting_channel_id"))), inline=True)
    embed.add_field(name="🚀 Bump — 7 дней", value=fmt(top_stats(bump_stats_col, d7, CONFIG.get("bump_channel_id"))), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # 30 дней
    embed.add_field(name="💬 Сообщения — 30 дней", value=fmt(top_stats(message_stats_col, d30, CONFIG.get("counting_channel_id"))), inline=True)
    embed.add_field(name="🚀 Bump — 30 дней", value=fmt(top_stats(bump_stats_col, d30, CONFIG.get("bump_channel_id"))), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Все время
    embed.add_field(name="💬 Сообщения — Все время", value=fmt(top_stats(message_stats_col, None, CONFIG.get("counting_channel_id"))), inline=True)
    embed.add_field(name="🚀 Bump — Все время", value=fmt(top_stats(bump_stats_col, None, CONFIG.get("bump_channel_id"))), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Каналы
    count_channel = CONFIG.get("counting_channel_id")
    bump_channel = CONFIG.get("bump_channel_id")
    embed.add_field(name="Канал для считалки", value=f"<#{count_channel}>" if count_channel else "Не установлен", inline=True)
    embed.add_field(name="Канал для бампа", value=f"<#{bump_channel}>" if bump_channel else "Не установлен", inline=True)

    embed.set_footer(text=FOOTER_TEXT)
    return embed

@bot.event
async def on_message(message: discord.Message):
    record_message_stat(message)
    await bot.process_commands(message)

@bot.event
async def on_interaction(interaction: discord.Interaction):
    record_bump_stat(interaction)

# Пункт 3: Лидерборд с прочерками (1. 2. 3. при отсутствии или недостатке людей)
def process_leaderboard():
    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    def get_top(collection, field: str, min_date=None, exclude_zero=False):
        match_stage = {}
        if min_date:
            match_stage["created_at"] = {"$gte": min_date}
        if exclude_zero:
            match_stage[field] = {"$ne": 0}

        pipeline = []
        if match_stage:
            pipeline.append({"$match": match_stage})

        pipeline.extend([
            {"$group": {"_id": f"${field}", "cnt": {"$sum": 1}}},
            {"$sort": {"cnt": -1}},
            {"$limit": 3}
        ])
        results = list(collection.aggregate(pipeline))
        return [(doc["_id"], doc["cnt"]) for doc in results]

    def format_top_with_dashes(top_list, unit_label="тикетов"):
        lines = []
        for i in range(1, 4):
            if i <= len(top_list):
                u_id, count = top_list[i - 1]
                lines.append(f"`{i}.` <@{u_id}> — **{count}** {unit_label}")
            else:
                lines.append(f"`{i}.` —")
        return "\n".join(lines)

    embed = discord.Embed(title="<:sparkles:1522342290494849034> Лидерборд тикетов и транскриптов", color=EMBED_COLOR)

    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (7 дн.)", value=format_top_with_dashes(get_top(tickets_col, "staff_id", d7)), inline=True)
    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (30 дн.)", value=format_top_with_dashes(get_top(tickets_col, "staff_id", d30)), inline=True)
    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (Все время)", value=format_top_with_dashes(get_top(tickets_col, "staff_id")), inline=True)

    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (7 дн.)", value=format_top_with_dashes(get_top(tickets_col, "author_id", d7, True), "транскриптов"), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (30 дн.)", value=format_top_with_dashes(get_top(tickets_col, "author_id", d30, True), "транскриптов"), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (Все время)", value=format_top_with_dashes(get_top(tickets_col, "author_id", exclude_zero=True), "транскриптов"), inline=True)

    embed.add_field(name="🗑️ Удалено тикетов (7 дн.)", value=format_top_with_dashes(get_top(deleted_tickets_col, "staff_id", d7), "удалений"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (30 дн.)", value=format_top_with_dashes(get_top(deleted_tickets_col, "staff_id", d30), "удалений"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (Все время)", value=format_top_with_dashes(get_top(deleted_tickets_col, "staff_id"), "удалений"), inline=True)

    embed.set_footer(text=f"Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")
    return embed

# ================= Пункты 1, 6 & 7: ОБНОВЛЕННЫЙ HELP И КНОПКА НАЗАД =================

def build_help_embed(category: str = "main", user: discord.Member | discord.User = None) -> discord.Embed:
    embed = discord.Embed(title="⚙ Меню команд бота", color=EMBED_COLOR)

    if category == "main":
        embed.description = "Выберите категорию ниже, чтобы просмотреть доступные команды."
        embed.add_field(
            name="📋 Доступные категории",
            value="🎟️ **Тикеты** — Полный список команд для работы с тикетами и транскриптами.",
            inline=False
        )
    elif category == "tickets":
        embed.title = "🎟️ Категория: Тикеты"
        embed.description = "Используй префикс `.` или слэш-команды `/`\n"
        
        # Пункт 6: Оформление команд тикетов как на скриншоте
        embed.add_field(
            name="🛟 Команды Support",
            value="`.help` — *Показать меню команд.*\n\n"
                  "`.ticketstats [ID / упоминание]`\n| *Посмотреть статистику тикетов, транскриптов и удалений.*\n\n"
                  "`.leaderboard`\n| *Посмотреть топ модераторов по тикетам, транскриптам и удалениям.*",
            inline=False
        )
        embed.add_field(
            name="🧾 Команды Transcript",
            value="`.addticket [ID модератора] [ссылка] [категория]`\n| *Записать новый обработанный тикет в базу данных.*\n\n"
                  "`.ticketlogs [ID / упоминание]`\n| *Посмотреть логи тикетов модератора (с кнопками листания).*\n\n"
                  "`.deleteticket [номер лога] [ссылка на транскрипт]`\n| *Записать удаление тикета (канала) и добавить +1 удалённый тикет модератору.*",
            inline=False
        )
        embed.add_field(
            name="🛡️ Команды Администрации",
            value="`.deletelog [ID лога]`\n| *Удалить конкретный лог тикета по ID.*\n\n"
                  "`.resetlogs [ID / упоминание]`\n| *Очистить абсолютно все логи модератора (тикеты, транскрипты, удаления).*",
            inline=False
        )
        embed.add_field(
            name="⚙️ Команды Владельца",
            value="`.config`\n| *Открыть меню настроек бота (цвет, footer, доступ).*",
            inline=False
        )

    user_group = "Владелец" if is_owner_user(user) else "Пользователь"
    embed.set_footer(text=f"Ваша текущая группа: {user_group} • {FOOTER_TEXT}")
    return embed

# Пункт 1 & 7: HelpView с переключением эмбедов, проверкой на автора и кнопкой "Назад"
class HelpView(discord.ui.View):
    def __init__(self, author_id: int, user: discord.Member | discord.User):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.user = user
        self.show_main_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            embed = make_error_embed("Отказ в доступе", "Вы не можете управлять этим меню, так как вызвали его не вы.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    def show_main_buttons(self):
        self.clear_items()
        tickets_btn = discord.ui.Button(label="Тикеты", emoji="🎟️", style=discord.ButtonStyle.primary, custom_id="help_tickets")
        tickets_btn.callback = self.tickets_callback
        self.add_item(tickets_btn)

    def show_back_button(self):
        self.clear_items()
        back_btn = discord.ui.Button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="help_back")
        back_btn.callback = self.back_callback
        self.add_item(back_btn)

    async def tickets_callback(self, interaction: discord.Interaction):
        embed = build_help_embed("tickets", self.user)
        self.show_back_button()
        await interaction.response.edit_message(embed=embed, view=self)

    async def back_callback(self, interaction: discord.Interaction):
        embed = build_help_embed("main", self.user)
        self.show_main_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

# ================= Пункт 5: CONFIG С DROP-OUT (ВЫПАДАЮЩИМ МЕНЮ) И КНОПКОЙ НАЗАД =================

class ConfigSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Сервер", description="Цвет embed, Footer, Итоги, Доступ", emoji="⚙️", value="server"),
            discord.SelectOption(label="Тикеты", description="Настройка тикетов и канала логов", emoji="🎟️", value="tickets"),
        ]
        super().__init__(placeholder="Выберите категорию настроек...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        embed = discord.Embed(color=EMBED_COLOR)
        
        if category == "server":
            embed.title = "⚙️ Настройки сервера"
            embed.description = (
                f"**Цвет Embed:** `{CONFIG.get('embed_color')}`\n"
                f"**Footer Текст:** `{CONFIG.get('footer_text')}`\n\n"
                f"**Канал считалки:** <#{CONFIG.get('counting_channel_id')}> \n"
                f"**Канал бампа:** <#{CONFIG.get('bump_channel_id')}>\n\n"
                f"**Права и доступ:** Управляются через конфиг."
            )
        elif category == "tickets":
            log_chan = CONFIG.get('log_channel_id')
            toggles = CONFIG.get("log_toggles", {})
            toggles_fmt = "\n".join([f"`{cmd}`: {'✅' if val else '❌'}" for cmd, val in toggles.items()])
            embed.title = "🎟️ Настройки тикетов"
            embed.description = (
                f"**Канал логов тикетов:** <#{log_chan}>\n\n"
                f"**Логирование команд:**\n{toggles_fmt}"
            )
        
        embed.set_footer(text=FOOTER_TEXT)
        view = ConfigCategoryView(interaction.user.id, self.view)
        await interaction.response.edit_message(embed=embed, view=view)

class ConfigMainView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.add_item(ConfigSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            embed = make_error_embed("Отказ в доступе", "Вы не можете управлять меню конфигурации.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

class ConfigCategoryView(discord.ui.View):
    def __init__(self, author_id: int, parent_view: ConfigMainView):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.parent_view = parent_view

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            embed = make_error_embed("Отказ в доступе", "Вы не можете управлять меню конфигурации.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_config_embed()
        await interaction.response.edit_message(embed=embed, view=self.parent_view)

def build_config_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⚙ Настройки бота",
        description="Выберите категорию в выпадающем меню ниже для перехода к настройкам.",
        color=EMBED_COLOR
    )
    embed.add_field(name="⚙️ Сервер", value="Настройка цвета, футера, каналов итогов и прав доступа.", inline=False)
    embed.add_field(name="🎟️ Тикеты", value="Настройка тикетов и каналов логирования.", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

# ================= 11. КОМАНДЫ БОТА (ВКЛЮЧАЯ АЛИАСЫ И НОВЫЕ КОМАНДЫ) =================

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    embed = build_help_embed("main", ctx.author)
    view = HelpView(ctx.author.id, ctx.author)
    await ctx.send(embed=embed, view=view)

# Алиасы sum / summaries
@bot.command(name="sum", aliases=["summaries"])
@check_access_decorator("sum")
async def summaries_cmd(ctx: commands.Context):
    embed = process_results()
    await ctx.send(embed=embed)

# Алиасы lb / leaderboard
@bot.command(name="leaderboard", aliases=["lb"])
@check_access_decorator("leaderboard")
async def leaderboard_cmd(ctx: commands.Context):
    embed = process_leaderboard()
    await ctx.send(embed=embed)

# Алиасы ts / ticketstats
@bot.command(name="ticketstats", aliases=["ts"])
@check_access_decorator("ticketstats")
async def ticketstats_cmd(ctx: commands.Context, target: discord.User = None):
    target = target or ctx.author
    t_count = tickets_col.count_documents({"staff_id": target.id})
    tr_count = tickets_col.count_documents({"author_id": target.id})
    del_count = deleted_tickets_col.count_documents({"staff_id": target.id})

    embed = discord.Embed(title=f"📊 Статистика — {target.name}", color=EMBED_COLOR)
    embed.add_field(name="🎟️ Обработано тикетов", value=str(t_count), inline=True)
    embed.add_field(name="🧾 Занесено транскриптов", value=str(tr_count), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов", value=str(del_count), inline=True)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

# Алиасы t / addticket
@bot.command(name="addticket", aliases=["t"])
@check_access_decorator("addticket")
async def addticket_cmd(ctx: commands.Context, staff: discord.User = None, transcript_url: str = None, *, category: str = None):
    if not staff or not transcript_url or not category:
        embed = make_error_embed(
            "Недостаточно аргументов",
            COMMAND_USAGE_HELP["addticket"]
        )
        return await ctx.send(embed=embed)

    if category not in VALID_CATEGORIES:
        cats = ", ".join(f"`{c}`" for c in VALID_CATEGORIES)
        embed = make_error_embed("Неверная категория", f"Указана недопустимая категория!\nРазрешенные: {cats}")
        return await ctx.send(embed=embed)

    log_id = get_next_sequence_value("ticket_id")
    now = datetime.now(timezone.utc)

    ticket_doc = {
        "_id": log_id,
        "staff_id": staff.id,
        "author_id": ctx.author.id,
        "transcript_url": transcript_url,
        "category": category,
        "created_at": now
    }
    tickets_col.insert_one(ticket_doc)

    month_ago = now - timedelta(days=30)
    month_count = tickets_col.count_documents({"staff_id": staff.id, "created_at": {"$gte": month_ago}})

    embed = discord.Embed(title=f"📋 Лог №{log_id} — {staff.name}", color=EMBED_COLOR)
    embed.add_field(name="Дата транскрипта", value=f"<t:{int(now.timestamp())}:f>", inline=False)
    embed.add_field(name="Ссылка на транскрипт", value=transcript_url, inline=False)
    embed.add_field(name="Кто вёл тикет", value=f"{staff.id} ({staff.mention})", inline=False)
    embed.add_field(name="Внёс в базу", value=ctx.author.mention, inline=False)
    embed.add_field(name="Тикетов за последний месяц", value=str(month_count), inline=False)
    embed.add_field(name="Категория", value=category, inline=False)
    embed.set_footer(text=FOOTER_TEXT)

    await ctx.send(embed=embed)
    await log_action(ctx.guild, "addticket", embed)

# Алиасы tl / ticketlogs
@bot.command(name="ticketlogs", aliases=["tl"])
@check_access_decorator("ticketlogs")
async def ticketlogs_cmd(ctx: commands.Context, target: discord.User = None):
    target = target or ctx.author
    logs = list(tickets_col.find({"staff_id": target.id}).sort("_id", ASCENDING))
    if not logs:
        embed = make_status_embed("Тикеты", f"У модератора {target.mention} нет логов тикетов.", "info")
        return await ctx.send(embed=embed)

    lines = []
    for doc in logs[:5]:
        lines.append(f"**Лог №{doc['_id']}** | {doc['category']} | [Ссылка]({doc['transcript_url']})")
    
    embed = discord.Embed(title=f"📜 Логи тикетов — {target.name}", description="\n".join(lines), color=EMBED_COLOR)
    embed.set_footer(text=FOOTER_TEXT)
    await ctx.send(embed=embed)

# Пункт 8: Новая команда deleteticket (добавляет +1 в удаленные тикеты)
@bot.command(name="deleteticket")
@check_access_decorator("deleteticket")
async def deleteticket_cmd(ctx: commands.Context, log_id: int = None, transcript_url: str = None):
    if log_id is None or transcript_url is None:
        embed = make_error_embed(
            "Недостаточно аргументов",
            COMMAND_USAGE_HELP["deleteticket"]
        )
        return await ctx.send(embed=embed)

    now = datetime.now(timezone.utc)
    deleted_id = get_next_sequence_value("deleted_ticket_id")

    deleted_doc = {
        "_id": deleted_id,
        "original_log_id": log_id,
        "staff_id": ctx.author.id,
        "transcript_url": transcript_url,
        "created_at": now
    }
    deleted_tickets_col.insert_one(deleted_doc)

    embed = make_status_embed(
        "Тикет удален",
        f"Удаление тикета по логу **№{log_id}** успешно зафиксировано!\nМодератору {ctx.author.mention} добавлено **+1** удаление тикета в казну.",
        "delete"
    )
    await ctx.send(embed=embed)
    await log_action(ctx.guild, "deleteticket", embed)

# Пункт 8: deletelog (бывший deleteticket) c алиасом del
@bot.command(name="deletelog", aliases=["del"])
@check_access_decorator("deletelog")
async def deletelog_cmd(ctx: commands.Context, log_id: int = None):
    if log_id is None:
        embed = make_error_embed("Недостаточно аргументов", COMMAND_USAGE_HELP["deletelog"])
        return await ctx.send(embed=embed)

    res = tickets_col.delete_one({"_id": log_id})
    if res.deleted_count > 0:
        embed = make_status_embed("Удаление лога", f"Лог тикета **№{log_id}** успешно удален из базы данных.", "success")
        await ctx.send(embed=embed)
        await log_action(ctx.guild, "deletelog", embed)
    else:
        embed = make_error_embed("Ошибка", f"Лог с номером **№{log_id}** не найден.")
        await ctx.send(embed=embed)

# Пункт 8: resetlogs (бывший resettickets)
@bot.command(name="resetlogs")
@check_access_decorator("resetlogs")
async def resetlogs_cmd(ctx: commands.Context, target: discord.User = None):
    if not target:
        embed = make_error_embed("Недостаточно аргументов", COMMAND_USAGE_HELP["resetlogs"])
        return await ctx.send(embed=embed)

    res1 = tickets_col.delete_many({"staff_id": target.id})
    res2 = deleted_tickets_col.delete_many({"staff_id": target.id})

    embed = make_status_embed(
        "Сброс логов",
        f"Все логи пользователя {target.mention} очищены.\nУдалено тикетов: **{res1.deleted_count}**, удалено записей удалений: **{res2.deleted_count}**.",
        "success"
    )
    await ctx.send(embed=embed)
    await log_action(ctx.guild, "resetlogs", embed)

# Алиасы cfg / config
@bot.command(name="config", aliases=["cfg"])
async def config_cmd(ctx: commands.Context):
    if not is_owner_user(ctx.author):
        embed = make_error_embed("Недостаточно прав", "Эта команда доступна только владельцу бота.")
        return await ctx.send(embed=embed)
    
    embed = build_config_embed()
    view = ConfigMainView(ctx.author.id)
    await ctx.send(embed=embed, view=view)

# ================= ЗАПУСК БОТА =================

BOT_TOKEN = os.getenv("DISCORD_TOKEN")
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("Ошибка: DISCORD_TOKEN не найден в .env")
    bot.run(BOT_TOKEN)
