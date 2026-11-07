import os
import sqlite3
import datetime

import discord
from discord import app_commands
from discord.ext import commands



def _resolve_db_path() -> str:
    """Where the database lives. On Railway, a Volume keeps it across redeploys."""
    explicit = os.environ.get("DB_PATH")
    if explicit:
        return explicit
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if volume:
        return os.path.join(volume, "suggestions.db")
    return "suggestions.db"


DB_PATH = _resolve_db_path()
_dir = os.path.dirname(DB_PATH)
if _dir:
    os.makedirs(_dir, exist_ok=True)
if os.environ.get("RAILWAY_ENVIRONMENT") and not (
    os.environ.get("DB_PATH") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
):
    print("WARNING: no Railway Volume attached - suggestions and numbering will be wiped on every redeploy!")
else:
    print(f"Database file: {DB_PATH}")
TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config (
            guild_id INTEGER PRIMARY KEY,
            suggest_channel INTEGER,
            review_channel INTEGER,
            log_channel INTEGER,
            staff_role INTEGER,
            next_number INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            kind TEXT DEFAULT 'suggestion',
            number INTEGER,
            parent_number INTEGER,
            user_id INTEGER,
            username TEXT,
            content TEXT,
            status TEXT DEFAULT 'pending',
            moderator_id INTEGER,
            moderator_username TEXT,
            created_at TEXT,
            decided_at TEXT,
            public_message_id INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def get_config(guild_id):
    conn = db()
    row = conn.execute("SELECT * FROM config WHERE guild_id = ?", (guild_id,)).fetchone()
    conn.close()
    return row


def set_config(guild_id, **kwargs):
    conn = db()
    existing = conn.execute("SELECT guild_id FROM config WHERE guild_id = ?", (guild_id,)).fetchone()
    if existing:
        cols = ", ".join(f"{k} = ?" for k in kwargs)
        conn.execute(f"UPDATE config SET {cols} WHERE guild_id = ?", (*kwargs.values(), guild_id))
    else:
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        conn.execute(
            f"INSERT INTO config (guild_id, {cols}) VALUES (?, {placeholders})",
            (guild_id, *kwargs.values()),
        )
    conn.commit()
    conn.close()


def create_suggestion(guild_id, user_id, username, content, kind="suggestion", parent_number=None):
    conn = db()
    cur = conn.execute(
        """INSERT INTO suggestions
           (guild_id, kind, parent_number, user_id, username, content, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (guild_id, kind, parent_number, user_id, username, content, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def get_suggestion(cid):
    conn = db()
    row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (cid,)).fetchone()
    conn.close()
    return row


def get_suggestion_by_number(guild_id, number):
    conn = db()
    row = conn.execute(
        "SELECT * FROM suggestions WHERE guild_id = ? AND number = ? AND kind = 'suggestion'",
        (guild_id, number),
    ).fetchone()
    conn.close()
    return row


def decide_suggestion(cid, status, moderator_id, moderator_username, number=None):
    conn = db()
    conn.execute(
        "UPDATE suggestions SET status=?, moderator_id=?, moderator_username=?, decided_at=?, number=? WHERE id=?",
        (
            status,
            moderator_id,
            moderator_username,
            datetime.datetime.utcnow().isoformat(),
            number,
            cid,
        ),
    )
    conn.commit()
    conn.close()


def set_public_message(cid, message_id):
    conn = db()
    conn.execute("UPDATE suggestions SET public_message_id = ? WHERE id = ?", (message_id, cid))
    conn.commit()
    conn.close()


def next_number(guild_id):
    conn = db()
    row = conn.execute("SELECT next_number FROM config WHERE guild_id = ?", (guild_id,)).fetchone()
    n = row["next_number"] if row else 1
    conn.execute(
        "UPDATE config SET next_number = ? WHERE guild_id = ?",
        (n + 1, guild_id),
    )
    conn.commit()
    conn.close()
    return n


init_db()


def staff_ping(cfg):
    if cfg and cfg["staff_role"]:
        return f"<@&{cfg['staff_role']}>"
    return None


def make_decision_view(cid):
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(label="Accept", style=discord.ButtonStyle.success, custom_id=f"suggest_accept_{cid}")
    )
    view.add_item(
        discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger, custom_id=f"suggest_reject_{cid}")
    )
    return view


def make_public_view(number):
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(label="Reply", style=discord.ButtonStyle.secondary, custom_id=f"reply_start_{number}")
    )
    view.add_item(
        discord.ui.Button(label="Suggest", style=discord.ButtonStyle.primary, custom_id="suggest_start")
    )
    return view


# ---------------------------------------------------------------------
# MODALS
# ---------------------------------------------------------------------
class SuggestModal(discord.ui.Modal, title="New Suggestion"):
    content = discord.ui.TextInput(
        label="Write your suggestion",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild_id)
        if not cfg or not cfg["review_channel"]:
            await interaction.response.send_message(
                "This bot hasn't been set up yet. Ask a staff member to run /setup.",
                ephemeral=True,
            )
            return

        review_channel = interaction.guild.get_channel(cfg["review_channel"])
        if review_channel is None:
            await interaction.response.send_message(
                "Review channel not found, please contact staff.", ephemeral=True
            )
            return

        cid = create_suggestion(
            interaction.guild_id, interaction.user.id, str(interaction.user), str(self.content)
        )

        embed = discord.Embed(
            title=f"New suggestion pending review (internal #{cid})",
            description=str(self.content),
            color=discord.Color.teal(),
            timestamp=datetime.datetime.utcnow(),
        )
        embed.add_field(
            name="Author (visible to staff only, here)",
            value=f"{interaction.user.mention} ({interaction.user.id})",
            inline=False,
        )

        await review_channel.send(
            content=staff_ping(cfg),
            embed=embed,
            view=make_decision_view(cid),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await interaction.response.send_message(
            "Your suggestion has been sent to staff for review. Thank you!", ephemeral=True
        )


class ReplyModal(discord.ui.Modal, title="Reply to a Suggestion"):
    def __init__(self, parent_number: int):
        super().__init__()
        self.parent_number = parent_number

    content = discord.ui.TextInput(
        label="Write your reply",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = get_config(interaction.guild_id)
        if not cfg or not cfg["review_channel"]:
            await interaction.response.send_message(
                "This bot hasn't been set up yet. Ask a staff member to run /setup.",
                ephemeral=True,
            )
            return

        review_channel = interaction.guild.get_channel(cfg["review_channel"])
        if review_channel is None:
            await interaction.response.send_message(
                "Review channel not found, please contact staff.", ephemeral=True
            )
            return

        parent = get_suggestion_by_number(interaction.guild_id, self.parent_number)

        cid = create_suggestion(
            interaction.guild_id,
            interaction.user.id,
            str(interaction.user),
            str(self.content),
            kind="reply",
            parent_number=self.parent_number,
        )

        embed = discord.Embed(
            title=f"New reply pending review (internal #{cid}) — replying to suggestion #{self.parent_number:03}",
            description=str(self.content),
            color=discord.Color.orange(),
            timestamp=datetime.datetime.utcnow(),
        )
        if parent:
            preview = parent["content"][:200] + ("…" if len(parent["content"]) > 200 else "")
            embed.add_field(name=f"Original suggestion #{self.parent_number:03}", value=preview, inline=False)
        embed.add_field(
            name="Author of the reply (visible to staff only, here)",
            value=f"{interaction.user.mention} ({interaction.user.id})",
            inline=False,
        )

        await review_channel.send(
            content=staff_ping(cfg),
            embed=embed,
            view=make_decision_view(cid),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        await interaction.response.send_message(
            "Your reply has been sent to staff for review. Thank you!", ephemeral=True
        )


# ---------------------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------------------
@bot.tree.command(name="suggest", description="Submit an anonymous suggestion")
async def suggest(interaction: discord.Interaction):
    await interaction.response.send_modal(SuggestModal())


@bot.tree.command(name="setup", description="Configure the suggestions bot channels (staff only)")
@app_commands.describe(
    suggestions_channel="Public channel where approved suggestions will be posted",
    review_channel="Private staff channel where new suggestions arrive for approval",
    log_channel="Private staff channel where accept/reject decisions are logged",
    staff_role="Role allowed to accept or reject suggestions",
)
@app_commands.default_permissions(manage_guild=True)
async def setup(
    interaction: discord.Interaction,
    suggestions_channel: discord.TextChannel,
    review_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    staff_role: discord.Role,
):
    set_config(
        interaction.guild_id,
        suggest_channel=suggestions_channel.id,
        review_channel=review_channel.id,
        log_channel=log_channel.id,
        staff_role=staff_role.id,
    )
    await interaction.response.send_message(
        "Configuration saved!\n"
        f"- Public suggestions: {suggestions_channel.mention}\n"
        f"- Staff review: {review_channel.mention}\n"
        f"- Staff log: {log_channel.mention}\n"
        f"- Staff role: {staff_role.mention}",
        ephemeral=True,
    )


# ---------------------------------------------------------------------
# HANDLE BUTTON INTERACTIONS
# ---------------------------------------------------------------------
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return

    custom_id = interaction.data.get("custom_id", "")

    # ---- "Reply" button on a public suggestion ----
    if custom_id.startswith("reply_start_"):
        number = int(custom_id.rsplit("_", 1)[1])
        await interaction.response.send_modal(ReplyModal(parent_number=number))
        return

    # ---- "Suggest" button on a public suggestion ----
    if custom_id == "suggest_start":
        await interaction.response.send_modal(SuggestModal())
        return

    if not custom_id.startswith("suggest_"):
        return

    # ---- Accept / Reject buttons in the review channel ----
    action, cid_str = custom_id.rsplit("_", 1)
    cid = int(cid_str)

    suggestion = get_suggestion(cid)
    if suggestion is None:
        await interaction.response.send_message("Entry not found in the database.", ephemeral=True)
        return
    if suggestion["status"] != "pending":
        await interaction.response.send_message("This entry has already been handled.", ephemeral=True)
        return

    cfg = get_config(interaction.guild_id)
    member = interaction.user

    has_perm = member.guild_permissions.administrator or member.guild_permissions.manage_guild
    if cfg and cfg["staff_role"]:
        has_perm = has_perm or any(r.id == cfg["staff_role"] for r in member.roles)

    if not has_perm:
        await interaction.response.send_message(
            "You don't have permission to manage suggestions.", ephemeral=True
        )
        return

    original_embed = interaction.message.embeds[0]
    is_reply = suggestion["kind"] == "reply"

    if action == "suggest_accept":
        suggest_channel = interaction.guild.get_channel(cfg["suggest_channel"]) if cfg else None

        if is_reply:
            decide_suggestion(cid, "approved", member.id, str(member))
            sent_message = None
            if suggest_channel:
                reference = None
                parent = get_suggestion_by_number(interaction.guild_id, suggestion["parent_number"])
                if parent and parent["public_message_id"]:
                    try:
                        reference = await suggest_channel.fetch_message(parent["public_message_id"])
                    except discord.NotFound:
                        reference = None
                public_embed = discord.Embed(
                    title=f"Reply to suggestion #{suggestion['parent_number']:03}",
                    description=suggestion["content"],
                    color=discord.Color.orange(),
                    timestamp=datetime.datetime.utcnow(),
                )
                sent_message = await suggest_channel.send(embed=public_embed, reference=reference)

            log_channel = interaction.guild.get_channel(cfg["log_channel"]) if cfg and cfg["log_channel"] else None
            if log_channel:
                log_embed = discord.Embed(
                    title=f"Reply to suggestion #{suggestion['parent_number']:03} approved",
                    color=discord.Color.green(),
                )
                log_embed.add_field(name="Content", value=suggestion["content"], inline=False)
                log_embed.add_field(
                    name="Author", value=f"<@{suggestion['user_id']}> ({suggestion['user_id']})", inline=True
                )
                log_embed.add_field(name="Approved by", value=f"{member.mention} ({member.id})", inline=True)
                await log_channel.send(embed=log_embed)

            original_embed.add_field(name="Status", value=f"Approved by {member.mention}", inline=False)
            await interaction.response.edit_message(embed=original_embed, view=None)

        else:
            number = next_number(interaction.guild_id)
            decide_suggestion(cid, "approved", member.id, str(member), number)

            if suggest_channel:
                public_embed = discord.Embed(
                    title=f"Suggestion #{number:03}",
                    description=suggestion["content"],
                    color=discord.Color.teal(),
                    timestamp=datetime.datetime.utcnow(),
                )
                sent_message = await suggest_channel.send(embed=public_embed, view=make_public_view(number))
                set_public_message(cid, sent_message.id)

            log_channel = interaction.guild.get_channel(cfg["log_channel"]) if cfg and cfg["log_channel"] else None
            if log_channel:
                log_embed = discord.Embed(title=f"Suggestion #{number:03} approved", color=discord.Color.green())
                log_embed.add_field(name="Content", value=suggestion["content"], inline=False)
                log_embed.add_field(
                    name="Author", value=f"<@{suggestion['user_id']}> ({suggestion['user_id']})", inline=True
                )
                log_embed.add_field(name="Approved by", value=f"{member.mention} ({member.id})", inline=True)
                await log_channel.send(embed=log_embed)

            original_embed.add_field(
                name="Status", value=f"Approved by {member.mention} — #{number:03}", inline=False
            )
            await interaction.response.edit_message(embed=original_embed, view=None)

    elif action == "suggest_reject":
        decide_suggestion(cid, "rejected", member.id, str(member))

        log_channel = interaction.guild.get_channel(cfg["log_channel"]) if cfg and cfg["log_channel"] else None
        if log_channel:
            title = "Reply rejected" if is_reply else "Suggestion rejected"
            log_embed = discord.Embed(title=title, color=discord.Color.red())
            log_embed.add_field(name="Content", value=suggestion["content"], inline=False)
            if is_reply:
                log_embed.add_field(
                    name="In reply to", value=f"Suggestion #{suggestion['parent_number']:03}", inline=False
                )
            log_embed.add_field(
                name="Author", value=f"<@{suggestion['user_id']}> ({suggestion['user_id']})", inline=True
            )
            log_embed.add_field(name="Rejected by", value=f"{member.mention} ({member.id})", inline=True)
            await log_channel.send(embed=log_embed)

        original_embed.add_field(name="Status", value=f"Rejected by {member.mention}", inline=False)
        await interaction.response.edit_message(embed=original_embed, view=None)


# Commands sync instantly to this server.
MY_GUILD_ID = int(os.environ.get("GUILD_ID", "1505662481324834906"))  # override with a GUILD_ID variable if needed


@bot.event
async def on_ready():
    if MY_GUILD_ID:
        guild = discord.Object(id=MY_GUILD_ID)
        try:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)  # instant sync for this server
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()             # remove leftover global copies
            print("Instant guild sync succeeded.")
        except discord.Forbidden as e:
            print(f"Guild sync failed, falling back to global sync: {e}")
            await bot.tree.sync()
    else:
        await bot.tree.sync()

    print(f"Logged in as {bot.user} - commands synced")


bot.run(TOKEN)
