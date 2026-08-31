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
    "summaries": False,
    "help": False,
    "config": True,
}

PERMISSION_GROUPS_DEFAULT = {
    "support": {
        "name": "Поддержка",
        "emoji": "🛟",
        "roles": [1501507449860001853, 1322962344040464424],
        "commands": ["help", "ticketstats", "leaderboard", "summaries"],
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
    "summaries": "sum / summaries",
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

        if doc.get("leaderboard_layout") not in ("horizontal", "vertical"):
            doc["leaderboard_layout"] = "horizontal"
            updated = True

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
    return EMOJI_DEFAULTS.get(name, "")

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
    return False, "Ваша группа не имеет доступа к этой команде."

def check_access_decorator(is_slash: bool = False, command_name: str | None = None):
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

# ================= ЛОГИРОВАНИЕ ДЕЙСТВИЙ В КАНАЛ ЛОГОВ =================

def build_log_embed(title: str, lines: list[str]) -> discord.Embed:
    embed = discord.Embed(title=title, description="\n".join(lines), color=EMBED_COLOR, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=FOOTER_TEXT)
    return embed

async def send_log(command_name: str, embed: discord.Embed):
    if not CONFIG.get("log_toggles", {}).get(command_name, False):
        return
    channel_id = CONFIG.get("log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return
    try:
        await channel.send(embed=embed)
    except Exception:
        pass

# ================= ОБРАБОТКА ОШИБОК =================

async def send_error_ctx(ctx: commands.Context, message: str, title: str = "Ошибка"):
    await ctx.send(embed=make_status_embed(title, message, "error"))

async def send_error_interaction(interaction: discord.Interaction, message: str, title: str = "Ошибка", ephemeral: bool = True):
    embed = make_status_embed(title, message, "error")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await send_error_ctx(ctx, str(error) or "У вас нет доступа к этой команде.", "Недостаточно прав")
        return
    await send_error_ctx(ctx, str(error) or "Произошла ошибка при выполнении команды.")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandNotFound):
        return
    if isinstance(error, app_commands.CommandOnCooldown):
        await send_error_interaction(interaction, f"Подождите ещё {error.retry_after:.1f} сек.", "Команда на кулдауне")
        return
    await send_error_interaction(interaction, str(error) or "Произошла ошибка при выполнении команды.")

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
    
    # 1 ряд: 7 дней
    embed.add_field(name="💬 Сообщения — 7 дней", value=fmt(top_stats(message_stats_col, d7, CONFIG.get("counting_channel_id"))), inline=True)
    embed.add_field(name="🚀 Bump — 7 дней", value=fmt(top_stats(bump_stats_col, d7, CONFIG.get("bump_channel_id"))), inline=True)
    
    # 2 ряд: 30 дней
    embed.add_field(name="💬 Сообщения — 30 дней", value=fmt(top_stats(message_stats_col, d30, CONFIG.get("counting_channel_id"))), inline=True)
    embed.add_field(name="🚀 Bump — 30 дней", value=fmt(top_stats(bump_stats_col, d30, CONFIG.get("bump_channel_id"))), inline=True)
    
    # 3 ряд: Канал считалки и bump
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

# ================= БИЗНЕС-ЛОГИКА ТИКЕТОВ =================

def get_monthly_tickets(staff_id: int) -> int:
    date_30_days = datetime.now(timezone.utc) - timedelta(days=30)
    return tickets_col.count_documents({
        "staff_id": staff_id,
        "created_at": {"$gte": date_30_days}
    })

def process_add_ticket(author_user: discord.User, staff_user: discord.User, transcript_url: str, category: str):
    ticket_id = get_next_sequence_value("ticket_id")
    now = datetime.now(timezone.utc)

    tickets_col.insert_one({
        "_id": ticket_id,
        "staff_id": staff_user.id,
        "author_id": author_user.id,
        "transcript_url": transcript_url,
        "category": category,
        "created_at": now
    })

    monthly_count = get_monthly_tickets(staff_user.id)
    discord_timestamp = f"<t:{int(now.timestamp())}:F>"

    embed = discord.Embed(title=f"<:logs:1522340749998428160> Лог тикета — {staff_user.display_name}", color=EMBED_COLOR)
    embed.add_field(name="Дата транскрипта", value=discord_timestamp, inline=False)
    embed.add_field(name="Номер лога", value=f"№{ticket_id}", inline=False)
    embed.add_field(name="Ссылка на транскрипт", value=transcript_url, inline=False)
    embed.add_field(name="Кто вёл тикет", value=str(staff_user.id), inline=False)
    embed.add_field(name="Внёс в базу", value=author_user.mention, inline=False)
    embed.add_field(name="Тикетов за последний месяц", value=str(monthly_count), inline=False)
    embed.add_field(name="Категория", value=category, inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed, ticket_id

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

        if isinstance(created_at, datetime):
            formatted_date = f"<t:{int(created_at.timestamp())}:F>"
        else:
            formatted_date = str(created_at)

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

# ================= КОМПОНЕНТ НАВИГАЦИИ (КНОПКИ СТРАНИЦ) =================

class TicketLogsPaginationView(discord.ui.View):
    def __init__(self, author_id: int, target_user: discord.User, total_pages: int, initial_page: int = 1):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.target_user = target_user
        self.total_pages = total_pages
        self.current_page = initial_page
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = (self.current_page <= 1)
        self.next_button.disabled = (self.current_page >= self.total_pages)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await send_error_interaction(interaction, "Вы не можете переключать страницы в чужом меню.", "Доступ запрещён")
            return False
        return True

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
            embed, _ = process_ticket_logs(self.target_user, self.current_page)
            self.update_buttons()
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages:
            self.current_page += 1
            embed, _ = process_ticket_logs(self.target_user, self.current_page)
            self.update_buttons()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if hasattr(self, 'message') and self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

def process_ticket_stats(target_user: discord.User):
    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    c7_s = tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": d7}})
    c30_s = tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": d30}})
    call_s = tickets_col.count_documents({"staff_id": target_user.id})

    c7_a = tickets_col.count_documents({"author_id": target_user.id, "created_at": {"$gte": d7}})
    c30_a = tickets_col.count_documents({"author_id": target_user.id, "created_at": {"$gte": d30}})
    call_a = tickets_col.count_documents({"author_id": target_user.id})

    c7_d = deleted_tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": d7}})
    c30_d = deleted_tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": d30}})
    call_d = deleted_tickets_col.count_documents({"staff_id": target_user.id})

    l_s_doc = tickets_col.find_one({"staff_id": target_user.id}, sort=[("_id", DESCENDING)])
    l_a_doc = tickets_col.find_one({"author_id": target_user.id}, sort=[("_id", DESCENDING)])

    def fmt_last(doc):
        if not doc:
            return "—"
        dt = doc.get("created_at")
        if isinstance(dt, datetime):
            return f"<t:{int(dt.timestamp())}:R>"
        return str(dt)

    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=target_user.name, icon_url=target_user.display_avatar.url)
    embed.title = "<:ticket:1522343287816716379> Статистика тикетов и транскриптов"
    embed.add_field(name="За последние 7 дней:", value=f"• Тикетов: **{c7_s}**\n• Транскриптов: **{c7_a}**\n• Удалено тикетов: **{c7_d}**", inline=True)
    embed.add_field(name="За последние 30 дней:", value=f"• Тикетов: **{c30_s}**\n• Транскриптов: **{c30_a}**\n• Удалено тикетов: **{c30_d}**", inline=True)
    embed.add_field(name="За всё время:", value=f"• Тикетов: **{call_s}**\n• Транскриптов: **{call_a}**\n• Удалено тикетов: **{call_d}**", inline=True)
    embed.add_field(
        name="<:lighting:1522337543360872489> Активность:",
        value=f"• **Последний проведённый тикет:** {fmt_last(l_s_doc)}\n• **Последний внесённый транскрипт:** {fmt_last(l_a_doc)}",
        inline=False
    )
    embed.set_footer(text=f"ID: {target_user.id} • Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")
    return embed

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

    # 1 РЯД: 7 дней
    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (7 дн.)", value=format_top(get_top(tickets_col, "staff_id", d7)), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (7 дн.)", value=format_top(get_top(tickets_col, "author_id", d7, True), "транскриптов"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (7 дн.)", value=format_top(get_top(deleted_tickets_col, "staff_id", d7), "удалений"), inline=True)

    # 2 РЯД: 30 дней
    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (30 дн.)", value=format_top(get_top(tickets_col, "staff_id", d30)), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (30 дн.)", value=format_top(get_top(tickets_col, "author_id", d30, True), "транскриптов"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (30 дн.)", value=format_top(get_top(deleted_tickets_col, "staff_id", d30), "удалений"), inline=True)

    # 3 РЯД: Все время
    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (Все время)", value=format_top(get_top(tickets_col, "staff_id")), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (Все время)", value=format_top(get_top(tickets_col, "author_id", exclude_zero=True), "транскриптов"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (Все время)", value=format_top(get_top(deleted_tickets_col, "staff_id"), "удалений"), inline=True)

    embed.set_footer(text=f"Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")
    return embed

def delete_ticket_log(log_id: int):
    return tickets_col.find_one_and_delete({"_id": log_id})

def reset_tickets(staff_id: int) -> int:
    res1 = tickets_col.delete_many({"$or": [{"staff_id": staff_id}, {"author_id": staff_id}]})
    res2 = deleted_tickets_col.delete_many({"$or": [{"staff_id": staff_id}, {"deleted_by": staff_id}]})
    return res1.deleted_count + res2.deleted_count

def process_delete_ticket_channel(author_user: discord.User, log_id: int, transcript_url: str):
    original = tickets_col.find_one({"_id": log_id})
    if not original:
        return None, "not_found"

    if is_deleted_transcript_exists(transcript_url):
        return None, "duplicate"

    staff_id = original["staff_id"]
    deleted_id = get_next_sequence_value("deleted_ticket_id")
    now = datetime.now(timezone.utc)

    deleted_tickets_col.insert_one({
        "_id": deleted_id,
        "original_log_id": log_id,
        "staff_id": staff_id,
        "deleted_by": author_user.id,
        "transcript_url": transcript_url,
        "created_at": now,
    })

    monthly_count = deleted_tickets_col.count_documents({
        "staff_id": staff_id,
        "created_at": {"$gte": datetime.now(timezone.utc) - timedelta(days=30)}
    })
    discord_timestamp = f"<t:{int(now.timestamp())}:F>"

    embed = discord.Embed(title="🗑️ Удалён тикет", color=EMBED_COLOR)
    embed.add_field(name="Дата удаления", value=discord_timestamp, inline=False)
    embed.add_field(name="Номер записи удаления", value=f"№{deleted_id}", inline=False)
    embed.add_field(name="Номер тикета", value=f"№{log_id}", inline=False)
    embed.add_field(name="Ссылка на транскрипт", value=transcript_url, inline=False)
    embed.add_field(name="Модератор тикета", value=f"<@{staff_id}> ({staff_id})", inline=False)
    embed.add_field(name="Удалил канал", value=author_user.mention, inline=False)
    embed.add_field(name="Удалено тикетов за 30 дней", value=str(monthly_count), inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed, None


# ================= ИНТЕРАКТИВНОЕ МЕНЮ CONFIG И HELP =================

# 3. ВЕРТИКАЛЬНЫЙ CONFIG БЕЗ ЭМОДЗИ
class ConfigMainView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="Выберите категорию настроек...",
        options=[
            discord.SelectOption(label="General", description="Цвет и footer", emoji="⚙️", value="general"),
            discord.SelectOption(label="Тикеты", description="Канал логов и логируемые команды", emoji="🎟️", value="tickets"),
            discord.SelectOption(label="Доступ", description="Группы, роли и команды", emoji="🛡️", value="access"),
            discord.SelectOption(label="Лидерборды", description="Горизонтально или вертикально", emoji="🏆", value="leaderboards"),
            discord.SelectOption(label="Итоги", description="Каналы считалки и bump", emoji="📊", value="results"),
            discord.SelectOption(label="Header-изображения", description="Ссылки в коде HEADER_IMAGES", emoji="🖼️", value="headers"),
        ]
    )
    async def select_category(self, interaction: discord.Interaction, select: discord.ui.Select):
        val = select.values[0]
        if val == "general":
            await interaction.response.send_modal(ConfigGeneralModal(self.user_id))
        elif val == "tickets":
            await interaction.response.edit_message(embed=make_config_tickets_embed(), view=ConfigTicketsView(self.user_id))
        elif val == "access":
            await interaction.response.edit_message(embed=make_config_access_embed(), view=ConfigAccessGroupSelectView(self.user_id))
        elif val == "leaderboards":
            await interaction.response.edit_message(embed=make_config_leaderboard_embed(), view=ConfigLeaderboardView(self.user_id))
        elif val == "results":
            await interaction.response.edit_message(embed=make_config_results_embed(), view=ConfigResultsView(self.user_id))
        elif val == "headers":
            await interaction.response.edit_message(embed=make_config_headers_embed(), view=ConfigBackView(self.user_id))


def make_config_main_embed() -> discord.Embed:
    embed = discord.Embed(title="⚙️ Настройки бота", description="Выберите категорию настроек ниже.", color=EMBED_COLOR)
    # Вертикальная компоновка (каждое поле отдельно / inline=False)
    embed.add_field(name="⚙️ General", value="Цвет и footer", inline=False)
    embed.add_field(name="🎟️ Тикеты", value="Канал логов и логируемые команды", inline=False)
    embed.add_field(name="🛡️ Доступ", value="Группы, роли и команды", inline=False)
    embed.add_field(name="🏆 Лидерборды", value="Горизонтально или вертикально", inline=False)
    embed.add_field(name="📊 Итоги", value="Каналы считалки и bump", inline=False)
    embed.add_field(name="🖼️ Header-изображения", value="Ссылки задаются в переменной HEADER_IMAGES в коде.", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


class ConfigBackView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_main_embed(), view=ConfigMainView(self.user_id))


class ConfigGeneralModal(discord.ui.Modal, title="Настройки General"):
    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id
        hex_color = f"#{CONFIG.get('embed_color', 0x212121):06X}"
        self.color_input = discord.ui.TextInput(label="Цвет Embed (Hex, например #212121)", default=hex_color, max_length=7)
        self.footer_input = discord.ui.TextInput(label="Текст Footer", default=CONFIG.get("footer_text", FOOTER_TEXT), max_length=100)
        self.add_item(self.color_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        color_raw = self.color_input.value.strip().lstrip("#")
        try:
            color_int = int(color_raw, 16)
        except ValueError:
            await send_error_interaction(interaction, "Некорректный Hex-код цвета.", ephemeral=True)
            return

        update_config({"embed_color": color_int, "footer_text": self.footer_input.value.strip()})
        await interaction.response.edit_message(embed=make_config_main_embed(), view=ConfigMainView(self.user_id))


def make_config_tickets_embed() -> discord.Embed:
    embed = discord.Embed(title="🎟️ Настройки тикетов и логов", color=EMBED_COLOR)
    cid = CONFIG.get("log_channel_id")
    embed.add_field(name="Канал логов", value=f"<#{cid}>" if cid else "Не установлен", inline=False)
    toggles = CONFIG.get("log_toggles", {})
    t_lines = [f"• `{cmd}`: **{'ВКЛ' if toggles.get(cmd, False) else 'ВЫКЛ'}**" for cmd in LOGGABLE_COMMANDS_DEFAULT.keys()]
    embed.add_field(name="Логируемые команды", value="\n".join(t_lines), inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


class ConfigTicketsView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Канал логов", emoji="📢", style=discord.ButtonStyle.primary)
    async def set_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_tickets_embed(), view=ConfigLogChannelSelectView(self.user_id))

    @discord.ui.button(label="Логируемые команды", emoji="⚙️", style=discord.ButtonStyle.primary)
    async def set_toggles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_tickets_embed(), view=ConfigLogTogglesSelectView(self.user_id))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_main_embed(), view=ConfigMainView(self.user_id))


class ConfigLogChannelSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Выберите канал логов...")
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        ch = select.values[0]
        update_config({"log_channel_id": ch.id})
        await interaction.response.edit_message(embed=make_config_tickets_embed(), view=ConfigTicketsView(self.user_id))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_tickets_embed(), view=ConfigTicketsView(self.user_id))


class ConfigLogTogglesSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

        options = []
        toggles = CONFIG.get("log_toggles", {})
        for cmd in LOGGABLE_COMMANDS_DEFAULT.keys():
            status = "ВКЛ" if toggles.get(cmd, False) else "ВЫКЛ"
            options.append(discord.SelectOption(label=cmd, description=f"Текущий статус: {status}", value=cmd))

        select = discord.ui.Select(placeholder="Выберите команду для переключения...", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        cmd = interaction.data["values"][0]
        toggles = CONFIG.get("log_toggles", {})
        current = toggles.get(cmd, False)
        update_config({f"log_toggles.{cmd}": not current})
        await interaction.response.edit_message(embed=make_config_tickets_embed(), view=ConfigLogTogglesSelectView(self.user_id))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_tickets_embed(), view=ConfigTicketsView(self.user_id))


def make_config_access_embed() -> discord.Embed:
    embed = discord.Embed(title="🛡️ Настройки доступа", color=EMBED_COLOR)
    groups = CONFIG.get("permission_groups", {})
    for gkey, gdata in groups.items():
        roles = ", ".join(f"<@&{rid}>" for rid in gdata.get("roles", [])) or "Нет ролей"
        cmds = ", ".join(f"`{c}`" for c in gdata.get("commands", [])) or "Нет команд"
        embed.add_field(
            name=f"{gdata.get('emoji', '🛡️')} {gdata.get('name', gkey)} (`{gkey}`)",
            value=f"**Роли:** {roles}\n**Команды:** {cmds}",
            inline=False,
        )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


class ConfigAccessGroupSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

        options = []
        for gkey, gdata in CONFIG.get("permission_groups", {}).items():
            options.append(discord.SelectOption(label=gdata.get("name", gkey), value=gkey, emoji=gdata.get("emoji")))
        select = discord.ui.Select(placeholder="Выберите группу для настройки...", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        gkey = interaction.data["values"][0]
        await interaction.response.edit_message(embed=make_config_access_group_embed(gkey), view=ConfigAccessGroupEditView(self.user_id, gkey))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_main_embed(), view=ConfigMainView(self.user_id))


def make_config_access_group_embed(gkey: str) -> discord.Embed:
    gdata = CONFIG.get("permission_groups", {}).get(gkey, {})
    embed = discord.Embed(title=f"Группа: {gdata.get('name', gkey)}", color=EMBED_COLOR)
    roles = ", ".join(f"<@&{rid}>" for rid in gdata.get("roles", [])) or "Нет ролей"
    cmds = ", ".join(f"`{c}`" for c in gdata.get("commands", [])) or "Нет команд"
    embed.add_field(name="Роли", value=roles, inline=False)
    embed.add_field(name="Команды", value=cmds, inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


class ConfigAccessGroupEditView(discord.ui.View):
    def __init__(self, user_id: int, gkey: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.gkey = gkey

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Изменить роли", emoji="🎭", style=discord.ButtonStyle.primary)
    async def edit_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_access_group_embed(self.gkey), view=ConfigAccessRoleSelectView(self.user_id, self.gkey))

    @discord.ui.button(label="Изменить команды", emoji="📜", style=discord.ButtonStyle.primary)
    async def edit_commands(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_access_group_embed(self.gkey), view=ConfigAccessCommandSelectView(self.user_id, self.gkey))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_access_embed(), view=ConfigAccessGroupSelectView(self.user_id))


class ConfigAccessRoleSelectView(discord.ui.View):
    def __init__(self, user_id: int, gkey: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.gkey = gkey

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Выберите роли для этой группы...", min_values=0, max_values=10)
    async def select_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role_ids = [role.id for role in select.values]
        update_config({f"permission_groups.{self.gkey}.roles": role_ids})
        await interaction.response.edit_message(embed=make_config_access_group_embed(self.gkey), view=ConfigAccessGroupEditView(self.user_id, self.gkey))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_access_group_embed(self.gkey), view=ConfigAccessGroupEditView(self.user_id, self.gkey))


class ConfigAccessCommandSelectView(discord.ui.View):
    def __init__(self, user_id: int, gkey: str):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.gkey = gkey

        all_cmds = list(COMMAND_LABELS.keys())
        current_cmds = CONFIG.get("permission_groups", {}).get(gkey, {}).get("commands", [])

        options = [
            discord.SelectOption(
                label=COMMAND_LABELS[c],
                value=c,
                default=(c in current_cmds),
            )
            for c in all_cmds
        ]

        select = discord.ui.Select(
            placeholder="Выберите команды для группы...",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        selected = interaction.data.get("values", [])
        update_config({f"permission_groups.{self.gkey}.commands": selected})
        await interaction.response.edit_message(embed=make_config_access_group_embed(self.gkey), view=ConfigAccessGroupEditView(self.user_id, self.gkey))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_access_group_embed(self.gkey), view=ConfigAccessGroupEditView(self.user_id, self.gkey))


def make_config_leaderboard_embed() -> discord.Embed:
    embed = discord.Embed(title="🏆 Настройки лидерборда", color=EMBED_COLOR)
    mode = CONFIG.get("leaderboard_layout", "horizontal")
    embed.add_field(name="Режим отображения", value="Горизонтально (в ряд)" if mode == "horizontal" else "Вертикально (в столбец)", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


class ConfigLeaderboardView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Горизонтально", style=discord.ButtonStyle.primary)
    async def set_horizontal(self, interaction: discord.Interaction, button: discord.ui.Button):
        update_config({"leaderboard_layout": "horizontal"})
        await interaction.response.edit_message(embed=make_config_leaderboard_embed(), view=self)

    @discord.ui.button(label="Вертикально", style=discord.ButtonStyle.primary)
    async def set_vertical(self, interaction: discord.Interaction, button: discord.ui.Button):
        update_config({"leaderboard_layout": "vertical"})
        await interaction.response.edit_message(embed=make_config_leaderboard_embed(), view=self)

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_main_embed(), view=ConfigMainView(self.user_id))


def make_config_results_embed() -> discord.Embed:
    embed = discord.Embed(title="📊 Настройки итогов", color=EMBED_COLOR)
    cnt = CONFIG.get("counting_channel_id")
    bmp = CONFIG.get("bump_channel_id")
    embed.add_field(name="Канал считалки", value=f"<#{cnt}>" if cnt else "Не установлен", inline=False)
    embed.add_field(name="Канал bump", value=f"<#{bmp}>" if bmp else "Не установлен", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


class ConfigResultsView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Канал считалки", emoji="💬", style=discord.ButtonStyle.primary)
    async def set_count_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_results_embed(), view=ConfigResultsCountChannelSelectView(self.user_id))

    @discord.ui.button(label="Канал bump", emoji="🚀", style=discord.ButtonStyle.primary)
    async def set_bump_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_results_embed(), view=ConfigResultsBumpChannelSelectView(self.user_id))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_main_embed(), view=ConfigMainView(self.user_id))


class ConfigResultsCountChannelSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Выберите канал считалки...")
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        ch = select.values[0]
        update_config({"counting_channel_id": ch.id})
        await interaction.response.edit_message(embed=make_config_results_embed(), view=ConfigResultsView(self.user_id))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_results_embed(), view=ConfigResultsView(self.user_id))


class ConfigResultsBumpChannelSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Выберите канал bump...")
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        ch = select.values[0]
        update_config({"bump_channel_id": ch.id})
        await interaction.response.edit_message(embed=make_config_results_embed(), view=ConfigResultsView(self.user_id))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_config_results_embed(), view=ConfigResultsView(self.user_id))


def make_config_headers_embed() -> discord.Embed:
    embed = discord.Embed(title="🖼️ Header-изображения", color=EMBED_COLOR)
    lines = [f"• `{k}`: {v if v else '*не задано*'}" for k, v in HEADER_IMAGES.items()]
    embed.description = "Ссылки на шапки редактируются только в файле бота (`HEADER_IMAGES`):\n\n" + "\n".join(lines)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


# 2. МЕНЮ ХЕЛП И КНОПКИ КАТЕГОРИЙ
def make_help_main_embed(user: discord.Member | discord.User, channel_id: int) -> discord.Embed:
    group_name = get_user_group_name(user, channel_id)
    embed = discord.Embed(
        title="☘️ Меню команд бота",
        description=(
            "Выберите категорию команд ниже с помощью кнопок.\n\n"
            "**Общие команды:**\n"
            "• `.help` — Вызвать данное справочное меню\n"
            "• `.config` — Настройки бота (доступно владельцу)"
        ),
        color=EMBED_COLOR
    )
    embed.set_footer(text=f"Ваша текущая группа: {group_name} • {FOOTER_TEXT}")
    return embed

