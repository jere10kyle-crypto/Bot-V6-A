import discord
from discord.ext import commands, tasks
import json
import os
import time
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN    = os.getenv("DISCORD_TOKEN", "")
OWNER_ID = os.getenv("OWNER_ID", "").strip()
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# ── Per-guild data helpers ────────────────────────────────────────────────────
def guild_dir(guild_id):
    d = os.path.join(DATA_DIR, str(guild_id))
    os.makedirs(d, exist_ok=True)
    return d

def load_guild(guild_id, filename, default):
    path = os.path.join(guild_dir(guild_id), filename)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_guild(guild_id, filename, data):
    path = os.path.join(guild_dir(guild_id), filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ── In-memory per-guild caches ────────────────────────────────────────────────
_guild_strikes: dict = {}
_guild_logs: dict    = {}
_guild_names: dict   = {}
_guild_words: dict   = {}

def get_strikes(guild_id):
    if guild_id not in _guild_strikes:
        _guild_strikes[guild_id] = load_guild(guild_id, "strikes.json", {})
    return _guild_strikes[guild_id]

def get_logs(guild_id):
    if guild_id not in _guild_logs:
        _guild_logs[guild_id] = load_guild(guild_id, "logs.json", [])
    return _guild_logs[guild_id]

def get_user_names(guild_id):
    if guild_id not in _guild_names:
        _guild_names[guild_id] = load_guild(guild_id, "user_names.json", {})
    return _guild_names[guild_id]

def get_banned_words(guild_id):
    raw = load_guild(guild_id, "banned_words.json", {})
    if isinstance(raw, list):
        raw = {w: 1 for w in raw}
        save_guild(guild_id, "banned_words.json", raw)
    _guild_words[guild_id] = raw
    return _guild_words[guild_id]

# Anti-spam / anti-raid tracking (in-memory, keyed by (guild_id, uid))
message_times      = defaultdict(list)
warned_users       = set()
word_warning_count = defaultdict(int)
word_repeat_times  = defaultdict(lambda: defaultdict(list))

# Anti-raid state
guild_join_times: dict = defaultdict(list)
raid_active: set       = set()
raid_unlock_tasks: dict = {}

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Owner check ───────────────────────────────────────────────────────────────
def is_owner(ctx) -> bool:
    return bool(OWNER_ID) and str(ctx.author.id) == OWNER_ID

def owner_or_perm(**perms):
    async def predicate(ctx):
        if is_owner(ctx):
            return True
        required = discord.Permissions(**perms)
        return ctx.author.guild_permissions >= required
    return commands.check(predicate)

# ── Logging helper ────────────────────────────────────────────────────────────
def add_log(guild_id, action, user, reason, moderator="AutoMod"):
    logs = get_logs(guild_id)
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "action":    action,
        "user":      str(user),
        "reason":    reason,
        "moderator": str(moderator),
    }
    logs.append(entry)
    if len(logs) > 500:
        logs.pop(0)
    save_guild(guild_id, "logs.json", logs)
    if hasattr(user, "id") and hasattr(user, "display_name"):
        names = get_user_names(guild_id)
        names[str(user.id)] = user.display_name
        save_guild(guild_id, "user_names.json", names)

DEFAULT_SETTINGS = {
    "tier1_strikes": 1,  "tier1_minutes": 5,
    "tier2_strikes": 2,  "tier2_minutes": 15,
    "tier3_strikes": 3,  "tier3_minutes": 60,
    "tier4_strikes": 4,  "tier4_minutes": 1440,
    "tier5_strikes": 5,  "tier5_minutes": 40320,
    "spam_word_limit":   5,
    "spam_word_window":  10,
    "spam_word_tier":    2,
    "raid_join_limit":       5,
    "raid_join_window":      10,
    "raid_lockdown_minutes": 10,
    "raid_timeout_minutes":  10,
}

TIERS = [5, 4, 3, 2, 1]

def get_settings(guild_id):
    return {**DEFAULT_SETTINGS, **load_guild(guild_id, "settings.json", {})}

