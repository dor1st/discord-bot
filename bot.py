import os
import math
import sqlite3
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ================= НАСТРОЙКИ БАЗЫ ДАННЫХ И БОТА =================

conn = sqlite3.connect("tickets.db")
cursor = conn.cursor()

# Создаём таблицу с колонкой author_id
cursor.execute("""
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL DEFAULT 0,
    transcript_url TEXT UNIQUE,
    category TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

# Автоматическая миграция БД
cursor.execute("PRAGMA table_info(tickets)")
columns = [column[1] for column in cursor.fetchall()]
if "author_id" not in columns:
    cursor.execute("ALTER TABLE tickets ADD COLUMN author_id INTEGER NOT NULL DEFAULT 0")
    conn.commit()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

EMBED_COLOR = discord.Color(0x212121)
FOOTER_TEXT = "ТУСОВКА ДОРИСТА"

# ================= НАСТРОЙКА РОЛЕЙ И КАНАЛОВ =================

# Единый список разрешённых каналов для обычных пользователей
ALLOWED_CHANNEL_IDS = [1322968592202993746, 1537220150267220018]

# Настройки ролей
SUPPORT_ROLE_IDS = [1502684875868737796, 1322962317885046844]
TRANSCRIPT_ROLE_IDS = [1502684875868737796, 1322962317885046844]
ADMIN_ROLE_IDS = [1502684875868737796, 1322962317885046844]

VALID_CATEGORIES = ["Помощь по серверу", "Получение призов", "Получение роли", "Покупка рекламы"]

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ПРОВЕРКИ =================

def has_role_access(user: discord.Member | discord.User, role_ids: list[int]) -> bool:
    """Проверяет права Администратора или наличие хотя бы одной роли."""
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    return any(role.id in role_ids for role in user.roles)

def is_admin_user(user: discord.Member | discord.User) -> bool:
    return has_role_access(user, ADMIN_ROLE_IDS)

def check_access(user: discord.Member | discord.User, channel_id: int, role_ids: list[int]) -> tuple[bool, str]:
    """Универсальная проверка: Админам можно везде, остальным — по ролям и разрешённым каналам."""
    if not isinstance(user, discord.Member):
        return False, "Команды работают только на сервере."

    # Администраторам разрешено всё и везде
    if user.guild_permissions.administrator:
        return True, ""

    if not has_role_access(user, role_ids):
        return False, "У вас недостаточно ролей для использования этой команды."

    if ALLOWED_CHANNEL_IDS and channel_id not in ALLOWED_CHANNEL_IDS:
        channels_mention = ", ".join([f"<#{cid}>" for cid in ALLOWED_CHANNEL_IDS])
        return False, f"Эта команда доступна только в каналах: {channels_mention}"

    return True, ""

def get_user_group_name(user: discord.Member | discord.User, channel_id: int) -> str:
    """Определяет группу пользователя без лишних вызовов проверок."""
    if is_admin_user(user):
        return "Администрация"
    if has_role_access(user, TRANSCRIPT_ROLE_IDS):
        return "Transcript"
    if has_role_access(user, SUPPORT_ROLE_IDS):
        return "Support"
    return "Пользователь"

# --- Декораторы проверок ---

def check_access_decorator(role_ids: list[int], is_slash: bool = False):
    async def predicate(target):
        user = target.user if is_slash else target.author
        channel_id = target.channel_id if is_slash else target.channel.id
        ok, msg = check_access(user, channel_id, role_ids)
        if not ok:
            raise app_commands.AppCommandError(msg) if is_slash else commands.CheckFailure(msg)
        return True
    return app_commands.check(predicate) if is_slash else commands.check(predicate)

def check_support_prefix(): return check_access_decorator(SUPPORT_ROLE_IDS)
def check_transcript_prefix(): return check_access_decorator(TRANSCRIPT_ROLE_IDS)
def check_admin_prefix(): return check_access_decorator(ADMIN_ROLE_IDS)

def check_support_slash(): return check_access_decorator(SUPPORT_ROLE_IDS, is_slash=True)
def check_transcript_slash(): return check_access_decorator(TRANSCRIPT_ROLE_IDS, is_slash=True)
def check_admin_slash(): return check_access_decorator(ADMIN_ROLE_IDS, is_slash=True)

def is_valid_addticket(transcript_url: str, category: str) -> bool:
    return "https://discord.com/" in transcript_url and category in VALID_CATEGORIES

def is_transcript_exists(transcript_url: str) -> bool:
    cursor.execute("SELECT 1 FROM tickets WHERE transcript_url = ?", (transcript_url,))
    return cursor.fetchone() is not None

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot logged in as {bot.user}")

# ================= ОБРАБОТКА ОШИБОК =================

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(f"<:bruh:1521904409582375174> {error}")
        return
    raise error

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandNotFound):
        return
    msg = f"<:bruh:1521904409582375174> {error}" if str(error) else "<:bruh:1521904409582375174> Произошла ошибка при выполнении команды."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

# ================= БИЗНЕС-ЛОГИКА ТИКЕТОВ =================

def get_monthly_tickets(staff_id: int) -> int:
    date_30_days = datetime.now() - timedelta(days=30)
    cursor.execute("SELECT COUNT(*) FROM tickets WHERE staff_id = ? AND created_at >= ?", (staff_id, date_30_days))
    return cursor.fetchone()[0]

def process_add_ticket(author_user: discord.User, staff_user: discord.User, transcript_url: str, category: str):
    cursor.execute(
        "INSERT INTO tickets (staff_id, author_id, transcript_url, category) VALUES (?, ?, ?, ?)",
        (staff_user.id, author_user.id, transcript_url, category),
    )
    conn.commit()

    monthly_count = get_monthly_tickets(staff_user.id)
    discord_timestamp = f"<t:{int(datetime.now().timestamp())}:F>"

    embed = discord.Embed(title=f"<:logs:1522340749998428160> Лог тикета — {staff_user.display_name}", color=EMBED_COLOR)
    embed.add_field(name="Дата транскрипта", value=discord_timestamp, inline=False)
    embed.add_field(name="Ссылка на транскрипт", value=transcript_url, inline=False)
    embed.add_field(name="Кто вёл тикет", value=str(staff_user.id), inline=False)
    embed.add_field(name="Внёс в базу", value=author_user.mention, inline=False)
    embed.add_field(name="Тикетов за последний месяц", value=str(monthly_count), inline=False)
    embed.add_field(name="Категория", value=category, inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def process_ticket_logs(target_user: discord.User, page: int = 1):
    cursor.execute("SELECT id, transcript_url, category, created_at FROM tickets WHERE staff_id = ? ORDER BY id ASC", (target_user.id,))
    logs = cursor.fetchall()

    if not logs:
        embed = discord.Embed(
            title=f"Тикеты — {target_user.name}",
            description="<:bruh:1521904409582375174> У этого модератора нет ни одного тикета.",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Страница 1/1 (0 логов) • {FOOTER_TEXT}")
        return embed

    items_per_page = 5
    total_logs = len(logs)
    total_pages = math.ceil(total_logs / items_per_page)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * items_per_page
    current_logs = logs[start_idx:start_idx + items_per_page]

    embed = discord.Embed(title=f"<:ticket:1522343287816716379> Тикеты — {target_user.name}", color=EMBED_COLOR)
    embed.description = f"{target_user.id}\n" + "—" * 28

    lines = []
    for log_id, transcript_url, category, created_at in current_logs:
        try:
            dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
            formatted_date = f"<t:{int(dt.timestamp())}:F>"
        except (ValueError, TypeError):
            formatted_date = created_at

        lines.append(
            f"**Тикет №{log_id}**\n"
            f"**Модератор:** {target_user.name} ({target_user.mention})\n"
            f"**Категория:** {category}\n"
            f"**Транскрипт:** {transcript_url}\n"
            f"{formatted_date}"
        )

    embed.description += "\n\n" + "\n\n".join(lines)
    embed.set_footer(text=f"Страница {current_page}/{total_pages} ({total_logs} логов) • {FOOTER_TEXT}")
    return embed

def process_ticket_stats(target_user: discord.User):
    now = datetime.now()
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    # Оптимизированный единый SQL-запрос для подсчета статистики
    cursor.execute("""
        SELECT 
            SUM(CASE WHEN staff_id = ? AND created_at >= ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN staff_id = ? AND created_at >= ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN staff_id = ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN author_id = ? AND created_at >= ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN author_id = ? AND created_at >= ? THEN 1 ELSE 0 END),
            SUM(CASE WHEN author_id = ? THEN 1 ELSE 0 END)
        FROM tickets
    """, (target_user.id, d7, target_user.id, d30, target_user.id,
          target_user.id, d7, target_user.id, d30, target_user.id))
    
    c7_s, c30_s, call_s, c7_a, c30_a, call_a = [v or 0 for v in cursor.fetchone()]

    # Последний проведенный и внесенный тикеты
    cursor.execute("SELECT created_at FROM tickets WHERE staff_id = ? ORDER BY id DESC LIMIT 1", (target_user.id,))
    l_s = cursor.fetchone()
    cursor.execute("SELECT created_at FROM tickets WHERE author_id = ? ORDER BY id DESC LIMIT 1", (target_user.id,))
    l_a = cursor.fetchone()

    def fmt_last(row):
        if not row: return "—"
        try:
            return f"<t:{int(datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S').timestamp())}:R>"
        except (ValueError, TypeError):
            return str(row[0])

    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=target_user.name, icon_url=target_user.display_avatar.url)
    embed.title = "<:ticket:1522343287816716379> Статистика тикетов и транскриптов"
    embed.add_field(name="За последние 7 дней:", value=f"• Тикетов: **{c7_s}**\n• Транскриптов: **{c7_a}**", inline=True)
    embed.add_field(name="За последние 30 дней:", value=f"• Тикетов: **{c30_s}**\n• Транскриптов: **{c30_a}**", inline=True)
    embed.add_field(name="За всё время:", value=f"• Тикетов: **{call_s}**\n• Транскриптов: **{call_a}**", inline=True)
    embed.add_field(
        name="<:lighting:1522337543360872489> Активность:",
        value=f"• **Последний проведённый тикет:** {fmt_last(l_s)}\n• **Последний внесённый транскрипт:** {fmt_last(l_a)}",
        inline=False
    )
    embed.set_footer(text=f"ID: {target_user.id} • Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")
    return embed

def process_leaderboard():
    now = datetime.now()
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    def get_top(column: str, min_date=None, exclude_zero=False):
        where_clauses = []
        params = []
        if min_date:
            where_clauses.append("created_at >= ?")
            params.append(min_date)
        if exclude_zero:
            where_clauses.append(f"{column} != 0")
        
        where_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        cursor.execute(f"SELECT {column}, COUNT(*) as cnt FROM tickets {where_str} GROUP BY {column} ORDER BY cnt DESC LIMIT 5", params)
        return cursor.fetchall()

    def format_top(top_list, unit_label="тикетов"):
        if not top_list:
            return "— *Нет данных*"
        return "\n".join([f"`{idx}.` <@{u_id}> — **{count}** {unit_label}" for idx, (u_id, count) in enumerate(top_list, 1)])

    embed = discord.Embed(title="<:ticket:1522343287816716379> Лидерборд тикетов и транскриптов", color=EMBED_COLOR)
    
    embed.add_field(name="<:ticket:1522343287816716379> Проведено тикетов (7 дн.)", value=format_top(get_top("staff_id", d7)), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Внесёно транскриптов (7 дн.)", value=format_top(get_top("author_id", d7, True), "транскриптов"), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(name="<:ticket:1522343287816716379> Проведено тикетов (30 дн.)", value=format_top(get_top("staff_id", d30)), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Внесёно транскриптов (30 дн.)", value=format_top(get_top("author_id", d30, True), "транскриптов"), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(name="<:ticket:1522343287816716379> Проведено тикетов (Все время)", value=format_top(get_top("staff_id")), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Внесёно транскриптов (Все время)", value=format_top(get_top("author_id", exclude_zero=True), "транскриптов"), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.set_footer(text=f"Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")
    return embed

def delete_ticket(log_id: int):
    cursor.execute("SELECT id, staff_id FROM tickets WHERE id = ?", (log_id,))
    ticket = cursor.fetchone()
    if ticket:
        cursor.execute("DELETE FROM tickets WHERE id = ?", (log_id,))
        conn.commit()
    return ticket

def reset_tickets(staff_id: int) -> int:
    cursor.execute("SELECT COUNT(*) FROM tickets WHERE staff_id = ? OR author_id = ?", (staff_id, staff_id))
    count = cursor.fetchone()[0]
    cursor.execute("DELETE FROM tickets WHERE staff_id = ? OR author_id = ?", (staff_id, staff_id))
    conn.commit()
    return count

# ================= EMBEDS ПОМОЩИ =================

def get_help_embed(user: discord.Member | discord.User, channel_id: int):
    group_name = get_user_group_name(user, channel_id)

    embed = discord.Embed(
        title="<:staff:1522338131339251823> Список команд бота",
        description="Используй префикс `.` или слэш-команды `/`",
        color=EMBED_COLOR,
    )

    if has_role_access(user, SUPPORT_ROLE_IDS):
        embed.add_field(
            name="<:ticket:1522343287816716379> Команды Support",
            value=(
                "`.help` — Показать это меню с командами.\n\n"
                "`.ticketstats [ID / упоминание]`\n> *Посмотреть статистику тикетов и транскриптов.*\n\n"
                "`.leaderboard`\n> *Посмотреть топ модераторов по тикетам и транскриптам.*"
            ),
            inline=False,
        )

    if has_role_access(user, TRANSCRIPT_ROLE_IDS):
        embed.add_field(
            name="<:logs:1522340749998428160> Команды Transcript",
            value=(
                "`.addticket [ID модератора] [ссылка] [категория]`\n> *Записать новый обработанный тикет в базу данных.*\n\n"
                "`.ticketlogs [ID / упоминание] [страница]`\n> *Посмотреть логи тикетов модератора.*"
            ),
            inline=False,
        )

    if is_admin_user(user):
        embed.add_field(
            name="<:mod:1522343179205087363> Команды Администрации",
            value=(
                "`.deleteticket [ID лога]`\n> *Удалить конкретный тикет по ID лога.*\n\n"
                "`.resettickets [ID / упоминание]`\n> *Очистить абсолютно все логи модератора.*"
            ),
            inline=False,
        )

    embed.set_footer(text=f"Ваша текущая группа: {group_name} • {FOOTER_TEXT}")
    return embed

def get_addticket_usage_embed():
    categories_str = ", ".join([f"`{c}`" for c in VALID_CATEGORIES])
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: addticket", description="Добавить новый лог об обработанном тикете")
    embed.add_field(name="Кулдаун:", value="3 секунды (Для Администрации отсутствует)", inline=False)
    embed.add_field(
        name="Правила аргументов:",
        value=(
            "1. Ссылка должна содержать: `https://discord.com/`\n"
            "2. Вы **не можете** указать свой собственный ID\n"
            "3. Один и тот же транскрипт нельзя вносить дважды\n"
            f"4. Допустимые категории: {categories_str}"
        ),
        inline=False,
    )
    embed.add_field(name="Использование:", value="`.addticket [ID модератора] [ссылка на транскрипт] [категория]`", inline=False)
    embed.add_field(name="Пример:", value="`.addticket 851443344718430210 https://discord.com/channels/... Получение призов`", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_ticketstats_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: ticketstats", description="Посмотреть статистику тикетов и транскриптов модератора")
    embed.add_field(name="Использование:", value="`.ticketstats [упоминание / ID модератора]`", inline=False)
    embed.add_field(name="Примеры:", value="`.ts` — показать свою статистику\n`.ticketstats [ID модератора]` — показать статистику выбранного модератора", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_ticketlogs_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: ticketlogs", description="Посмотреть список обработанных тикетов модератора")
    embed.add_field(name="Использование:", value="`.ticketlogs [упоминание / ID модератора] [номер страницы]`", inline=False)
    embed.add_field(name="Примеры:", value="`.tl` — 1-я страница своих логов\n`.tl 2` — 2-я страница своих логов\n`.ticketlogs [ID] 1` — 1-я страница логов модератора", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_deleteticket_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: deleteticket 🔒", description="Удалить один тикет из базы данных (Только Администрация)")
    embed.add_field(name="Использование:", value="`.deleteticket [номер лога]`", inline=False)
    embed.add_field(name="Пример:", value="`.dt 15` — удалить лог под номером 15", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_resettickets_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: resettickets (алиасы: .rt) 🔒", description="Полностью очистить логи пользователя (Только Администрация)")
    embed.add_field(name="Использование:", value="`.resettickets [упоминание / ID пользователя]`", inline=False)
    embed.add_field(name="Пример:", value="`.resettickets [ID модератора]` — сбросить тикеты указанного модератора", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

# ================= СЛЭШ КОМАНДЫ =================

@bot.tree.command(name="help", description="Показать полный список команд бота")
@check_support_slash()
async def slash_help(interaction: discord.Interaction):
    embed = get_help_embed(interaction.user, interaction.channel_id)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="addticket", description="Записать новый обработанный тикет")
@app_commands.describe(staff="ID участника персонала", transcript="Ссылка на транскрипт", category="Категория тикета")
@app_commands.choices(category=[app_commands.Choice(name=cat, value=cat) for cat in VALID_CATEGORIES])
@check_transcript_slash()
@app_commands.checks.cooldown(1, 3.0, key=lambda i: i.user.id)
async def slash_add_ticket(interaction: discord.Interaction, staff: str, transcript: str, category: str):
    if not is_valid_addticket(transcript, category):
        await interaction.response.send_message(embed=get_addticket_usage_embed(), ephemeral=True)
        return

    try:
        staff_id = int(staff.strip("<@!>"))
    except ValueError:
        await interaction.response.send_message(f"<:bruh:1521904409582375174> Некорректный ID: `{staff}`.", ephemeral=True)
        return

    if staff_id == interaction.user.id:
        await interaction.response.send_message("<:bruh:1521904409582375174> Вы не можете занести тикет, который провели сами!", ephemeral=True)
        return

    if is_transcript_exists(transcript):
        await interaction.response.send_message("<:bruh:1521904409582375174> Этот транскрипт уже внесен", ephemeral=True)
        return

    try:
        staff_user = await bot.fetch_user(staff_id)
    except (discord.NotFound, discord.HTTPException):
        await interaction.response.send_message(f"<:bruh:1521904409582375174> Не удалось найти пользователя по ID `{staff}`.", ephemeral=True)
        return

    embed = process_add_ticket(interaction.user, staff_user, transcript, category)
    await interaction.response.send_message(embed=embed)

@slash_add_ticket.error
async def slash_add_ticket_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        if is_admin_user(interaction.user):
            return
        await interaction.response.send_message(
            f"<:zzz:1522341702852022412> Подождите ещё {error.retry_after:.1f} сек.", ephemeral=True
        )
        return

@bot.tree.command(name="ticketstats", description="Посмотреть статистику тикетов")
@app_commands.describe(staff="Участник персонала")
@check_support_slash()
async def slash_ticket_stats(interaction: discord.Interaction, staff: discord.User = None):
    embed = process_ticket_stats(staff or interaction.user)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ticketlogs", description="Посмотреть тикеты пользователя")
@app_commands.describe(staff="Участник персонала", page="Номер страницы")
@check_transcript_slash()
async def slash_ticket_logs(interaction: discord.Interaction, staff: discord.User = None, page: int = 1):
    embed = process_ticket_logs(staff or interaction.user, page)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Посмотреть топ модераторов по тикетам и транскриптам")
@check_support_slash()
async def slash_leaderboard(interaction: discord.Interaction):
    embed = process_leaderboard()
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="deleteticket", description="Удалить тикет по номеру лога (Только для Админов)")
@app_commands.describe(log_id="Номер лога")
@check_admin_slash()
async def slash_delete_ticket(interaction: discord.Interaction, log_id: int):
    ticket = delete_ticket(log_id)
    if not ticket:
        await interaction.response.send_message(f"<:bruh:1521904409582375174> Лог с номером `{log_id}` не найден.", ephemeral=True)
        return
    await interaction.response.send_message(f"<a:gif_verify:1522328481956888686> Лог `{log_id}` удалён.")

@bot.tree.command(name="resettickets", description="Удалить все логи модератора (Только для Админов)")
@app_commands.describe(staff="Модератор")
@check_admin_slash()
async def slash_reset_tickets(interaction: discord.Interaction, staff: discord.User):
    count = reset_tickets(staff.id)
    await interaction.response.send_message(f"<a:gif_verify:1522328481956888686> Удалено логов модератора **{staff.name}**: `{count}`.")

# ================= ПРЕФИКСНЫЕ КОМАНДЫ =================

@bot.command(name="help")
@check_support_prefix()
async def prefix_help(ctx: commands.Context):
    embed = get_help_embed(ctx.author, ctx.channel.id)
    await ctx.send(embed=embed)

@bot.command(name="addticket", aliases=["t", "ticket"])
@check_transcript_prefix()
@commands.cooldown(1, 3.0, commands.BucketType.user)
async def prefix_add_ticket(ctx: commands.Context, *, args: str = None):
    if not args:
        await ctx.send(embed=get_addticket_usage_embed())
        return

    parts = args.split(maxsplit=2)
    if len(parts) < 3:
        await ctx.send(embed=get_addticket_usage_embed())
        return

    staff_raw, transcript, category = parts[0], parts[1], parts[2]

    if not is_valid_addticket(transcript, category):
        await ctx.send(embed=get_addticket_usage_embed())
        return

    try:
        staff_id = int(staff_raw.strip("<@!>"))
    except ValueError:
        await ctx.send("<:bruh:1521904409582375174> Укажите корректный numeric ID модератора.")
        return

    if staff_id == ctx.author.id:
        await ctx.send("<:bruh:1521904409582375174> Вы не можете занести тикет, который провели сами!")
        return

    if is_transcript_exists(transcript):
        await ctx.send("<:bruh:1521904409582375174> Этот транскрипт уже внесен")
        return

    try:
        staff_user = await bot.fetch_user(staff_id)
    except (discord.NotFound, discord.HTTPException):
        await ctx.send(f"<:bruh:1521904409582375174> Не удалось найти пользователя по ID `{staff_raw}`.")
        return

    embed = process_add_ticket(ctx.author, staff_user, transcript, category)
    await ctx.send(embed=embed)

@prefix_add_ticket.error
async def prefix_add_ticket_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandOnCooldown):
        if is_admin_user(ctx.author):
            ctx.command.reset_cooldown(ctx)
            await ctx.reinvoke()
            return
        await ctx.send(f"<:zzz:1522341702852022412> Подождите ещё {error.retry_after:.1f} сек.")
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(f"<:bruh:1521904409582375174> {error}")
        return
    await ctx.send(embed=get_addticket_usage_embed())

@bot.command(name="ticketstats", aliases=["ts"])
@check_support_prefix()
async def prefix_ticket_stats(ctx: commands.Context, staff: discord.User = None):
    embed = process_ticket_stats(staff or ctx.author)
    await ctx.send(embed=embed)

@prefix_ticket_stats.error
async def prefix_ticket_stats_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_ticketstats_usage_embed())

@bot.command(name="ticketlogs", aliases=["tl", "tlogs"])
@check_transcript_prefix()
async def prefix_ticket_logs(ctx: commands.Context, staff: discord.User = None, page: int = 1):
    embed = process_ticket_logs(staff or ctx.author, page)
    await ctx.send(embed=embed)

@prefix_ticket_logs.error
async def prefix_ticket_logs_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_ticketlogs_usage_embed())

@bot.command(name="leaderboard", aliases=["lb", "top"])
@check_support_prefix()
async def prefix_leaderboard(ctx: commands.Context):
    embed = process_leaderboard()
    await ctx.send(embed=embed)

@bot.command(name="deleteticket", aliases=["dt"])
@check_admin_prefix()
async def prefix_delete_ticket(ctx: commands.Context, log_id: int = None):
    if log_id is None:
        await ctx.send(embed=get_deleteticket_usage_embed())
        return
    ticket = delete_ticket(log_id)
    if not ticket:
        await ctx.send(f"<:bruh:1521904409582375174> Лог под номером `{log_id}` не найден.")
        return
    await ctx.send(f"<a:gif_verify:1522328481956888686> Лог `{log_id}` удалён.")

@prefix_delete_ticket.error
async def prefix_delete_ticket_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_deleteticket_usage_embed())

@bot.command(name="resettickets", aliases=["rt"])
@check_admin_prefix()
async def prefix_reset_tickets(ctx: commands.Context, staff: discord.User = None):
    if staff is None:
        await ctx.send(embed=get_resettickets_usage_embed())
        return
    count = reset_tickets(staff.id)
    await ctx.send(f"<a:gif_verify:1522328481956888686> Удалено логов модератора **{staff.name}**: `{count}`.")

@prefix_reset_tickets.error
async def prefix_reset_tickets_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_resettickets_usage_embed())

# ================= ЗАПУСК БОТА =================

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("Ошибка: DISCORD_TOKEN не найден в файле .env")