def make_help_category_embed(user: discord.Member | discord.User, channel_id: int, category: str) -> discord.Embed:
    group_name = get_user_group_name(user, channel_id)
    embed = discord.Embed(color=EMBED_COLOR)
    
    if category == "tickets":
        embed.title = "🎟️ Команды — Тикеты"
        embed.description = (
            "**Список команд для работы с тикетами:**\n\n"
            "• `.addticket <@модератор> <ссылка> <категория>` — Добавить лог тикета в базу\n"
            "• `.deleteticket <№ тикета> <ссылка>` — Зафиксировать удаление канала тикета\n"
            "• `.ticketstats [@пользователь]` — Посмотреть статистику тикетов\n"
            "• `.ticketlogs [@пользователь]` — Посмотреть историю тикетов с пагинацией\n"
            "• `.leaderboard` — Посмотреть топ модераторов по тикетам\n"
            "• `.deletelog <№ тикета>` — Удалить запись тикета из базы\n"
            "• `.resetlogs <@пользователь>` — Сбросить всю статистику пользователя"
        )
    elif category == "results":
        embed.title = "🏆 Команды — Итоги"
        embed.description = (
            "**Список команд статистики:**\n\n"
            "• `.sum` / `.summaries` — Посмотреть топ участников по сообщениям в считалке и bump"
        )
    elif category == "config":
        embed.title = "⚙️ Команды — Конфиг"
        embed.description = (
            "**Настройка бота:**\n\n"
            "• `.config` — Открыть интерактивную панель управления конфигурацией бота"
        )

    embed.set_footer(text=f"Ваша текущая группа: {group_name} • {FOOTER_TEXT}")
    return embed