# ── Strike / timeout helpers ──────────────────────────────────────────────────
async def apply_timeout(member, mins, reason):
    mins = min(mins, 40320)
    until = discord.utils.utcnow() + timedelta(minutes=mins)
    try:
        await member.timeout(until, reason=reason)
        return True
    except discord.Forbidden:
        print(f"[AutoMod] ⚠️  TIMEOUT FAILED for {member} — bot needs 'Timeout Members' permission and a higher role")
        return False
    except discord.HTTPException as e:
        print(f"[AutoMod] ⚠️  TIMEOUT HTTP ERROR for {member}: {e}")
        return False

async def add_strike(guild, member, reason):
    gid = guild.id
    uid = str(member.id)
    strikes = get_strikes(gid)
    strikes[uid] = strikes.get(uid, 0) + 1
    save_guild(gid, "strikes.json", strikes)
    add_log(gid, "STRIKE", member, reason)

    count = strikes[uid]
    s = get_settings(gid)
    for t in TIERS:
        if count >= s[f"tier{t}_strikes"]:
            mins = min(s[f"tier{t}_minutes"], 40320)
            ok = await apply_timeout(member, mins,
                reason=f"Tier {t} – {count} strike(s) – {mins}m mute")
            if ok:
                add_log(gid, f"MUTE_T{t}", member,
                    f"Tier {t} triggered at {count} strike(s) ({mins}m)")
            break
    return count

# ── Spam / anti-raid detection ────────────────────────────────────────────────
SPAM_LIMIT  = 5
SPAM_WINDOW = 5

async def send_dm(user, content):
    try:
        await user.send(content)
    except (discord.Forbidden, discord.HTTPException):
        pass

async def purge_user_messages(channel, user, limit=50):
    try:
        await channel.purge(limit=limit, check=lambda m: m.author.id == user.id)
    except (discord.Forbidden, discord.HTTPException):
        pass

async def check_spam(message):
    gid = message.guild.id
    uid = str(message.author.id)
    key = (gid, uid)
    now = time.time()
    message_times[key] = [t for t in message_times[key] if now - t < SPAM_WINDOW]
    message_times[key].append(now)
    if len(message_times[key]) < SPAM_LIMIT:
        warned_users.discard(key)
    if len(message_times[key]) >= SPAM_LIMIT:
        if key not in warned_users:
            warned_users.add(key)
            message_times[key] = []
            await purge_user_messages(message.channel, message.author)
            await send_dm(message.author,
                f"⚠️ **AutoMod — {message.guild.name}**\n"
                f"You were flagged for spamming and your messages were removed. "
                f"Please slow down or you will be muted.")
            await add_strike(message.guild, message.author, "Spam detection")
            return True
    return False

async def check_word_spam(message):
    gid    = message.guild.id
    uid    = str(message.author.id)
    key    = (gid, uid)
    s      = get_settings(gid)
    limit  = s["spam_word_limit"]
    window = s["spam_word_window"]
    tier   = max(1, min(5, s["spam_word_tier"]))
    now    = time.time()
    for word in message.content.lower().split():
        if not word:
            continue
        times = word_repeat_times[key][word]
        times = [t for t in times if now - t < window]
        times.append(now)
        word_repeat_times[key][word] = times
        if len(times) >= limit:
            word_repeat_times[key][word] = []
            s2   = get_settings(gid)
            mins = min(s2[f"tier{tier}_minutes"], 40320)
            await purge_user_messages(message.channel, message.author)
            await send_dm(message.author,
                f"🔁 **AutoMod — {message.guild.name}**\n"
                f"You were muted for repeating the same word too many times. "
                f"Tier {tier} mute applied ({mins} minute(s)). Your messages were removed.")
            ok = await apply_timeout(message.author, mins,
                reason=f"Word-repeat spam: '{word}' x{limit} in {window}s (Tier {tier})")
            if ok:
                add_log(gid, f"MUTE_T{tier}", message.author,
                    f"Word-repeat spam: '{word}' x{limit} in {window}s ({mins}m)")
            strikes = get_strikes(gid)
            strikes[uid] = strikes.get(uid, 0) + 1
            save_guild(gid, "strikes.json", strikes)
            add_log(gid, "STRIKE", message.author, f"Word-repeat spam: '{word}'")
            return True
    return False

