"""
Event Reminder plugin
=====================

    .event <event link>

Opens a setup panel (buttons + dropdown) where you edit how the reminder looks,
pick the channel and the reminder times, preview it, and schedule it.

Panel
    Mention        who to ping (@everyone, @here, a role name / ID / mention)
    Announcement   text shown in the embed (empty = the event's own description)
    Color          embed color (hex like #5865F2, or red / blue / gold / ...)
    Image URL      big picture at the bottom of the embed
    Thumbnail URL  small picture in the corner (pre-filled with the event link)
    Host           pre-filled from the event, editable
    Location       pre-filled from the event, editable
    Reminder times e.g. "1 hour before, 2 hours before, at start"
    Channel        dropdown
    Preview        shows exactly what will be posted (only you can see it)
    Post now       posts it straight away
    Schedule       saves it; the bot posts it at every reminder time

Other commands
    .event list                          show every pending reminder
    .event cancel <event link or ID>     remove an event's reminders
    .event cancel <event link> #channel  remove only that channel's reminders

Needs discord.py 2.1+ (buttons, modals, channel select).
"""

import logging
import re
import time
from typing import List, Optional, Tuple

import discord
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
CHECK_INTERVAL = 30                # seconds between checks for due reminders
MAX_REMINDERS_PER_EVENT = 10
START_GRACE = 15 * 60              # an "at start" reminder later than this is dropped
MAX_RETRIES = 3                    # send attempts before a failing reminder is dropped
PANEL_TIMEOUT = 15 * 60            # seconds of inactivity before the panel stops working

DEFAULT_COLOR = "#5865F2"
DEFAULT_CONFIG = {
    "mention": "",
    "announcement": "",
    "color": DEFAULT_COLOR,
    "image_url": "",
    "thumbnail_url": "",
    "host": "",
    "location": "",
}
NAMED_COLORS = {
    "red": "#ED4245", "orange": "#E67E22", "gold": "#F1C40F", "green": "#57F287",
    "teal": "#1ABC9C", "blue": "#3498DB", "blurple": "#5865F2", "purple": "#9B59B6",
    "pink": "#EB459E", "white": "#FFFFFF",
}

ALLOW_PINGS = discord.AllowedMentions(everyone=True, roles=True, users=True)

EVENT_URL_RE = re.compile(r"discord(?:app)?\.com/events/(\d+)/(\d+)")
INVITE_EVENT_RE = re.compile(r"[?&]event=(\d+)")
TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")

UNITS = {}
for _names, _seconds in (
    (("w", "wk", "wks", "week", "weeks"), 7 * 86400),
    (("d", "day", "days"), 86400),
    (("h", "hr", "hrs", "hour", "hours"), 3600),
    (("m", "min", "mins", "minute", "minutes"), 60),
):
    for _name in _names:
        UNITS[_name] = _seconds