class HelpMainView(discord.ui.View):
    def __init__(self, user_id: int, channel_id: int, user: discord.Member | discord.User):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.channel_id = channel_id
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Тикеты", emoji="🎟️", style=discord.ButtonStyle.primary)
    async def btn_tickets(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=make_help_category_embed(self.user, self.channel_id, "tickets"),
            view=HelpCategoryView(self.user_id, self.channel_id, self.user)
        )

    @discord.ui.button(label="Итоги", emoji="🏆", style=discord.ButtonStyle.primary)
    async def btn_results(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=make_help_category_embed(self.user, self.channel_id, "results"),
            view=HelpCategoryView(self.user_id, self.channel_id, self.user)
        )

    @discord.ui.button(label="Конфиг", emoji="⚙️", style=discord.ButtonStyle.secondary)
    async def btn_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=make_help_category_embed(self.user, self.channel_id, "config"),
            view=HelpCategoryView(self.user_id, self.channel_id, self.user)
        )


class HelpCategoryView(discord.ui.View):
    def __init__(self, user_id: int, channel_id: int, user: discord.Member | discord.User):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.channel_id = channel_id
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await send_error_interaction(interaction, "Вы не можете управлять этим меню.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=make_help_main_embed(self.user, self.channel_id),
            view=HelpMainView(self.user_id, self.channel_id, self.user)
        )