async def check_banned_words(message):
    gid     = message.guild.id
    uid     = str(message.author.id)
    key     = (gid, uid)
    content = message.content.lower()
    bw      = get_banned_words(gid)
    for word, tier in bw.items():
        if word.lower() in content:
            word_warning_count[key] += 1
            await message.delete()
            strikes = get_strikes(gid)
            strikes[uid] = strikes.get(uid, 0) + 1
            save_guild(gid, "strikes.json", strikes)
            add_log(gid, "STRIKE", message.author, f"Tier {tier} banned word: '{word}'")
            if word_warning_count[key] >= 2:
                s    = get_settings(gid)
                mins = min(s[f"tier{tier}_minutes"], 40320)
                await send_dm(message.author,
                    f"🚫 **AutoMod — {message.guild.name}**\n"
                    f"Your message was deleted for containing a Tier {tier} banned word. "
                    f"A mute of {mins} minute(s) has been applied.")
                ok = await apply_timeout(message.author, mins,
                    reason=f"Tier {tier} banned word: '{word}' ({mins}m)")
                if ok:
                    add_log(gid, f"MUTE_T{tier}", message.author,
                        f"Tier {tier} word: '{word}' ({mins}m)")
            else:
                await send_dm(message.author,
                    f"⚠️ **AutoMod — {message.guild.name}**\n"
                    f"Your message was deleted — it contained a Tier {tier} banned word. "
                    f"One more and you will be muted.")
            return True
    return False

# ── Anti-raid detection ───────────────────────────────────────────────────────
async def _lock_guild(guild: discord.Guild):
    everyone = guild.default_role
    locked = 0
    for ch in guild.text_channels:
        try:
            overwrite = ch.overwrites_for(everyone)
            overwrite.send_messages = False
            await ch.set_permissions(everyone, overwrite=overwrite, reason="AutoMod anti-raid lockdown")
            locked += 1
        except (discord.Forbidden, discord.HTTPException):
            pass
    print(f"[Raid] 🔒 Locked {locked} channel(s) in {guild.name}")

async def _unlock_guild(guild: discord.Guild):
    everyone = guild.default_role
    unlocked = 0
    for ch in guild.text_channels:
        try:
            overwrite = ch.overwrites_for(everyone)
            overwrite.send_messages = None
            await ch.set_permissions(everyone, overwrite=overwrite, reason="AutoMod anti-raid lockdown lifted")
            unlocked += 1
        except (discord.Forbidden, discord.HTTPException):
            pass
    raid_active.discard(guild.id)
    raid_unlock_tasks.pop(guild.id, None)
    add_log(guild.id, "RAID_UNLOCK", "AutoMod",
        f"Lockdown lifted — {unlocked} channel(s) re-opened in {guild.name}")
    print(f"[Raid] 🔓 Unlocked {unlocked} channel(s) in {guild.name}")

async def _schedule_unlock(guild: discord.Guild, mins: int):
    await asyncio.sleep(mins * 60)
    if guild.id in raid_active:
        await _unlock_guild(guild)
        for ch in guild.text_channels:
            try:
                await ch.send("✅ **Anti-Raid** — Lockdown lifted automatically. Chat is open again.")
                break
            except (discord.Forbidden, discord.HTTPException):
                pass