# The editable fields: button label/emoji/row, plus the pop-up form that opens.
FIELD_SPECS = {
    "mention": dict(
        label="Mention", emoji="📣", row=0, title="Edit mention", input="Who should be pinged?",
        placeholder="@everyone, @here, a role name or role ID. Empty = no ping", max=200,
    ),
    "announcement": dict(
        label="Announcement", emoji="📢", row=0, title="Edit announcement", input="Announcement text",
        placeholder="Shown in the embed. Empty = the event's own description", max=1500,
        style=discord.TextStyle.paragraph,
    ),
    "color": dict(
        label="Color", emoji="🎨", row=0, title="Edit embed color", input="Color",
        placeholder="#5865F2 or: red, orange, gold, green, blue, purple, pink", max=20,
    ),
    "image_url": dict(
        label="Image URL", emoji="🖼️", row=0, title="Edit image URL", input="Image URL",
        placeholder="Direct link to an image (png, jpg, gif). Empty = no image", max=500,
    ),
    "thumbnail_url": dict(
        label="Thumbnail URL", emoji="🔗", row=0, title="Edit thumbnail URL", input="Thumbnail URL",
        placeholder="Small image in the corner of the embed. Empty = none", max=500,
    ),
    "host": dict(
        label="Host", emoji="👤", row=1, title="Edit host", input="Host",
        placeholder="A name or a mention. Empty = hide the host", max=200,
    ),
    "location": dict(
        label="Location", emoji="📍", row=1, title="Edit location", input="Location",
        placeholder="Where it happens. Empty = hide the location", max=200,
    ),
    "timings": dict(
        label="Reminder times", emoji="⏰", row=1, title="Edit reminder times", input="Reminder times",
        placeholder="1 hour before, 2 hours before, at start", max=200, required=True,
    ),
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_event_ref(text: str) -> Optional[Tuple[Optional[int], int]]:
    """Return (guild_id or None, event_id) from an event link / invite / raw ID."""
    text = text.strip().strip("<>")
    match = EVENT_URL_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = INVITE_EVENT_RE.search(text)  # discord.gg/xxxx?event=123
    if match:
        return None, int(match.group(1))
    if text.isdigit():
        return None, int(text)
    return None


def parse_offsets(text: str) -> List[int]:
    """
    Turn "1 hour before, 2 hours before" into [7200, 3600] (seconds, largest first).

    * Separate reminders with commas (or ; or |).
    * Units inside one reminder are added together: "1h 30m" -> 90 minutes.
    * "start" / "now" / "0" -> a reminder at the moment the event starts.
    """
    offsets = set()
    for chunk in re.split(r"[,;|\n]+", text.lower()):
        chunk = chunk.strip()
        if not chunk:
            continue

        if chunk == "0" or re.search(r"\b(start|starts|starting|now)\b", chunk):
            offsets.add(0)
            continue

        matches = TOKEN_RE.findall(chunk)
        leftover = TOKEN_RE.sub(" ", chunk)
        if not matches or re.search(r"\d", leftover):
            raise ValueError(f"I couldn't understand `{chunk}`. Try `1 hour before` or `30 min`.")

        total = 0.0
        for number, unit in matches:
            if unit not in UNITS:
                raise ValueError(
                    f"I don't know the time unit `{unit}` in `{chunk}`. Use weeks, days, hours or minutes."
                )
            total += float(number) * UNITS[unit]

        seconds = int(round(total))
        if seconds < 60:
            raise ValueError(f"`{chunk}` is too short. The minimum is 1 minute.")
        offsets.add(seconds)

    if not offsets:
        raise ValueError("Give me at least one reminder time, e.g. `1 hour before, 2 hours before`.")
    if len(offsets) > MAX_REMINDERS_PER_EVENT:
        raise ValueError(f"That's a lot of reminders. The limit is {MAX_REMINDERS_PER_EVENT} per event.")
    return sorted(offsets, reverse=True)


def format_duration(seconds: int) -> str:
    parts = []
    for name, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        amount, seconds = divmod(int(seconds), size)
        if amount:
            parts.append(f"{amount} {name}{'s' if amount != 1 else ''}")
    return " ".join(parts) or "0 minutes"


def offset_label(offset: int) -> str:
    return "at event start" if offset == 0 else f"{format_duration(offset)} before"


def short(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_color(text: str) -> str:
    value = text.strip().lower()
    if value in NAMED_COLORS:
        return NAMED_COLORS[value]
    value = value.lstrip("#")
    if value.startswith("0x"):
        value = value[2:]
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if not re.fullmatch(r"[0-9a-f]{6}", value):
        raise ValueError("I couldn't read that color. Use a hex code like `#5865F2` or a name like `red`.")
    return "#" + value.upper()


def resolve_mention(guild: discord.Guild, text: str) -> str:
    """Turn '@everyone', 'Event Squad', '1234567890' or '<@&123>' into mention text."""
    text = text.strip()
    if not text:
        return ""

    # The whole text might be a role name that contains spaces.
    wanted = text.lstrip("@").lower()
    role = discord.utils.find(lambda r: r.name.lower() == wanted and not r.is_default(), guild.roles)
    if role:
        return role.mention

    parts = []
    for token in re.split(r"[\s,]+", text):
        if not token:
            continue
        name = token.lstrip("@").lower()
        if name in ("everyone", "here"):
            parts.append(f"@{name}")
        elif re.fullmatch(r"<@[!&]?\d+>", token):
            parts.append(token)
        elif token.isdigit():
            role = guild.get_role(int(token))
            if role is None:
                raise ValueError(f"I couldn't find a role with the ID `{token}`.")
            parts.append(role.mention)
        else:
            raise ValueError(f"I couldn't find a role called `{token}`.")
    return " ".join(parts)


def event_location(event: discord.ScheduledEvent) -> Optional[str]:
    channel = getattr(event, "channel", None)
    if channel is not None:
        return channel.mention
    location = getattr(event, "location", None)
    if location is None:
        return None
    return getattr(location, "mention", None) or str(location)


def event_is_over(event: discord.ScheduledEvent) -> bool:
    return event.status in (discord.EventStatus.completed, discord.EventStatus.cancelled)


def initial_config(event: discord.ScheduledEvent) -> dict:
    """Starting values for a brand-new setup. Host/location come from the event."""
    config = dict(DEFAULT_CONFIG)
    cover = getattr(event, "cover_image", None)
    config["image_url"] = cover.url if cover else ""
    config["thumbnail_url"] = event.url
    config["host"] = f"<@{event.creator_id}>" if event.creator_id else ""
    config["location"] = event_location(event) or ""
    return config


def build_reminder_embed(config: dict, event: discord.ScheduledEvent, offset: Optional[int] = None):
    """The embed that gets posted (and previewed). Start time always comes from the live event."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    embed = discord.Embed(
        title=event.name,
        url=event.url,
        color=discord.Color(int(cfg["color"].lstrip("#"), 16)),
    )
    embed.set_author(name="🎉 Starting now!" if offset == 0 else "⏰ Event reminder")

    body = cfg["announcement"].strip() or (event.description or "").strip()
    if body:
        embed.description = short(body, 1500)

    start = int(event.start_time.timestamp())
    embed.add_field(name="🕒 Start", value=f"<t:{start}:F>\n(<t:{start}:R>)", inline=True)
    if cfg["location"]:
        embed.add_field(name="📍 Location", value=cfg["location"], inline=True)
    if cfg["host"]:
        embed.add_field(name="👤 Host", value=cfg["host"], inline=True)

    if cfg["image_url"]:
        embed.set_image(url=cfg["image_url"])
    if cfg["thumbnail_url"]:
        embed.set_thumbnail(url=cfg["thumbnail_url"])
    return embed


# --------------------------------------------------------------------------- #
# Setup panel
# --------------------------------------------------------------------------- #
class FieldModal(discord.ui.Modal):
    """Small pop-up form used by every edit button."""

    def __init__(self, panel: "EventSetupView", key: str):
        spec = FIELD_SPECS[key]
        super().__init__(title=spec["title"])
        self.panel = panel
        self.key = key
        self.field = discord.ui.TextInput(
            label=spec["input"],
            style=spec.get("style", discord.TextStyle.short),
            placeholder=spec["placeholder"],
            default=panel.field_text(key) or None,
            required=spec.get("required", False),
            max_length=spec["max"],
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.panel.apply(self.key, self.field.value)
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        await interaction.response.edit_message(embed=self.panel.panel_embed(), view=self.panel)


class EventSetupView(discord.ui.View):
    def __init__(self, cog, ctx, event, config, channel_id, offsets, loaded=False):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.owner_id = ctx.author.id
        self.guild = ctx.guild          # server where the reminders will be posted
        self.event = event
        self.config = config
        self.channel_id = channel_id
        self.offsets = offsets
        self.loaded = loaded
        self.message = None

        # Row 0 and 1: one button per editable field (they open FieldModal).
        for key, spec in FIELD_SPECS.items():
            button = discord.ui.Button(
                label=spec["label"], emoji=spec["emoji"], style=discord.ButtonStyle.secondary, row=spec["row"]
            )
            button.callback = self._opener(key)
            self.add_item(button)

    # ----- state helpers ------------------------------------------------- #
    def _opener(self, key):
        async def callback(interaction: discord.Interaction):
            await interaction.response.send_modal(FieldModal(self, key))
        return callback

    def field_text(self, key: str) -> str:
        if key == "timings":
            return ", ".join("at start" if o == 0 else format_duration(o) for o in self.offsets)
        return self.config.get(key, "")

    def apply(self, key: str, raw: str):
        raw = raw.strip()
        if key == "timings":
            self.offsets = parse_offsets(raw)
        elif key == "mention":
            self.config["mention"] = resolve_mention(self.guild, raw)
        elif key == "color":
            self.config["color"] = parse_color(raw) if raw else DEFAULT_COLOR
        elif key in ("image_url", "thumbnail_url"):
            if raw and not re.fullmatch(r"https?://\S+", raw):
                raise ValueError("That needs to be a full link starting with `http://` or `https://`.")
            self.config[key] = raw
        else:
            self.config[key] = raw

    def channel_problem(self) -> Optional[str]:
        if not self.channel_id:
            return "⚠️ Pick a channel first."
        channel = self.guild.get_channel(self.channel_id)
        if channel is None:
            return "⚠️ I can't see that channel. Pick it again."
        perms = channel.permissions_for(self.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return f"❌ I need **View Channel**, **Send Messages** and **Embed Links** in {channel.mention}."
        return None

    def panel_embed(self) -> discord.Embed:
        cfg = self.config

        def show(value, limit=120):
            return short(value, limit) or "—"

        intro = f"**[{self.event.name}]({self.event.url})**\n"
        if self.loaded:
            intro += "*Loaded your existing reminders for this event.*\n"
        intro += "Edit anything below, check it with **Preview**, then press **Schedule**."

        embed = discord.Embed(
            title="🗓️ Event reminder setup",
            description=intro,
            color=discord.Color(int(cfg["color"].lstrip("#"), 16)),
        )
        embed.add_field(
            name="📺 Channel",
            value=f"<#{self.channel_id}>" if self.channel_id else "⚠️ Not chosen yet",
        )
        embed.add_field(
            name="⏰ Reminders",
            value="\n".join(f"• {offset_label(o)}" for o in self.offsets) or "⚠️ None set",
        )
        embed.add_field(name="🎨 Color", value=f"`{cfg['color']}`")
        embed.add_field(name="📣 Mention", value=show(cfg["mention"]))
        embed.add_field(name="📢 Announcement", value=show(cfg["announcement"]) if cfg["announcement"] else "*Event description*")
        embed.add_field(name="👤 Host", value=show(cfg["host"]))
        embed.add_field(name="📍 Location", value=show(cfg["location"]))
        embed.add_field(name="🖼️ Image URL", value=show(cfg["image_url"], 80), inline=False)
        embed.add_field(name="🔗 Thumbnail URL", value=show(cfg["thumbnail_url"], 80), inline=False)
        return embed

    # ----- view plumbing ------------------------------------------------- #
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran the command can use this panel.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(content="⌛ This setup panel timed out. Run the command again.", view=self)
            except discord.HTTPException:
                pass

    # ----- row 2: channel dropdown --------------------------------------- #
    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="📺 Channel to post in",
        min_values=1,
        max_values=1,
        row=2,
    )
    async def pick_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.channel_id = select.values[0].id
        await interaction.response.edit_message(embed=self.panel_embed(), view=self)

    # ----- row 3: actions ------------------------------------------------ #
    @discord.ui.button(label="Preview", emoji="👁️", style=discord.ButtonStyle.primary, row=3)
    async def preview_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        header = "👁️ **Preview** (only you can see this)"
        if self.config["mention"]:
            header += f"\n{self.config['mention']}"
        await interaction.response.send_message(
            content=header,
            embed=build_reminder_embed(self.config, self.event),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Post now", emoji="📤", style=discord.ButtonStyle.secondary, row=3)
    async def post_now_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        problem = self.channel_problem()
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)
        channel = self.guild.get_channel(self.channel_id)
        await channel.send(
            content=self.config["mention"] or None,
            embed=build_reminder_embed(self.config, self.event),
            allowed_mentions=ALLOW_PINGS,
        )
        await interaction.response.send_message(f"📤 Posted in {channel.mention}.", ephemeral=True)

    @discord.ui.button(label="Schedule", emoji="✅", style=discord.ButtonStyle.success, row=3)
    async def schedule_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        problem = self.channel_problem()
        if not problem and not self.offsets:
            problem = "⚠️ Set at least one reminder time first."
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)

        await interaction.response.defer()
        channel = self.guild.get_channel(self.channel_id)
        try:
            event = await self.event.guild.fetch_scheduled_event(self.event.id)
        except discord.HTTPException:
            return await interaction.followup.send("❌ I couldn't re-read the event. Try again.", ephemeral=True)
        if event_is_over(event):
            return await interaction.followup.send("❌ That event has already ended or been cancelled.", ephemeral=True)

        scheduled, skipped = await self.cog.schedule_reminders(
            event, channel, self.offsets, self.config, interaction.user.id
        )
        if not scheduled:
            return await interaction.followup.send(
                f"❌ All of those times have already passed (the event starts <t:{int(event.start_time.timestamp())}:R>).",
                ephemeral=True,
            )

        lines = [
            f"• **{offset_label(offset)}** → <t:{int(at)}:f> (<t:{int(at)}:R>)" for offset, at in scheduled
        ]
        done = discord.Embed(
            title="✅ Reminders scheduled",
            description=f"**[{event.name}]({event.url})**\nPosting in {channel.mention}\n\n" + "\n".join(lines),
            color=discord.Color.green(),
        )
        if skipped:
            done.add_field(
                name="Skipped (already passed)",
                value="\n".join(f"• {offset_label(offset)}" for offset, _ in skipped),
                inline=False,
            )
        done.set_footer(text="Run the command again with the same event to change anything.")
        self.stop()
        await interaction.edit_original_response(content=None, embed=done, view=None)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.danger, row=3)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Setup closed. Nothing was changed.", embed=None, view=None)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class EventReminder(commands.Cog):
    """Set up and post reminders for Discord scheduled events."""

    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)  # motor AsyncIOMotorCollection
        self.reminder_loop.start()

    def cog_unload(self):
        self.reminder_loop.cancel()

    async def cog_check(self, ctx):
        # Applies to every command in this cog (including subcommands).
        # To use Modmail's permission levels instead, delete this method and decorate the
        # commands with:  @checks.has_permissions(PermissionLevel.SUPPORTER)
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not ctx.author.guild_permissions.manage_events:
            raise commands.MissingPermissions(["manage_events"])
        return True

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @commands.group(name="event", invoke_without_command=True)
    async def event_cmd(self, ctx, *, event_link: str):
        """
        Open the reminder setup panel for a Discord event.

        Usage: event <event link>
        """
        ref = parse_event_ref(event_link)
        if ref is None:
            return await ctx.send(
                "❌ That doesn't look like a Discord event link. It should look like "
                "`https://discord.com/events/<server_id>/<event_id>`."
            )
        guild_id, event_id = ref
        guild = self.bot.get_guild(guild_id) if guild_id else ctx.guild
        if guild is None:
            return await ctx.send("❌ I'm not in the server that event belongs to.")

        try:
            event = await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            return await ctx.send("❌ I couldn't find that event. Is the link right, and is the event still there?")
        except discord.HTTPException:
            logger.exception("Could not fetch scheduled event %s", event_id)
            return await ctx.send("❌ Discord returned an error while I was reading that event. Try again in a moment.")
        if event_is_over(event):
            return await ctx.send("❌ That event has already ended or been cancelled.")

        # If reminders already exist for this event, start from those so you can edit them.
        existing = await self._load_existing(guild.id, event.id)
        if existing:
            channel_id, config, offsets = existing
        else:
            channel_id, config, offsets = None, initial_config(event), []

        view = EventSetupView(self, ctx, event, config, channel_id, offsets, loaded=bool(existing))
        view.message = await ctx.send(embed=view.panel_embed(), view=view)

    @event_cmd.command(name="list")
    async def event_list(self, ctx):
        """Show every pending event reminder."""
        docs = await self.coll.find({}).sort("remind_at", 1).to_list(length=None)
        if not docs:
            return await ctx.send("There are no pending reminders.")

        groups = {}
        for doc in docs:
            groups.setdefault((doc["event_id"], doc["channel_id"]), []).append(doc)

        lines = []
        for (event_id, channel_id), items in groups.items():
            first = items[0]
            url = f"https://discord.com/events/{first['guild_id']}/{event_id}"
            lines.append(f"**[{first['event_name']}]({url})** → <#{channel_id}>")
            for doc in items:
                ts = int(doc["remind_at"])
                lines.append(f"> {offset_label(doc['offset_seconds'])}: <t:{ts}:f> (<t:{ts}:R>)")
            lines.append("")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000].rsplit("\n", 1)[0] + "\n…and more"
        await ctx.send(embed=discord.Embed(title="⏰ Pending reminders", description=text, color=discord.Color.blurple()))

    @event_cmd.command(name="cancel")
    async def event_cancel(self, ctx, event: str, channel: Optional[discord.TextChannel] = None):
        """
        Cancel reminders for an event.

        Usage: event cancel <event link or ID> [channel]
        """
        ref = parse_event_ref(event)
        if ref is None:
            return await ctx.send("❌ Give me an event link or event ID.")
        guild_id, event_id = ref

        query = {"event_id": event_id}
        if guild_id:
            query["guild_id"] = guild_id
        if channel:
            query["channel_id"] = channel.id

        result = await self.coll.delete_many(query)
        if result.deleted_count:
            await ctx.send(f"🗑️ Removed {result.deleted_count} reminder(s).")
        else:
            await ctx.send("I didn't find any reminders for that event.")

    # ------------------------------------------------------------------ #
    # Storage
    # ------------------------------------------------------------------ #
    async def _load_existing(self, guild_id: int, event_id: int):
        docs = await self.coll.find({"guild_id": guild_id, "event_id": event_id}).sort("remind_at", 1).to_list(length=None)
        if not docs:
            return None
        channel_id = docs[0]["channel_id"]
        same = [d for d in docs if d["channel_id"] == channel_id]
        config = {**DEFAULT_CONFIG, **same[0].get("config", {})}
        offsets = sorted({d["offset_seconds"] for d in same}, reverse=True)
        return channel_id, config, offsets

    async def schedule_reminders(self, event, channel, offsets, config, author_id):
        """Save reminders (replacing any earlier ones for this event + channel)."""
        start_ts = event.start_time.timestamp()
        now = time.time()
        scheduled, skipped = [], []
        for offset in offsets:
            remind_at = start_ts - offset
            (scheduled if remind_at > now else skipped).append((offset, remind_at))

        if scheduled:
            key = {"guild_id": event.guild.id, "event_id": event.id, "channel_id": channel.id}
            await self.coll.delete_many(key)
            await self.coll.insert_many(
                [
                    {
                        **key,
                        "event_name": event.name,
                        "event_start": start_ts,
                        "offset_seconds": offset,
                        "remind_at": remind_at,
                        "config": dict(config),
                        "created_by": author_id,
                        "attempts": 0,
                    }
                    for offset, remind_at in scheduled
                ]
            )
        return scheduled, skipped

    # ------------------------------------------------------------------ #
    # Keep reminders in sync with the event
    # (needs the "guild scheduled events" intent; if it's off, reminders
    #  still catch moved events at send time)
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before, after):
        if event_is_over(after):
            await self.coll.delete_many({"guild_id": after.guild.id, "event_id": after.id})
            return
        if before.start_time == after.start_time and before.name == after.name:
            return

        new_start = after.start_time.timestamp()
        now = time.time()
        async for doc in self.coll.find({"guild_id": after.guild.id, "event_id": after.id}):
            remind_at = new_start - doc["offset_seconds"]
            if remind_at <= now:
                await self.coll.delete_one({"_id": doc["_id"]})  # event moved up; this one is stale
            else:
                await self.coll.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"remind_at": remind_at, "event_start": new_start, "event_name": after.name}},
                )

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event):
        await self.coll.delete_many({"guild_id": event.guild.id, "event_id": event.id})

    # ------------------------------------------------------------------ #
    # Background task that actually posts the reminders
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=CHECK_INTERVAL)
    async def reminder_loop(self):
        due = await self.coll.find({"remind_at": {"$lte": time.time()}}).to_list(length=None)
        for doc in due:
            try:
                await self._fire(doc)
            except Exception:
                logger.exception("Failed to send reminder %s", doc.get("_id"))
                if doc.get("attempts", 0) + 1 >= MAX_RETRIES:
                    await self.coll.delete_one({"_id": doc["_id"]})
                else:
                    await self.coll.update_one({"_id": doc["_id"]}, {"$inc": {"attempts": 1}})

    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        await self.bot.wait_until_ready()

    async def _fire(self, doc):
        guild = self.bot.get_guild(doc["guild_id"])
        channel = self.bot.get_channel(doc["channel_id"])
        if guild is None or channel is None:
            return await self.coll.delete_one({"_id": doc["_id"]})

        try:
            event = await guild.fetch_scheduled_event(doc["event_id"])
        except discord.NotFound:
            return await self.coll.delete_one({"_id": doc["_id"]})

        if event_is_over(event):
            return await self.coll.delete_one({"_id": doc["_id"]})

        offset = doc["offset_seconds"]
        start = event.start_time.timestamp()
        now = time.time()

        # The event was moved later while we weren't listening: push the reminder back.
        due_at = start - offset
        if due_at > now + 60:
            return await self.coll.update_one(
                {"_id": doc["_id"]}, {"$set": {"remind_at": due_at, "event_start": start}}
            )

        # Too late to be useful (bot was offline, or the event moved up).
        if offset > 0 and now >= start:
            return await self.coll.delete_one({"_id": doc["_id"]})
        if offset == 0 and now > start + START_GRACE:
            return await self.coll.delete_one({"_id": doc["_id"]})

        config = doc.get("config", {})
        await channel.send(
            content=config.get("mention") or None,
            embed=build_reminder_embed(config, event, offset),
            allowed_mentions=ALLOW_PINGS,
        )
        await self.coll.delete_one({"_id": doc["_id"]})


async def setup(bot):
    await bot.add_cog(EventReminder(bot))