# ================= КОМАНДЫ БОТА =================

# 1. ПЕРЕИМЕНОВАНИЕ В .sum И .summaries
@bot.command(name="sum", aliases=["summaries"])
@check_access_decorator(command_name="summaries")
async def cmd_summaries(ctx: commands.Context):
    embed = process_results()
    await send_embed_with_header(ctx, embed, "results")

@bot.tree.command(name="summaries", description="Посмотреть статистику считалки и bump-канала")
@check_access_decorator(is_slash=True, command_name="summaries")
async def slash_summaries(interaction: discord.Interaction):
    embed = process_results()
    await send_embed_with_header(interaction, embed, "results")


@bot.command(name="help")
@check_access_decorator(command_name="help")
async def cmd_help(ctx: commands.Context):
    embed = make_help_main_embed(ctx.author, ctx.channel.id)
    view = HelpMainView(ctx.author.id, ctx.channel.id, ctx.author)
    await send_embed_with_header(ctx, embed, "help", view=view)

@bot.tree.command(name="help", description="Открыть меню команд")
@check_access_decorator(is_slash=True, command_name="help")
async def slash_help(interaction: discord.Interaction):
    embed = make_help_main_embed(interaction.user, interaction.channel_id)
    view = HelpMainView(interaction.user.id, interaction.channel_id, interaction.user)
    await send_embed_with_header(interaction, embed, "help", view=view)