async def check_raid(member: discord.Member):
    guild = member.guild
    gid   = guild.id
    if gid in raid_active:
        s = get_settings(gid)
        mins = s["raid_timeout_minutes"]
        await apply_timeout(member, mins, reason="Joined during anti-raid lockdown")
        await send_dm(member,
            f"🛡️ **{guild.name}** is currently under an anti-raid lockdown.\n"
            f"You have been timed out for {mins} minute(s). Please try again later.")
        add_log(gid, "RAID_JOIN_MUTED", member, f"Joined during lockdown — {mins}m timeout")
        return

    s   = get_settings(gid)
    now = time.time()
    guild_join_times[gid] = [t for t in guild_join_times[gid] if now - t < s["raid_join_window"]]
    guild_join_times[gid].append(now)

    if len(guild_join_times[gid]) >= s["raid_join_limit"]:
        guild_join_times[gid] = []
        raid_active.add(gid)
        add_log(gid, "RAID_DETECTED", "AutoMod",
            f"{s['raid_join_limit']} joins in {s['raid_join_window']}s — lockdown triggered in {guild.name}")
        print(f"[Raid] ⚠️  Raid detected in {guild.name} — locking down")

        await _lock_guild(guild)

        for ch in guild.text_channels:
            try:
                await ch.send(
                    f"🚨 **Anti-Raid Mode Activated** — {s['raid_join_limit']} accounts joined in "
                    f"{s['raid_join_window']} seconds. Chat is locked. "
                    f"Lockdown lifts in **{s['raid_lockdown_minutes']} minute(s)**.")
                break
            except (discord.Forbidden, discord.HTTPException):
                pass

        mins   = s["raid_timeout_minutes"]
        cutoff = now - s["raid_join_window"]
        for m in guild.members:
            if m.bot or m == member:
                continue
            if m.joined_at and m.joined_at.timestamp() >= cutoff:
                await apply_timeout(m, mins, reason="Anti-raid: mass join detected")
                await send_dm(m,
                    f"🛡️ **{guild.name}** triggered anti-raid mode due to a surge of joins.\n"
                    f"You have been timed out for {mins} minute(s).")
        await apply_timeout(member, mins, reason="Anti-raid: mass join detected")
        await send_dm(member,
            f"🛡️ **{guild.name}** triggered anti-raid mode due to a surge of joins.\n"
            f"You have been timed out for {mins} minute(s).")

        task = asyncio.create_task(_schedule_unlock(guild, s["raid_lockdown_minutes"]))
        raid_unlock_tasks[gid] = task

# ── Background task: process dashboard-queued strikes ─────────────────────────
@tasks.loop(seconds=10)
async def process_pending_strikes():
    for guild in bot.guilds:
        gid = guild.id

        # Process pending strikes
        pending = load_guild(gid, "pending_strikes.json", [])
        if pending:
            save_guild(gid, "pending_strikes.json", [])
            for entry in pending:
                uid    = str(entry.get("user_id", ""))
                reason = entry.get("reason", "Dashboard strike")
                if not uid:
                    continue
                member = guild.get_member(int(uid))
                if member:
                    await add_strike(guild, member, reason)
                    print(f"[AutoMod] Dashboard strike processed for {member} in {guild.name}")
                else:
                    print(f"[AutoMod] ⚠️  Pending strike: user {uid} not found in {guild.name}")

        # Process pending unmutes
        unmutes = load_guild(gid, "pending_unmutes.json", [])
        if unmutes:
            save_guild(gid, "pending_unmutes.json", [])
            for entry in unmutes:
                uid    = str(entry.get("user_id", ""))
                reason = entry.get("reason", "Dashboard unmute")
                if not uid:
                    continue
                member = guild.get_member(int(uid))
                if member:
                    try:
                        await member.timeout(None, reason=reason)
                        add_log(gid, "UNMUTE", member, reason)
                        print(f"[AutoMod] Unmuted {member} in {guild.name} via dashboard")
                    except Exception as e:
                        print(f"[AutoMod] ⚠️  Could not unmute {uid} in {guild.name}: {e}")

@process_pending_strikes.before_loop
async def before_pending():
    await bot.wait_until_ready()

# ── Background task: reset strikes every 12 hours ─────────────────────────────
@tasks.loop(hours=12)
async def reset_strikes_task():
    for guild in bot.guilds:
        gid   = guild.id
        count = len(get_strikes(gid))
        _guild_strikes[gid] = {}
        save_guild(gid, "strikes.json", {})
        add_log(gid, "AUTO_RESET", "AutoMod",
            f"Scheduled 12-hour strike reset cleared {count} user record(s)")
    print(f"[AutoMod] 🔄 Scheduled strike reset across {len(bot.guilds)} guild(s)")

