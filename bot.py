import os
import datetime
import sqlite3
import requests
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# Load configuration
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID'))
CLIST_USER = os.getenv('CLIST_USERNAME')
CLIST_KEY = os.getenv('CLIST_API_KEY')

# Clist resource IDs: 1 = Codeforces, 2 = CodeChef, 93 = AtCoder
RESOURCE_IDS = "1,2,93"
PLATFORM_MAPPING = {
    "codeforces.com": "Codeforces",
    "codechef.com": "CodeChef",
    "atcoder.jp": "AtCoder"
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------------------------------------------------------
# DATABASE SETUP
# -------------------------------------------------------------------------
DB_FILE = "reminders.db"

def init_db():
    """Initializes the local SQLite database to store sent reminder keys."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_alerts (
            alert_id TEXT PRIMARY KEY,
            sent_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

def has_alert_been_sent(alert_id):
    """Checks the database to see if this specific alert type was sent."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM sent_alerts WHERE alert_id = ?", (alert_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def record_sent_alert(alert_id):
    """Saves the alert ID to the database so it is never repeated."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cursor.execute("INSERT OR IGNORE INTO sent_alerts (alert_id, sent_at) VALUES (?, ?)", (alert_id, now_str))
    conn.commit()
    conn.close()

# -------------------------------------------------------------------------
# API FETCHING
# -------------------------------------------------------------------------
def fetch_upcoming_contests():
    """Fetches and strictly filters CP contests based on specific criteria."""
    if not CLIST_USER or not CLIST_KEY:
        print("Error: CLIST_USERNAME or CLIST_API_KEY is missing from .env")
        return []

    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    
    url = (
        f"https://clist.by/api/v1/contest/?"
        f"username={CLIST_USER}&"
        f"api_key={CLIST_KEY}&"
        f"limit=100&"
        f"start__gte={now_utc}&"
        f"order_by=start&"
        f"resource_id__in={RESOURCE_IDS}"
    )
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            clist_contests = data.get('objects', [])
            
            normalized_contests = []
            for c in clist_contests:
                title = c.get('event', '')
                title_lower = title.lower()
                site_domain = c.get('resource', {}).get('name', '').lower()
                
                is_valid = False

                if "codeforces.com" in site_domain:
                    if any(div in title_lower for div in ["div. 3"]):
                        is_valid = True
                elif "codechef.com" in site_domain:
                    if "starters" in title_lower:
                        is_valid = True
                elif "atcoder.jp" in site_domain:
                    if any(abc in title_lower for abc in ["beginner", "abc"]):
                        is_valid = True

                if is_valid:
                    normalized_contests.append({
                        'name': title,
                        'site': site_domain,
                        'start_time': c.get('start'),  
                        'duration': c.get('duration'), 
                        'url': c.get('href')
                    })
            return normalized_contests
    except Exception as e:
        print(f"Error fetching data from Clist: {e}")
    return []

# -------------------------------------------------------------------------
# BACKGROUND TASK AUTOMATION
# -------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    init_db()  # Initialize database storage file
    reminder_scheduler.start()

@tasks.loop(minutes=1)
async def reminder_scheduler():
    """Background monitoring loop handling catch-ups and final alerts."""
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    contests = fetch_upcoming_contests()
    now = datetime.datetime.now(datetime.timezone.utc)

    for contest in contests:
        try:
            start_time_str = contest['start_time'] + '+00:00'
            start_time = datetime.datetime.fromisoformat(start_time_str)
        except ValueError:
            continue

        time_delta = start_time - now
        time_to_start_minutes = time_delta.total_seconds() / 60

        # Resolve pretty name for display
        display_site = "Unknown Platform"
        for domain, pretty_name in PLATFORM_MAPPING.items():
            if domain in contest['site']:
                display_site = pretty_name

        unix_timestamp = int(start_time.timestamp())

        # -------------------------------------------------------------------------
        # WINDOW 1: Early Announcement / Recovery Window (Anywhere within 2 Days)
        # -------------------------------------------------------------------------
        # This catch-all identity registers if the general info was posted
        general_info_id = f"info_{contest['name']}_{contest['start_time']}"
        
        # If the contest starts within 48 hours (2880 mins) and we HAVEN'T posted it yet
        if 0 < time_to_start_minutes <= 2880:
            if not has_alert_been_sent(general_info_id):
                embed = discord.Embed(
                    title=f"📅 Contest Update: {contest['name']}",
                    url=contest['url'],
                    description="Found an upcoming match scheduled within the next 2 days!",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Platform", value=display_site, inline=True)
                embed.add_field(name="Duration", value=f"{float(contest['duration'])/3600:.1f} hours", inline=True)
                embed.add_field(name="Start Time", value=f"<t:{unix_timestamp}:F> (<t:{unix_timestamp}:R>)", inline=False)
                
                await channel.send(content="@everyone", embed=embed)
                record_sent_alert(general_info_id) # Log permanently to database

        # -------------------------------------------------------------------------
        # WINDOW 2: Final Critical Alert (Lock-step at 1 hour out)
        # -------------------------------------------------------------------------
        final_reminder_id = f"1hour_{contest['name']}_{contest['start_time']}"
        
        # Runs strictly when the clock hits the 1-hour marker
        if 10 <= time_to_start_minutes <= 60:
            if not has_alert_been_sent(final_reminder_id):
                embed = discord.Embed(
                    title=f"🚨 Starting Soon: {contest['name']}",
                    url=contest['url'],
                    description="This contest starts in **1 hour**! Warm up your IDEs.",
                    color=discord.Color.gold()
                )
                embed.add_field(name="Platform", value=display_site, inline=True)
                embed.add_field(name="Duration", value=f"{float(contest['duration'])/3600:.1f} hours", inline=True)
                embed.add_field(name="Start Time", value=f"<t:{unix_timestamp}:F> (<t:{unix_timestamp}:R>)", inline=False)
                
                await channel.send(content="@here", embed=embed)
                record_sent_alert(final_reminder_id) # Log permanently to database

@bot.command(name="contests")
async def show_contests(ctx):
    """Manual lookup command."""
    contests = fetch_upcoming_contests()
    if not contests:
        await ctx.send("No upcoming targeted matches available.")
        return

    embed = discord.Embed(title="Upcoming CP Contests", color=discord.Color.blue())
    count = 0
    for c in contests[:10]:  
        try:
            start_time_str = c['start_time'] + '+00:00'
            start_time = datetime.datetime.fromisoformat(start_time_str)
            unix_timestamp = int(start_time.timestamp())
            
            display_site = "Unknown"
            for domain, pretty_name in PLATFORM_MAPPING.items():
                if domain in c['site']:
                    display_site = pretty_name

            embed.add_field(
                name=f"{display_site} | {c['name']}",
                value=f"Starts: <t:{unix_timestamp}:f> (<t:{unix_timestamp}:R>)\n[Link]({c['url']})",
                inline=False
            )
            count += 1
        except ValueError:
            continue
    await ctx.send(embed=embed)

bot.run(TOKEN)