@bot.command(name="config")
async def cmd_config(ctx: commands.Context):
    if not is_owner_user(ctx.author):
        await send_error_ctx(ctx, "Команда конфигурации доступна только владельцу бота.")
        return
    embed = make_config_main_embed()
    view = ConfigMainView(ctx.author.id)
    await send_embed_with_header(ctx, embed, "config", view=view)

@bot.tree.command(name="config", description="Открыть настройки бота (только для владельца)")
async def slash_config(interaction: discord.Interaction):
    if not is_owner_user(interaction.user):
        await send_error_interaction(interaction, "Команда конфигурации доступна только владельцу бота.")
        return
    embed = make_config_main_embed()
    view = ConfigMainView(interaction.user.id)
    await send_embed_with_header(interaction, embed, "config", view=view)


@bot.command(name="addticket")
@check_access_decorator(command_name="addticket")
async def cmd_addticket(ctx: commands.Context, staff: discord.User, transcript_url: str, *, category: str):
    if not is_valid_addticket(transcript_url, category):
        valid_cats = ", ".join(f"`{c}`" for c in VALID_CATEGORIES)
        await send_error_ctx(
            ctx,
            f"Неверные данные.\n• Ссылка должна содержать `https://discord.com/`\n• Категория должна быть одной из: {valid_cats}",
        )
        return

    if is_transcript_exists(transcript_url):
        await send_error_ctx(ctx, "Этот транскрипт уже добавлен в базу данных.")
        return

    embed, ticket_id = process_add_ticket(ctx.author, staff, transcript_url, category)
    await ctx.send(embed=embed)
    log_embed = build_log_embed(
        "🎟️ Добавлен тикет",
        [
            f"**Номер лога:** №{ticket_id}",
            f"**Кто внёс:** {ctx.author.mention} (`{ctx.author.id}`)",
            f"**Модератор:** {staff.mention} (`{staff.id}`)",
            f"**Категория:** {category}",
            f"**Транскрипт:** {transcript_url}",
        ]
    )
    await send_log("addticket", log_embed)