@reset_strikes_task.before_loop
async def before_reset():
    await bot.wait_until_ready()

# ── Guild info helper ─────────────────────────────────────────────────────────
def save_guild_info(guild: discord.Guild):
    save_guild(guild.id, "guild_info.json", {
        "name":         guild.name,
        "id":           str(guild.id),
        "member_count": guild.member_count,
        "icon":         str(guild.icon) if guild.icon else None,
    })

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print(f"   py-cord {discord.__version__}")
    if OWNER_ID:
        print(f"   Owner bypass active for user ID: {OWNER_ID}")
    else:
        print(f"   ⚠️  No OWNER_ID set — owner bypass disabled")
    if not process_pending_strikes.is_running():
        process_pending_strikes.start()
    if not reset_strikes_task.is_running():
        reset_strikes_task.start()
    print(f"   Strike auto-reset scheduled every 12 hours")
    count = 0
    for guild in bot.guilds:
        save_guild_info(guild)
        for member in guild.members:
            if not member.bot:
                names = get_user_names(guild.id)
                names[str(member.id)] = member.display_name
                count += 1
        save_guild(guild.id, "user_names.json", get_user_names(guild.id))
        print(f"   [{guild.name}] ready")
    print(f"   Cached {count} member name(s) across {len(bot.guilds)} guild(s)")

@bot.event
async def on_guild_join(guild: discord.Guild):
    save_guild_info(guild)
    names = get_user_names(guild.id)
    for member in guild.members:
        if not member.bot:
            names[str(member.id)] = member.display_name
    save_guild(guild.id, "user_names.json", names)
    add_log(guild.id, "BOT_JOIN", "AutoMod", f"Bot added to {guild.name}")
    print(f"[AutoMod] ➕ Joined new guild: {guild.name} ({guild.id})")
    if OWNER_ID:
        try:
            owner = await bot.fetch_user(int(OWNER_ID))
            await owner.send(
                f"✅ **Bot joined a new server!**\n"
                f"**Server:** {guild.name}\n"
                f"**Members:** {guild.member_count}\n"
                f"**Server ID:** `{guild.id}`\n\n"
                f"You have full command access in this server.")
        except Exception:
            pass

async def handle_owner_dm(message):
    cmd = message.content.strip().lower()
    if cmd in ("!unmute", "!free", "!unmuteme"):
        unmuted = []
        not_muted = []
        for guild in bot.guilds:
            member = guild.get_member(int(OWNER_ID))
            if member:
                if member.is_timed_out():
                    try:
                        await member.timeout(None, reason="Owner self-unmute via DM")
                        add_log(guild.id, "UNMUTE", member, "Owner self-unmute via DM")
                        unmuted.append(guild.name)
                    except Exception as e:
                        await message.channel.send(f"❌ Failed to unmute in **{guild.name}**: {e}")
                else:
                    not_muted.append(guild.name)
        if unmuted:
            await message.channel.send(f"✅ Unmuted you in: **{', '.join(unmuted)}**")
        else:
            await message.channel.send("ℹ️ You're not currently muted in any server I'm in.")
    elif cmd in ("!strikes", "!mystrike", "!mystrikes"):
        lines = []
        for guild in bot.guilds:
            count = get_strikes(guild.id).get(OWNER_ID, 0)
            lines.append(f"**{guild.name}:** {count} strike(s)")
        await message.channel.send("⚠️ **Your strikes:**\n" + "\n".join(lines) if lines else "No servers found.")
    elif cmd == "!help":
        await message.channel.send(
            "**Owner DM Commands:**\n"
            "`!unmute` — Remove your timeout from every server\n"
            "`!mystrikes` — Check your strike count across all servers")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    # Handle owner DMs (works even when muted in a server)
    if not message.guild:
        if OWNER_ID and str(message.author.id) == OWNER_ID:
            await handle_owner_dm(message)
        return
    if await check_banned_words(message):
        return
    if await check_word_spam(message):
        return
    if await check_spam(message):
        return
    await bot.process_commands(message)

