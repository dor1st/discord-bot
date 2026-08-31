import os
import math
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING, ASCENDING

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


# ================= ВЛАДЕЛЕЦ БОТА =================
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

COMMAND_LABELS = {
    "help": "help",
    "addticket": "addticket",
    "deleteticket": "deleteticket",
    "ticketstats": "ticketstats",
    "ticketlogs": "ticketlogs",
    "leaderboard": "leaderboard",
    "deletelog": "deletelog",
    "resetlogs": "resetlogs",
    "sum": "sum / summaries",
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
        "leaderboard_layout": "horizontal",
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

        toggles = doc.get("log_toggles", {})
        for cmd, default_value in LOGGABLE_COMMANDS_DEFAULT.items():
            if cmd not in toggles:
                toggles[cmd] = default_value
                updated = True
        doc["log_toggles"] = toggles

        groups = doc.get("permission_groups", {})
        for group_key, group_default in PERMISSION_GROUPS_DEFAULT.items():
            if group_key not in groups:
                groups[group_key] = dict(group_default)
                updated = True
            else:
                for key, value in group_default.items():
                    if key not in groups[group_key]:
                        groups[group_key][key] = value
                        updated = True
        doc["permission_groups"] = groups

        if updated:
            settings_col.update_one({"_id": "config"}, {"$set": doc}, upsert=True)

    CONFIG = doc
    apply_config_globals()

def update_config(patch: dict):
    global CONFIG
    settings_col.update_one({"_id": "config"}, {"$set": patch}, upsert=True)
    for key, value in patch.items():
        if "." not in key:
            CONFIG[key] = value
            continue
        target = CONFIG
        parts = key.split(".")
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
    apply_config_globals()

def get_emoji(name: str) -> str:
    return CONFIG.get("emojis", {}).get(name, EMOJI_DEFAULTS.get(name, ""))

def make_status_embed(title: str, message: str, kind: str = "info") -> discord.Embed:
    emoji = get_emoji(kind)
    embed = discord.Embed(
        title=title,
        description=f"{emoji} {message}" if emoji else message,
        color=EMBED_COLOR,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def make_header_embed(header_key: str):
    url = HEADER_IMAGES.get(header_key, "")
    if not url:
        return None
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_image(url=url)
    return embed

async def send_embed_with_header(destination, embed: discord.Embed, header_key: str | None = None, **kwargs):
    header = make_header_embed(header_key) if header_key else None
    if isinstance(destination, discord.Interaction):
        if header:
            await destination.response.send_message(embed=header, **kwargs)
            return await destination.followup.send(embed=embed, wait=True, **kwargs)
        await destination.response.send_message(embed=embed, **kwargs)
        return await destination.original_response()
    if header:
        await destination.send(embed=header)
    return await destination.send(embed=embed, **kwargs)

load_config()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)
LOGS_PER_PAGE = 3

# ================= НАСТРОЙКА РОЛЕЙ И КАНАЛОВ =================

ALLOWED_CHANNEL_IDS = [1322968592202993746, 1537220150267220018]
VALID_CATEGORIES = ["Помощь по серверу", "Получение призов", "Получение роли", "Покупка рекламы"]

def get_group(group_key: str) -> dict:
    return CONFIG.get("permission_groups", {}).get(group_key, {})

def get_group_role_ids(group_key: str) -> list[int]:
    return [int(x) for x in get_group(group_key).get("roles", [])]

def is_owner_user(user: discord.Member | discord.User) -> bool:
    return user.id == OWNER_ID

def user_in_group(user: discord.Member | discord.User, group_key: str) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if is_owner_user(user) or user.guild_permissions.administrator:
        return True
    return any(role.id in get_group_role_ids(group_key) for role in user.roles)

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
    return False, "Ваша группа не имеет доступа к этой команде. Доступ можно изменить в конфигурации бота."

def check_access_decorator(role_ids=None, is_slash: bool = False, command_name: str | None = None):
    configured_command_name = command_name
    async def predicate(target):
        user = target.user if is_slash else target.author
        channel_id = target.channel_id if is_slash else target.channel.id
        command = getattr(target, "command", None)
        current_command_name = configured_command_name or getattr(command, "name", "") or ""
        ok, msg = check_access(user, channel_id, current_command_name)
        if not ok:
            if is_slash:
                raise app_commands.AppCommandError(msg)
            raise commands.CheckFailure(msg)
        return True
    return app_commands.check(predicate) if is_slash else commands.check(predicate)

def check_support_prefix(command_name=None): return check_access_decorator(command_name=command_name)
def check_transcript_prefix(command_name=None): return check_access_decorator(command_name=command_name)
def check_admin_prefix(command_name=None): return check_access_decorator(command_name=command_name)
def check_support_slash(command_name=None): return check_access_decorator(is_slash=True, command_name=command_name)
def check_transcript_slash(command_name=None): return check_access_decorator(is_slash=True, command_name=command_name)
def check_admin_slash(command_name=None): return check_access_decorator(is_slash=True, command_name=command_name)

def is_valid_addticket(transcript_url: str, category: str) -> bool:
    return "https://discord.com/" in transcript_url and category in VALID_CATEGORIES

def is_valid_transcript_link(transcript_url: str) -> bool:
    return "https://discord.com/" in transcript_url

def is_transcript_exists(transcript_url: str) -> bool:
    return tickets_col.find_one({"transcript_url": transcript_url}) is not None

def is_deleted_transcript_exists(transcript_url: str) -> bool:
    return deleted_tickets_col.find_one({"transcript_url": transcript_url}) is not None

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot logged in as {bot.user}")

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

# 5. ИТОГИ В ДВЕ КОЛОНКИ
def process_results():
    now = datetime.now(timezone.utc)
    d7 = (now - timedelta(days=7)).date().isoformat()
    d30 = (now - timedelta(days=30)).date().isoformat()

    def top_stats(collection, start_day, channel_id):
        if not channel_id:
            return []
        pipeline = [
            {"$match": {"channel_id": channel_id, "day": {"$gte": start_day}}},
            {"$group": {"_id": "$user_id", "count": {"$sum": "$count"}}},
            {"$sort": {"count": -1}},
            {"$limit": 10},
        ]
        return [(doc["_id"], doc["count"]) for doc in collection.aggregate(pipeline)]

    def fmt(rows):
        if not rows:
            return "— *Нет данных*"
        return "\n".join(f"`{i}.` <@{uid}> — **{count}**" for i, (uid, count) in enumerate(rows, 1))

    embed = discord.Embed(title=f"{get_emoji('info')} Итоги", color=EMBED_COLOR)
    # Ряд 1: 7 дней
    embed.add_field(name="💬 Сообщения — 7 дней", value=fmt(top_stats(message_stats_col, d7, CONFIG.get("counting_channel_id"))), inline=True)
    embed.add_field(name="🚀 Bump — 7 дней", value=fmt(top_stats(bump_stats_col, d7, CONFIG.get("bump_channel_id"))), inline=True)
    
    # Ряд 2: 30 дней
    embed.add_field(name="💬 Сообщения — 30 дней", value=fmt(top_stats(message_stats_col, d30, CONFIG.get("counting_channel_id"))), inline=True)
    embed.add_field(name="🚀 Bump — 30 дней", value=fmt(top_stats(bump_stats_col, d30, CONFIG.get("bump_channel_id"))), inline=True)
    
    # Ряд 3: Каналы
    count_channel = CONFIG.get("counting_channel_id")
    bump_channel = CONFIG.get("bump_channel_id")
    embed.add_field(name="Канал считалки", value=f"<#{count_channel}>" if count_channel else "Не установлен", inline=True)
    embed.add_field(name="Канал bump", value=f"<#{bump_channel}>" if bump_channel else "Не установлен", inline=True)
    
    embed.set_footer(text=FOOTER_TEXT)
    return embed

@bot.event
async def on_message(message: discord.Message):
    record_message_stat(message)
    await bot.process_commands(message)

@bot.event
async def on_interaction(interaction: discord.Interaction):
    record_bump_stat(interaction)

# ================= БИЗНЕС-ЛОГИКА ТИКЕТОВ И СТАТИСТИКИ =================

def process_ticket_logs(target_user: discord.User, page: int = 1) -> tuple[discord.Embed, int]:
    logs = list(tickets_col.find({"staff_id": target_user.id}).sort("_id", ASCENDING))

    if not logs:
        embed = discord.Embed(
            title=f"Тикеты — {target_user.name}",
            description=f"{get_emoji('error')} У этого модератора нет ни одного тикета.",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Страница 1/1 (0 логов) • {FOOTER_TEXT}")
        return embed, 1

    total_logs = len(logs)
    total_pages = math.ceil(total_logs / LOGS_PER_PAGE)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * LOGS_PER_PAGE
    current_logs = logs[start_idx:start_idx + LOGS_PER_PAGE]

    embed = discord.Embed(title=f"<:ticket:1522343287816716379> Тикеты — {target_user.name}", color=EMBED_COLOR)
    embed.description = f"{target_user.id}\n" + "—" * 28

    lines = []
    for doc in current_logs:
        log_id = doc["_id"]
        transcript_url = doc["transcript_url"]
        category = doc["category"]
        created_at = doc["created_at"]

        formatted_date = f"<t:{int(created_at.timestamp())}:F>" if isinstance(created_at, datetime) else str(created_at)

        lines.append(
            f"**Тикет №{log_id}**\n"
            f"**Модератор:** {target_user.name} ({target_user.mention})\n"
            f"**Категория:** {category}\n"
            f"**Транскрипт:** {transcript_url}\n"
            f"{formatted_date}"
        )

    embed.description += "\n\n" + "\n\n".join(lines)
    embed.set_footer(text=f"Страница {current_page}/{total_pages} ({total_logs} логов) • {FOOTER_TEXT}")
    return embed, total_pages

# 4. ЛИДЕРБОРД СЛЕВА НАПРАВО (7 дн, 30 дн, Все время)
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
            {"$limit": 5}
        ])

        results = list(collection.aggregate(pipeline))
        return [(doc["_id"], doc["cnt"]) for doc in results]

    def format_top(top_list, unit_label="тикетов"):
        if not top_list:
            return "— *Нет данных*"
        return "\n".join([f"`{idx}.` <@{u_id}> — **{count}** {unit_label}" for idx, (u_id, count) in enumerate(top_list, 1)])

    embed = discord.Embed(title="<:sparkles:1522342290494849034> Лидерборд тикетов и транскриптов", color=EMBED_COLOR)

    # Столбец 1: 7 дней
    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (7 дн.)", value=format_top(get_top(tickets_col, "staff_id", d7)), inline=True)
    # Столбец 2: 30 дней
    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (30 дн.)", value=format_top(get_top(tickets_col, "staff_id", d30)), inline=True)
    # Столбец 3: Все время
    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (Все время)", value=format_top(get_top(tickets_col, "staff_id")), inline=True)

    # Второй ряд
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (7 дн.)", value=format_top(get_top(tickets_col, "author_id", d7, True), "транскриптов"), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (30 дн.)", value=format_top(get_top(tickets_col, "author_id", d30, True), "транскриптов"), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (Все время)", value=format_top(get_top(tickets_col, "author_id", exclude_zero=True), "транскриптов"), inline=True)

    # Третий ряд
    embed.add_field(name="🗑️ Удалено тикетов (7 дн.)", value=format_top(get_top(deleted_tickets_col, "staff_id", d7), "удалений"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (30 дн.)", value=format_top(get_top(deleted_tickets_col, "staff_id", d30), "удалений"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (Все время)", value=format_top(get_top(deleted_tickets_col, "staff_id"), "удалений"), inline=True)

    embed.set_footer(text=f"Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")
    return embed

# ================= 2. ОБНОВЛЕННЫЙ HELP EMBED И VIEW =================

def build_help_embed(category: str = "main", user: discord.Member | discord.User = None) -> discord.Embed:
    embed = discord.Embed(title="⚙ Меню команд бота", color=EMBED_COLOR)

    if category == "main":
        embed.description = "Выберите категорию команд ниже."
        embed.add_field(
            name="📋 Общие команды",
            value="`.help` — Открыть данное меню справочной информации.\n`.config` — Настройка бота, прав, логов и каналов.",
            inline=False
        )
    elif category == "tickets":
        embed.description = "**Команды для работы с тикетами и транскриптами:**"
        embed.add_field(
            name="Тикеты",
            value="`.addticket` — Внести транскрипт тикета в базу данных.\n"
                  "`.deleteticket` — Отметить тикет как удаленный.\n"
                  "`.ticketlogs` — Просмотреть логи тикетов пользователя.\n"
                  "`.ticketstats` — Просмотреть статистику тикетов пользователя.\n"
                  "`.leaderboard` — Открыть топ пользователей по тикетам.",
            inline=False
        )
    elif category == "other":
        embed.description = "**Прочие команды:**"
        embed.add_field(
            name="Статистика и подсчет",
            value="`.sum` / `.summaries` — Статистика сообщений и bump на сервере.",
            inline=False
        )

    user_group = "Владелец" if is_owner_user(user) else "Пользователь"
    embed.set_footer(text=f"Ваша текущая группа: {user_group} • {FOOTER_TEXT}")
    return embed

class HelpView(discord.ui.View):
    def __init__(self, author_id: int, user: discord.Member | discord.User):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Тикеты", emoji="🎟️", style=discord.ButtonStyle.primary)
    async def tickets_cat(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_help_embed("tickets", self.user)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Другое", emoji="📊", style=discord.ButtonStyle.primary)
    async def other_cat(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_help_embed("other", self.user)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Главная", emoji="🏠", style=discord.ButtonStyle.secondary)
    async def main_cat(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_help_embed("main", self.user)
        await interaction.response.edit_message(embed=embed, view=self)

# ================= 3. & 6. НАСТРОЙКИ БОТА (ВЕРТИКАЛЬНО И БЕЗ ЭМОДЗИ) =================

def build_config_embed() -> discord.Embed:
    embed = discord.Embed(title="⚙ Настройки бота", description="Выберите категорию настроек ниже.", color=EMBED_COLOR)
    
    # Вертикальное отображение категорий (в 1 столб)
    embed.add_field(name="⚙ General", value="Цвет и footer", inline=False)
    embed.add_field(name="💸 Тикеты", value="Канал логов и логируемые команды", inline=False)
    embed.add_field(name="🛡 Доступ", value="Группы, роли и команды", inline=False)
    embed.add_field(name="🏆 Лидерборды", value="Горизонтально или вертикально", inline=False)
    embed.add_field(name="📊 Итоги", value="Каналы считалки и bump", inline=False)
    embed.add_field(
        name="🖼 Header-изображения",
        value="Ссылки задаются только в переменной `HEADER_IMAGES` в коде.",
        inline=False
    )
    
    embed.set_footer(text=FOOTER_TEXT)
    return embed

# ================= КОМАНДЫ БОТА =================

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    embed = build_help_embed("main", ctx.author)
    view = HelpView(ctx.author.id, ctx.author)
    await send_embed_with_header(ctx, embed, "help", view=view)

@bot.tree.command(name="help", description="Открыть меню команд бота")
async def help_slash(interaction: discord.Interaction):
    embed = build_help_embed("main", interaction.user)
    view = HelpView(interaction.user.id, interaction.user)
    await send_embed_with_header(interaction, embed, "help", view=view)

# 1. ЗАМЕНА .ПОДСЧЕТ НА .SUM / .SUMMARIES
@bot.command(name="sum", aliases=["summaries"])
@check_support_prefix("sum")
async def summaries_cmd(ctx: commands.Context):
    embed = process_results()
    await send_embed_with_header(ctx, embed, "results")

@bot.tree.command(name="sum", description="Посмотреть итоги активности по сообщениям и bump")
@check_support_slash("sum")
async def summaries_slash(interaction: discord.Interaction):
    embed = process_results()
    await send_embed_with_header(interaction, embed, "results")

@bot.command(name="leaderboard")
@check_support_prefix("leaderboard")
async def leaderboard_cmd(ctx: commands.Context):
    embed = process_leaderboard()
    await send_embed_with_header(ctx, embed, "leaderboard")

@bot.tree.command(name="leaderboard", description="Лидерборд тикетов и транскриптов")
@check_support_slash("leaderboard")
async def leaderboard_slash(interaction: discord.Interaction):
    embed = process_leaderboard()
    await send_embed_with_header(interaction, embed, "leaderboard")

@bot.command(name="config")
async def config_cmd(ctx: commands.Context):
    if not is_owner_user(ctx.author):
        return await ctx.send(embed=make_status_embed("Недостаточно прав", "Эта команда доступна только владельцу бота.", "error"))
    embed = build_config_embed()
    await send_embed_with_header(ctx, embed, "config")

# ================= ЗАПУСК БОТА =================

BOT_TOKEN = os.getenv("DISCORD_TOKEN")
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("Ошибка: DISCORD_TOKEN не найден в .env")
    bot.run(BOT_TOKEN)