@bot.tree.command(name="addticket", description="Добавить лог тикета в базу")
@app_commands.describe(staff="Кто вёл тикет", transcript_url="Ссылка на транскрипт", category="Категория тикета")
@app_commands.choices(category=[app_commands.Choice(name=cat, value=cat) for cat in VALID_CATEGORIES])
@check_access_decorator(is_slash=True, command_name="addticket")
async def slash_addticket(interaction: discord.Interaction, staff: discord.User, transcript_url: str, category: app_commands.Choice[str]):
    cat_val = category.value
    if not is_valid_addticket(transcript_url, cat_val):
        valid_cats = ", ".join(f"`{c}`" for c in VALID_CATEGORIES)
        await send_error_interaction(
            interaction,
            f"Неверные данные.\n• Ссылка должна содержать `https://discord.com/`\n• Категория должна быть одной из: {valid_cats}",
        )
        return

    if is_transcript_exists(transcript_url):
        await send_error_interaction(interaction, "Этот транскрипт уже добавлен в базу данных.")
        return

    embed, ticket_id = process_add_ticket(interaction.user, staff, transcript_url, cat_val)
    await interaction.response.send_message(embed=embed)
    log_embed = build_log_embed(
        "🎟️ Добавлен тикет",
        [
            f"**Номер лога:** №{ticket_id}",
            f"**Кто внёс:** {interaction.user.mention} (`{interaction.user.id}`)",
            f"**Модератор:** {staff.mention} (`{staff.id}`)",
            f"**Категория:** {cat_val}",
            f"**Транскрипт:** {transcript_url}",
        ]
    )
    await send_log("addticket", log_embed)


@bot.command(name="deleteticket")
@check_access_decorator(command_name="deleteticket")
async def cmd_deleteticket(ctx: commands.Context, log_id: int, transcript_url: str):
    if not is_valid_transcript_link(transcript_url):
        await send_error_ctx(ctx, "Ссылка должна быть корректной ссылкой Discord (содержать `https://discord.com/`).")
        return

    embed, err = process_delete_ticket_channel(ctx.author, log_id, transcript_url)
    if err == "not_found":
        await send_error_ctx(ctx, f"Тикет с номером **№{log_id}** не найден в базе данных.")
        return
    if err == "duplicate":
        await send_error_ctx(ctx, "Удаление для этого транскрипта уже зарегистрировано.")
        return

    await ctx.send(embed=embed)
    log_embed = build_log_embed(
        "🗑️ Удалён тикет",
        [
            f"**Номер лога:** №{log_id}",
            f"**Кто зафиксировал:** {ctx.author.mention} (`{ctx.author.id}`)",
            f"**Транскрипт:** {transcript_url}",
        ]
    )
    await send_log("deleteticket", log_embed)

@bot.tree.command(name="deleteticket", description="Зафиксировать удаление канала тикета")
@app_commands.describe(log_id="Номер тикета в базе", transcript_url="Ссылка на транскрипт")
@check_access_decorator(is_slash=True, command_name="deleteticket")
async def slash_deleteticket(interaction: discord.Interaction, log_id: int, transcript_url: str):
    if not is_valid_transcript_link(transcript_url):
        await send_error_interaction(interaction, "Ссылка должна быть корректной ссылкой Discord (содержать `https://discord.com/`).")
        return

    embed, err = process_delete_ticket_channel(interaction.user, log_id, transcript_url)
    if err == "not_found":
        await send_error_interaction(interaction, f"Тикет с номером **№{log_id}** не найден в базе данных.")
        return
    if err == "duplicate":
        await send_error_interaction(interaction, "Удаление для этого транскрипта уже зарегистрировано.")
        return

    await interaction.response.send_message(embed=embed)
    log_embed = build_log_embed(
        "🗑️ Удалён тикет",
        [
            f"**Номер лога:** №{log_id}",
            f"**Кто зафиксировал:** {interaction.user.mention} (`{interaction.user.id}`)",
            f"**Транскрипт:** {transcript_url}",
        ]
    )
    await send_log("deleteticket", log_embed)


@bot.command(name="deletelog")
@check_access_decorator(command_name="deletelog")
async def cmd_deletelog(ctx: commands.Context, log_id: int):
    doc = delete_ticket_log(log_id)
    if not doc:
        await send_error_ctx(ctx, f"Лог с номером **№{log_id}** не найден.")
        return
    await ctx.send(embed=make_status_embed("Запись удалена", f"Запись тикета **№{log_id}** успешно удалена из базы.", "success"))
    log_embed = build_log_embed(
        "🗑️ Удалена запись тикета",
        [
            f"**Номер записи:** №{log_id}",
            f"**Удалил:** {ctx.author.mention} (`{ctx.author.id}`)",
        ]
    )
    await send_log("deletelog", log_embed)