# ── Welcome message ───────────────────────────────────────────────────────────
DEFAULT_WELCOME = {
    "enabled":    False,
    "dm":         True,
    "channel_id": "",
    "message":    "👋 Welcome to **{server}**, {user}!\nPlease read the rules and enjoy your stay.",
}

def get_welcome(guild_id):
    return {**DEFAULT_WELCOME, **load_guild(guild_id, "welcome.json", {})}

def format_welcome(text: str, member: discord.Member) -> str:
    return (text
        .replace("{user}",     member.mention)
        .replace("{username}", member.display_name)
        .replace("{server}",   member.guild.name)
        .replace("{count}",    str(member.guild.member_count))
    )

async def send_welcome(member: discord.Member):
    gid = member.guild.id
    w   = get_welcome(gid)
    if not w.get("enabled"):
        return
    text = format_welcome(w["message"], member)
    if w.get("dm"):
        await send_dm(member, text)
    else:
        ch_id = w.get("channel_id", "")
        if ch_id and ch_id.isdigit():
            ch = member.guild.get_channel(int(ch_id))
            if ch:
                try:
                    await ch.send(text)
                except (discord.Forbidden, discord.HTTPException):
                    pass

@bot.event
async def on_member_join(member):
    if not member.bot:
        names = get_user_names(member.guild.id)
        names[str(member.id)] = member.display_name
        save_guild(member.guild.id, "user_names.json", names)
    add_log(member.guild.id, "JOIN", member, "Joined the server")
    if not member.bot:
        await check_raid(member)
        await send_welcome(member)

@bot.event
async def on_application_command_error(ctx, error):
    if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
        await ctx.respond("❌ You don't have permission to use this command.", ephemeral=True)

# ── Slash commands ─────────────────────────────────────────────────────────────
@bot.slash_command(name="mute", description="Mute a member for a given number of minutes")
@owner_or_perm(moderate_members=True)
@discord.option("member", description="The member to mute")
@discord.option("minutes", description="Duration in minutes (default 15)", default=15)
async def mute(ctx: discord.ApplicationContext, member: discord.Member, minutes: int = 15):
    ok = await apply_timeout(member, minutes, reason=f"Muted by {ctx.author}")
    if ok:
        add_log(ctx.guild.id, "MUTE", member, f"Manual mute {minutes}m by {ctx.author}")
        await ctx.respond(f"🔇 {member.mention} muted for {minutes} minute(s).", ephemeral=True)
    else:
        await ctx.respond(
            f"❌ Could not mute {member.mention} — ensure the bot has **Timeout Members** permission "
            f"and that {member.mention} has a lower role than the bot.", ephemeral=True)

@bot.slash_command(name="unmute", description="Remove a mute from a member")
@owner_or_perm(moderate_members=True)
@discord.option("member", description="The member to unmute")
async def unmute(ctx: discord.ApplicationContext, member: discord.Member):
    try:
        await member.timeout(None, reason=f"Unmuted by {ctx.author}")
        add_log(ctx.guild.id, "UNMUTE", member, f"Manual unmute by {ctx.author}")
        await ctx.respond(f"🔊 {member.mention} unmuted.", ephemeral=True)
    except discord.Forbidden:
        await ctx.respond("❌ Missing permissions to remove timeout.", ephemeral=True)
    except discord.HTTPException as e:
        await ctx.respond(f"❌ Failed: {e}", ephemeral=True)

@bot.slash_command(name="strike", description="Manually add a strike to a member")
@owner_or_perm(moderate_members=True)
@discord.option("member", description="The member to strike")
@discord.option("reason", description="Reason for the strike", default="Manual strike")
async def strike_cmd(ctx: discord.ApplicationContext, member: discord.Member, reason: str = "Manual strike"):
    await ctx.defer(ephemeral=True)
    count    = await add_strike(ctx.guild, member, f"{reason} (by {ctx.author})")
    s        = get_settings(ctx.guild.id)
    tier_hit = next((t for t in TIERS if count >= s[f"tier{t}_strikes"]), None)
    tier_info = f"Tier {tier_hit} mute applied." if tier_hit else ""
    await ctx.followup.send(
        f"⚠️ **{member.display_name}** now has **{count}** strike(s). {tier_info}",
        ephemeral=True)

