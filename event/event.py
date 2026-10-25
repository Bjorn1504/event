"""
Event Reminder plugin
=====================

    .remind <event link> <#channel> <time>, <time>, ...

Reads a Discord scheduled event and posts a reminder embed (name, description,
start time, location, host, interested count, cover image) in the channel you
pick, once for every reminder time you list.

Examples
    .remind https://discord.com/events/123/456 #announcements 1 hour before, 2 hours before
    .remind https://discord.com/events/123/456 #announcements 1 day, 3h, 30 min, at start

Other commands
    .remind list                           show every pending reminder
    .remind cancel <event link or ID>      remove all reminders for an event
    .remind cancel <event link> #channel   remove only that channel's reminders

Reminders are stored in the plugin's database partition, so they survive bot
restarts. If someone edits the event's start time, pending reminders move with
it; if the event is deleted or cancelled, they are dropped.
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
MAX_REMINDERS_PER_COMMAND = 10     # stops accidental spam
START_GRACE = 15 * 60              # an "at start" reminder later than this is dropped
MAX_RETRIES = 3                    # send attempts before a failing reminder is dropped

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
            raise ValueError(
                f"I couldn't understand `{chunk}`. Try something like `1 hour before` or `30 min`."
            )

        total = 0.0
        for number, unit in matches:
            if unit not in UNITS:
                raise ValueError(
                    f"I don't know the time unit `{unit}` in `{chunk}`. "
                    "Use weeks, days, hours or minutes."
                )
            total += float(number) * UNITS[unit]

        seconds = int(round(total))
        if seconds < 60:
            raise ValueError(f"`{chunk}` is too short. The minimum is 1 minute.")
        offsets.add(seconds)

    if not offsets:
        raise ValueError(
            "You didn't give me any reminder times. Example: `1 hour before, 2 hours before`."
        )
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


def event_location(event: discord.ScheduledEvent) -> Optional[str]:
    channel = getattr(event, "channel", None)
    if channel is not None:
        return channel.mention
    location = getattr(event, "location", None)
    if location is None:
        return None
    return getattr(location, "mention", None) or str(location)


def build_event_embed(event: discord.ScheduledEvent, color: Optional[discord.Color] = None):
    start = int(event.start_time.timestamp())
    embed = discord.Embed(title=event.name, url=event.url, color=color or discord.Color.blurple())

    if event.description:
        text = event.description.strip()
        embed.description = text[:1000] + ("…" if len(text) > 1000 else "")

    embed.add_field(name="🕒 Starts", value=f"<t:{start}:F>\n(<t:{start}:R>)", inline=True)
    if event.end_time:
        embed.add_field(name="🏁 Ends", value=f"<t:{int(event.end_time.timestamp())}:F>", inline=True)

    location = event_location(event)
    if location:
        embed.add_field(name="📍 Location", value=location, inline=False)
    if event.creator_id:
        embed.add_field(name="👤 Host", value=f"<@{event.creator_id}>", inline=True)
    if getattr(event, "user_count", None) is not None:
        embed.add_field(name="✋ Interested", value=str(event.user_count), inline=True)

    cover = getattr(event, "cover_image", None)
    if cover:
        embed.set_image(url=cover.url)
    return embed


def event_is_over(event: discord.ScheduledEvent) -> bool:
    return event.status in (discord.EventStatus.completed, discord.EventStatus.cancelled)


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #
class Remind(commands.Cog):
    """Post reminders for Discord scheduled events."""

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
    @commands.group(name="remind", invoke_without_command=True)
    async def remind(self, ctx, event_link: str, channel: discord.TextChannel, *, timings: str):
        """
        Schedule reminders for a Discord event.

        Usage: remind <event link> <channel> <time>, <time>, ...

        Separate reminders with commas. Examples:
        remind https://discord.com/events/1/2 #announcements 1 hour before, 2 hours before
        remind https://discord.com/events/1/2 #announcements 1 day, 3h, 30 min, at start

        Running it again for the same event and channel replaces the old reminders,
        which is how you change the timing.
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
            offsets = parse_offsets(timings)
        except ValueError as exc:
            return await ctx.send(f"❌ {exc}")
        if len(offsets) > MAX_REMINDERS_PER_COMMAND:
            return await ctx.send(f"❌ That's a lot of reminders. The limit is {MAX_REMINDERS_PER_COMMAND} per event.")

        try:
            event = await guild.fetch_scheduled_event(event_id, with_counts=True)
        except discord.NotFound:
            return await ctx.send("❌ I couldn't find that event. Is the link right, and is the event still there?")
        except discord.HTTPException:
            logger.exception("Could not fetch scheduled event %s", event_id)
            return await ctx.send("❌ Discord returned an error while I was reading that event. Try again in a moment.")

        if event_is_over(event):
            return await ctx.send("❌ That event has already ended or been cancelled.")

        perms = channel.permissions_for(channel.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await ctx.send(
                f"❌ I need **View Channel**, **Send Messages** and **Embed Links** in {channel.mention}."
            )

        start_ts = event.start_time.timestamp()
        now = time.time()
        scheduled, skipped = [], []
        for offset in offsets:
            remind_at = start_ts - offset
            (scheduled if remind_at > now else skipped).append((offset, remind_at))

        if not scheduled:
            return await ctx.send(
                "❌ All of those times have already passed for this event "
                f"(it starts <t:{int(start_ts)}:R>), so nothing was scheduled."
            )

        # Same event + same channel = replace, so re-running the command edits the timing.
        key = {"guild_id": guild.id, "event_id": event.id, "channel_id": channel.id}
        await self.coll.delete_many(key)
        await self.coll.insert_many(
            [
                {
                    **key,
                    "event_name": event.name,
                    "event_start": start_ts,
                    "offset_seconds": offset,
                    "remind_at": remind_at,
                    "created_by": ctx.author.id,
                    "attempts": 0,
                }
                for offset, remind_at in scheduled
            ]
        )

        lines = [
            f"• **{offset_label(offset)}** → <t:{int(remind_at)}:f> (<t:{int(remind_at)}:R>)"
            for offset, remind_at in scheduled
        ]
        embed = discord.Embed(
            title="✅ Reminders scheduled",
            description=f"**[{event.name}]({event.url})**\nPosting in {channel.mention}\n\n" + "\n".join(lines),
            color=discord.Color.green(),
        )
        if skipped:
            embed.add_field(
                name="Skipped (already passed)",
                value="\n".join(f"• {offset_label(offset)}" for offset, _ in skipped),
                inline=False,
            )
        embed.set_footer(text="Run the command again with new times to change them.")
        await ctx.send(embed=embed)

    @remind.command(name="list")
    async def remind_list(self, ctx):
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

    @remind.command(name="cancel")
    async def remind_cancel(self, ctx, event: str, channel: Optional[discord.TextChannel] = None):
        """
        Cancel reminders for an event.

        Usage: remind cancel <event link or ID> [channel]
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
    # Keep reminders in sync with the event
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
            event = await guild.fetch_scheduled_event(doc["event_id"], with_counts=True)
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

        if offset == 0:
            content = f"🎉 **{event.name}** is starting now!"
        else:
            content = f"⏰ **{event.name}** starts <t:{int(start)}:R>!"

        await channel.send(content=content, embed=build_event_embed(event))
        await self.coll.delete_one({"_id": doc["_id"]})


async def setup(bot):
    await bot.add_cog(Remind(bot))