@bot.tree.command(name="deletelog", description="Удалить запись лога тикета из базы")
@app_commands.describe(log_id="Номер записи для удаления")
@check_access_decorator(is_slash=True, command_name="deletelog")
async def slash_deletelog(interaction: discord.Interaction, log_id: int):
    doc = delete_ticket_log(log_id)
    if not doc:
        await send_error_interaction(interaction, f"Лог с номером **№{log_id}** не найден.")
        return
    await interaction.response.send_message(embed=make_status_embed("Запись удалена", f"Запись тикета **№{log_id}** успешно удалена из базы.", "success"))
    log_embed = build_log_embed(
        "🗑️ Удалена запись тикета",
        [
            f"**Номер записи:** №{log_id}",
            f"**Удалил:** {interaction.user.mention} (`{interaction.user.id}`)",
        ]
    )
    await send_log("deletelog", log_embed)


@bot.command(name="resetlogs")
@check_access_decorator(command_name="resetlogs")
async def cmd_resetlogs(ctx: commands.Context, target: discord.User):
    cnt = reset_tickets(target.id)
    await ctx.send(embed=make_status_embed("Сброс выполнен", f"Очищено записей для {target.mention}: **{cnt}**", "success"))
    log_embed = build_log_embed(
        "⚠️ Сброс логов пользователя",
        [
            f"**Пользователь:** {target.mention} (`{target.id}`)",
            f"**Удалено записей:** {cnt}",
            f"**Сбросил:** {ctx.author.mention} (`{ctx.author.id}`)",
        ]
    )
    await send_log("resetlogs", log_embed)

@bot.tree.command(name="resetlogs", description="Сбросить всю статистику конкретного пользователя")
@app_commands.describe(target="Пользователь, чью статистику нужно сбросить")
@check_access_decorator(is_slash=True, command_name="resetlogs")
async def slash_resetlogs(interaction: discord.Interaction, target: discord.User):
    cnt = reset_tickets(target.id)
    await interaction.response.send_message(embed=make_status_embed("Сброс выполнен", f"Очищено записей для {target.mention}: **{cnt}**", "success"))
    log_embed = build_log_embed(
        "⚠️ Сброс логов пользователя",
        [
            f"**Пользователь:** {target.mention} (`{target.id}`)",
            f"**Удалено записей:** {cnt}",
            f"**Сбросил:** {interaction.user.mention} (`{interaction.user.id}`)",
        ]
    )
    await send_log("resetlogs", log_embed)


@bot.command(name="ticketlogs")
@check_access_decorator(command_name="ticketlogs")
async def cmd_ticketlogs(ctx: commands.Context, target: discord.User | None = None):
    target_user = target or ctx.author
    embed, total_pages = process_ticket_logs(target_user, page=1)

    if total_pages > 1:
        view = TicketLogsPaginationView(ctx.author.id, target_user, total_pages, initial_page=1)
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg
    else:
        await ctx.send(embed=embed)

@bot.tree.command(name="ticketlogs", description="Посмотреть историю тикетов с пагинацией")
@app_commands.describe(target="Пользователь, чью историю тикетов нужно посмотреть")
@check_access_decorator(is_slash=True, command_name="ticketlogs")
async def slash_ticketlogs(interaction: discord.Interaction, target: discord.User | None = None):
    target_user = target or interaction.user
    embed, total_pages = process_ticket_logs(target_user, page=1)

    if total_pages > 1:
        view = TicketLogsPaginationView(interaction.user.id, target_user, total_pages, initial_page=1)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()
    else:
        await interaction.response.send_message(embed=embed)


@bot.command(name="ticketstats")
@check_access_decorator(command_name="ticketstats")
async def cmd_ticketstats(ctx: commands.Context, target: discord.User | None = None):
    target_user = target or ctx.author
    embed = process_ticket_stats(target_user)
    await ctx.send(embed=embed)

@bot.tree.command(name="ticketstats", description="Посмотреть статистику тикетов модератора")
@app_commands.describe(target="Модератор, чью статистику нужно узнать")
@check_access_decorator(is_slash=True, command_name="ticketstats")
async def slash_ticketstats(interaction: discord.Interaction, target: discord.User | None = None):
    target_user = target or interaction.user
    embed = process_ticket_stats(target_user)
    await interaction.response.send_message(embed=embed)


@bot.command(name="leaderboard")
@check_access_decorator(command_name="leaderboard")
async def cmd_leaderboard(ctx: commands.Context):
    embed = process_leaderboard()
    await send_embed_with_header(ctx, embed, "leaderboard")

@bot.tree.command(name="leaderboard", description="Посмотреть лидерборд по тикетам")
@check_access_decorator(is_slash=True, command_name="leaderboard")
async def slash_leaderboard(interaction: discord.Interaction):
    embed = process_leaderboard()
    await send_embed_with_header(interaction, embed, "leaderboard")


# ================= ЗАПУСК БОТА =================

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("Ошибка: DISCORD_TOKEN не найден в файле .env или Variables Railway")

bot.run(TOKEN)