@bot.slash_command(name="strikes", description="Check how many strikes a user has")
@owner_or_perm(moderate_members=True)
@discord.option("member", description="The member to check")
async def strikes_cmd(ctx: discord.ApplicationContext, member: discord.Member):
    count = get_strikes(ctx.guild.id).get(str(member.id), 0)
    await ctx.respond(f"⚠️ {member.mention} has **{count}** strike(s).", ephemeral=True)

@bot.slash_command(name="resetstrikes", description="Reset all strikes for a user")
@owner_or_perm(administrator=True)
@discord.option("member", description="The member to reset")
async def reset_strikes_cmd(ctx: discord.ApplicationContext, member: discord.Member):
    strikes = get_strikes(ctx.guild.id)
    strikes.pop(str(member.id), None)
    save_guild(ctx.guild.id, "strikes.json", strikes)
    add_log(ctx.guild.id, "RESET_STRIKES", member, f"Reset by {ctx.author}")
    await ctx.respond(f"✅ Strikes reset for {member.mention}.", ephemeral=True)

@bot.slash_command(name="purge", description="Delete the last N messages in this channel")
@owner_or_perm(manage_messages=True)
@discord.option("amount", description="Number of messages to delete (1–200)")
async def purge_cmd(ctx: discord.ApplicationContext, amount: int):
    if amount < 1 or amount > 200:
        await ctx.respond("❌ Amount must be between 1 and 200.", ephemeral=True)
        return
    await ctx.defer(ephemeral=True)
    try:
        deleted = await ctx.channel.purge(limit=amount)
        add_log(ctx.guild.id, "PURGE", ctx.author,
            f"Deleted {len(deleted)} message(s) in #{ctx.channel.name}")
        await ctx.followup.send(f"🗑️ Deleted **{len(deleted)}** message(s).", ephemeral=True)
    except discord.Forbidden:
        await ctx.followup.send("❌ I don't have permission to delete messages here.", ephemeral=True)
    except discord.HTTPException as e:
        await ctx.followup.send(f"❌ Failed: {e}", ephemeral=True)

@bot.slash_command(name="addword", description="Add a word to the banned list")
@owner_or_perm(administrator=True)
@discord.option("word", description="The word to ban")
@discord.option("tier", description="Severity tier 1–5 (default 1)", default=1)
async def addword_cmd(ctx: discord.ApplicationContext, word: str, tier: int = 1):
    gid = ctx.guild.id
    w   = word.lower().strip()
    t   = max(1, min(5, tier))
    bw  = get_banned_words(gid)
    bw[w] = t
    save_guild(gid, "banned_words.json", bw)
    await ctx.respond(f"✅ `{w}` added as Tier {t} banned word.", ephemeral=True)

@bot.slash_command(name="removeword", description="Remove a word from the banned list")
@owner_or_perm(administrator=True)
@discord.option("word", description="The word to remove")
async def removeword_cmd(ctx: discord.ApplicationContext, word: str):
    gid = ctx.guild.id
    w   = word.lower().strip()
    bw  = get_banned_words(gid)
    if w in bw:
        bw.pop(w)
        save_guild(gid, "banned_words.json", bw)
        await ctx.respond(f"✅ `{w}` removed.", ephemeral=True)
    else:
        await ctx.respond(f"❌ `{w}` not in the banned list.", ephemeral=True)

@bot.slash_command(name="panel", description="Open the moderation panel")
@owner_or_perm(moderate_members=True)
async def panel_cmd(ctx: discord.ApplicationContext):
    embed = discord.Embed(
        title="🛡️ Moderation Panel",
        description="Use the buttons below for quick actions.",
        color=0x5865F2,
    )
    embed.add_field(name="Dashboard",
        value="Open the dashboard to manage strikes, words, and settings.", inline=False)
    embed.set_footer(text="AutoMod v2.0 — py-cord")
    await ctx.respond(embed=embed, view=PanelView(ctx.guild.id), ephemeral=True)

@bot.slash_command(name="raidmode", description="Manually enable or disable anti-raid lockdown")
@owner_or_perm(administrator=True)
@discord.option("action", description="on or off", choices=["on", "off"])
async def raidmode_cmd(ctx: discord.ApplicationContext, action: str):
    guild = ctx.guild
    gid   = guild.id
    await ctx.defer(ephemeral=True)
    if action == "on":
        if gid in raid_active:
            await ctx.followup.send("⚠️ Raid mode is already active.", ephemeral=True)
            return
        raid_active.add(gid)
        await _lock_guild(guild)
        s = get_settings(gid)
        add_log(gid, "RAID_MANUAL_ON", ctx.author, f"Manual lockdown activated by {ctx.author}")
        task = asyncio.create_task(_schedule_unlock(guild, s["raid_lockdown_minutes"]))
        raid_unlock_tasks[gid] = task
        for ch in guild.text_channels:
            try:
                await ch.send(
                    f"🚨 **Anti-Raid Mode Activated** by {ctx.author.mention}. "
                    f"Chat locked for **{s['raid_lockdown_minutes']} minute(s)**.")
                break
            except (discord.Forbidden, discord.HTTPException):
                pass
        await ctx.followup.send(
            f"🔒 Lockdown active. All channels locked for {s['raid_lockdown_minutes']} minute(s).",
            ephemeral=True)
    else:
        if gid not in raid_active:
            await ctx.followup.send("ℹ️ Raid mode is not currently active.", ephemeral=True)
            return
        t = raid_unlock_tasks.pop(gid, None)
        if t:
            t.cancel()
        await _unlock_guild(guild)
        add_log(gid, "RAID_MANUAL_OFF", ctx.author, f"Manual lockdown lifted by {ctx.author}")
        for ch in guild.text_channels:
            try:
                await ch.send(
                    f"✅ **Anti-Raid Mode lifted** by {ctx.author.mention}. Chat is open again.")
                break
            except (discord.Forbidden, discord.HTTPException):
                pass
        await ctx.followup.send("🔓 Lockdown lifted. All channels re-opened.", ephemeral=True)

# ── Panel buttons ─────────────────────────────────────────────────────────────
class PanelView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=60)
        self.guild_id = guild_id

    @discord.ui.button(label="📊 View Logs", style=discord.ButtonStyle.primary)
    async def view_logs(self, button: discord.ui.Button, interaction: discord.Interaction):
        logs   = get_logs(self.guild_id)
        recent = logs[-5:] if logs else []
        if not recent:
            await interaction.response.send_message("No logs yet.", ephemeral=True)
            return
        lines = [f"`{e['timestamp'][:19]}` **{e['action']}** – {e['user']} – {e['reason']}"
                 for e in reversed(recent)]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="⚠️ Top Striked", style=discord.ButtonStyle.danger)
    async def top_striked(self, button: discord.ui.Button, interaction: discord.Interaction):
        strikes = get_strikes(self.guild_id)
        if not strikes:
            await interaction.response.send_message("No strikes recorded.", ephemeral=True)
            return
        top   = sorted(strikes.items(), key=lambda x: x[1], reverse=True)[:5]
        lines = [f"<@{uid}>: **{count}** strike(s)" for uid, count in top]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="📝 Banned Words", style=discord.ButtonStyle.secondary)
    async def list_words(self, button: discord.ui.Button, interaction: discord.Interaction):
        bw    = get_banned_words(self.guild_id)
        words = ", ".join(f"`{w}`(T{t})" for w, t in bw.items()) if bw else "None"
        await interaction.response.send_message(f"🚫 Banned: {words}", ephemeral=True)

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN environment variable is not set. Please add it as a secret.")
        exit(1)
    bot.run(TOKEN)
