import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import aiohttp
import hashlib
import time
import asyncio
from typing import List
from datetime import datetime
import os
import ssl

SECRET = 'tB87#kPtkxqOS2'

class PaginationView(discord.ui.View):
    def __init__(self, chunks: List[discord.Embed], author_id: int):
        super().__init__(timeout=7200)
        self.chunks = chunks
        self.current_page = 0
        self.message = None
        self.author_id = author_id
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("You cannot use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.blurple, disabled=True)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_page_change(interaction, -1)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.blurple)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_page_change(interaction, 1)

    async def _handle_page_change(self, interaction: discord.Interaction, change: int):
        self.current_page = max(0, min(self.current_page + change, len(self.chunks) - 1))
        self.update_buttons()
        await self.update_page(interaction)

    def update_buttons(self):
        self.previous_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page == len(self.chunks) - 1

    async def update_page(self, interaction: discord.Interaction):
        embed = self.chunks[self.current_page]
        embed.set_footer(text=f"Page {self.current_page + 1}/{len(self.chunks)}")
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

def fix_rtl(text):
    return f"\u202B{text}\u202C"

class AllianceMemberOperations(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.conn_alliance = sqlite3.connect('db/alliance.sqlite')
        self.c_alliance = self.conn_alliance.cursor()
        
        self.conn_users = sqlite3.connect('db/users.sqlite')
        self.c_users = self.conn_users.cursor()
        
        self.level_mapping = {
            31: "30-1", 32: "30-2", 33: "30-3", 34: "30-4",
            35: "FC 1", 36: "FC 1 - 1", 37: "FC 1 - 2", 38: "FC 1 - 3", 39: "FC 1 - 4",
            40: "FC 2", 41: "FC 2 - 1", 42: "FC 2 - 2", 43: "FC 2 - 3", 44: "FC 2 - 4",
            45: "FC 3", 46: "FC 3 - 1", 47: "FC 3 - 2", 48: "FC 3 - 3", 49: "FC 3 - 4",
            50: "FC 4", 51: "FC 4 - 1", 52: "FC 4 - 2", 53: "FC 4 - 3", 54: "FC 4 - 4",
            55: "FC 5", 56: "FC 5 - 1", 57: "FC 5 - 2", 58: "FC 5 - 3", 59: "FC 5 - 4",
            60: "FC 6", 61: "FC 6 - 1", 62: "FC 6 - 2", 63: "FC 6 - 3", 64: "FC 6 - 4",
            65: "FC 7", 66: "FC 7 - 1", 67: "FC 7 - 2", 68: "FC 7 - 3", 69: "FC 7 - 4",
            70: "FC 8", 71: "FC 8 - 1", 72: "FC 8 - 2", 73: "FC 8 - 3", 74: "FC 8 - 4",
            75: "FC 9", 76: "FC 9 - 1", 77: "FC 9 - 2", 78: "FC 9 - 3", 79: "FC 9 - 4",
            80: "FC 10", 81: "FC 10 - 1", 82: "FC 10 - 2", 83: "FC 10 - 3", 84: "FC 10 - 4"
        }

        self.fl_emojis = {
            range(35, 40): "<:fc1:1326751863764156528>",
            range(40, 45): "<:fc2:1326751886954594315>",
            range(45, 50): "<:fc3:1326751903912034375>",
            range(50, 55): "<:fc4:1326751938674692106>",
            range(55, 60): "<:fc5:1326751952750776331>",
            range(60, 65): "<:fc6:1326751966184869981>",
            range(65, 70): "<:fc7:1326751983939489812>",
            range(70, 75): "<:fc8:1326751996707082240>",
            range(75, 80): "<:fc9:1326752008505528331>",
            range(80, 85): "<:fc10:1326752023001174066>"
        }

        self.log_directory = 'log'
        if not os.path.exists(self.log_directory):
            os.makedirs(self.log_directory)
        self.log_file = os.path.join(self.log_directory, 'alliance_memberlog.txt')
        
        # Rate limiting configuration for dual-API support
        self.api1_url = 'https://wos-giftcode-api.centurygame.com/api/player'
        self.api2_url = 'https://gof-report-api-formal.centurygame.com/api/player'
        self.api1_requests = []  # Timestamps of API1 requests
        self.api2_requests = []  # Timestamps of API2 requests
        self.rate_limit_per_api = 30
        self.rate_limit_window = 60  # seconds
        self.last_api_used = 1  # Track which API was used last
        self.dual_api_mode = False  # Set after availability check
        self.available_apis = []    # List of available API numbers [1] or [1,2]
        self.request_delay = 2.0    # Default for single API
        
        # Operation queue to prevent concurrent member additions
        self.operation_lock = asyncio.Lock()
        self.operation_queue = []
        self.current_operation = None

    def log_message(self, message: str):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"[{timestamp}] {message}\n"
        
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    
    async def _check_apis_availability(self):
        """Check which APIs are available before starting member addition"""
        api_status = {
            "api1_available": False,
            "api2_available": False,
            "api1_url": self.api1_url,
            "api2_url": self.api2_url
        }
        
        # Use a known test FID - we'll use the first one from a typical list
        test_fid = "46765089"
        
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        
        async with aiohttp.ClientSession(connector=connector) as session:
            # Test API 1
            try:
                current_time = int(time.time() * 1000)
                form = f"fid={test_fid}&time={current_time}"
                sign = hashlib.md5((form + SECRET).encode('utf-8')).hexdigest()
                form = f"sign={sign}&{form}"
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}
                
                async with session.post(self.api1_url, headers=headers, data=form, timeout=5) as response:
                    # API is available if we get 200 (success) or 429 (rate limit)
                    api_status["api1_available"] = response.status in [200, 429]
                    self.log_message(f"API1 availability check: Status {response.status}")
            except Exception as e:
                self.log_message(f"API1 availability check failed: {str(e)}")
                api_status["api1_available"] = False
            
            # Test API 2
            try:
                current_time = int(time.time() * 1000)
                form = f"fid={test_fid}&time={current_time}"
                sign = hashlib.md5((form + SECRET).encode('utf-8')).hexdigest()
                form = f"sign={sign}&{form}"
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}
                
                async with session.post(self.api2_url, headers=headers, data=form, timeout=5) as response:
                    api_status["api2_available"] = response.status in [200, 429]
                    self.log_message(f"API2 availability check: Status {response.status}")
            except Exception as e:
                self.log_message(f"API2 availability check failed: {str(e)}")
                api_status["api2_available"] = False
        
        return api_status
    
    def _get_available_api(self):
        """Determine which API to use next based on rate limits"""
        now = time.time()
        
        # Clean old requests outside the rate limit window
        self.api1_requests = [t for t in self.api1_requests if now - t < self.rate_limit_window]
        self.api2_requests = [t for t in self.api2_requests if now - t < self.rate_limit_window]
        
        if not self.dual_api_mode:
            # Single API mode - simpler logic
            api_num = self.available_apis[0] if self.available_apis else 1
            requests = self.api1_requests if api_num == 1 else self.api2_requests
            
            if len(requests) < self.rate_limit_per_api:
                return api_num
            else:
                # Calculate wait time until oldest request expires
                wait_time = self.rate_limit_window - (now - requests[0]) if requests else 0
                return None, max(0, wait_time)
        else:
            # Dual API mode - intelligent switching
            api1_available = 1 in self.available_apis and len(self.api1_requests) < self.rate_limit_per_api
            api2_available = 2 in self.available_apis and len(self.api2_requests) < self.rate_limit_per_api
            
            if api1_available and api2_available:
                # Both available - alternate or use the one with more capacity
                if self.last_api_used == 1:
                    return 2
                else:
                    return 1
            elif api1_available:
                return 1
            elif api2_available:
                return 2
            else:
                # Both at limit - calculate minimum wait time
                wait_time1 = self.rate_limit_window - (now - self.api1_requests[0]) if self.api1_requests else 0
                wait_time2 = self.rate_limit_window - (now - self.api2_requests[0]) if self.api2_requests else 0
                min_wait = min(wait_time1, wait_time2) if self.dual_api_mode else wait_time1
                return None, max(0, min_wait)
    
    def _record_api_request(self, api_num):
        """Record timestamp of API request"""
        now = time.time()
        if api_num == 1:
            self.api1_requests.append(now)
        else:
            self.api2_requests.append(now)
        self.last_api_used = api_num
    
    def _get_wait_time(self):
        """Calculate wait time when both APIs are at limit"""
        now = time.time()
        wait_time1 = self.rate_limit_window - (now - self.api1_requests[0]) if self.api1_requests else 0
        wait_time2 = self.rate_limit_window - (now - self.api2_requests[0]) if self.api2_requests else 0
        return max(0, min(wait_time1, wait_time2))

    def get_fl_emoji(self, fl_level: int) -> str:
        for level_range, emoji in self.fl_emojis.items():
            if fl_level in level_range:
                return emoji
        return "🔥"

    async def handle_member_operations(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="👥 Alliance Member Operations",
            description=(
                "Please select an operation from below:\n\n"
                "**Available Operations:**\n"
                "➕ `Add Member` - Add new members to alliance\n"
                "➖ `Remove Member` - Remove members from alliance\n"
                "📋 `View Members` - View alliance member list\n"
                "🔄 `Transfer Member` - Transfer members to another alliance\n"
                "🏠 `Main Menu` - Return to main menu"
            ),
            color=discord.Color.blue()
        )
        
        embed.set_footer(text="Select an option to continue")

        class MemberOperationsView(discord.ui.View):
            def __init__(self, cog):
                super().__init__()
                self.cog = cog
                self.bot = cog.bot

            @discord.ui.button(
                label="Add Member",
                emoji="➕",
                style=discord.ButtonStyle.success,
                custom_id="add_member",
                row=0
            )
            async def add_member_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    is_admin = False
                    is_initial = 0
                    
                    with sqlite3.connect('db/settings.sqlite') as settings_db:
                        cursor = settings_db.cursor()
                        cursor.execute("SELECT is_initial FROM admin WHERE id = ?", (button_interaction.user.id,))
                        result = cursor.fetchone()
                        
                        if result:
                            is_admin = True
                            is_initial = result[0] if result[0] is not None else 0
                        
                    if not is_admin:
                        await button_interaction.response.send_message(
                            "❌ You don't have permission to use this command.", 
                            ephemeral=True
                        )
                        return

                    alliances, special_alliances, is_global = await self.cog.get_admin_alliances(
                        button_interaction.user.id, 
                        button_interaction.guild_id
                    )
                    
                    if not alliances:
                        await button_interaction.response.send_message(
                            "❌ No alliances found for your permissions.", 
                            ephemeral=True
                        )
                        return

                    special_alliance_text = ""
                    if special_alliances:
                        special_alliance_text = "\n\n**Special Access Alliances**\n"
                        special_alliance_text += "━━━━━━━━━━━━━━━━━━━━━━\n"
                        for _, name in special_alliances:
                            special_alliance_text += f"🔸 {name}\n"
                        special_alliance_text += "━━━━━━━━━━━━━━━━━━━━━━"

                    select_embed = discord.Embed(
                        title="📋 Alliance Selection",
                        description=(
                            "Please select an alliance to add members:\n\n"
                            "**Permission Details**\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👤 **Access Level:** `{'Global Admin' if is_initial == 1 else 'Server Admin'}`\n"
                            f"🔍 **Access Type:** `{'All Alliances' if is_initial == 1 else 'Server + Special Access'}`\n"
                            f"📊 **Available Alliances:** `{len(alliances)}`\n"
                            "━━━━━━━━━━━━━━━━━━━━━━"
                            f"{special_alliance_text}"
                        ),
                        color=discord.Color.green()
                    )

                    alliances_with_counts = []
                    for alliance_id, name in alliances:
                        with sqlite3.connect('db/users.sqlite') as users_db:
                            cursor = users_db.cursor()
                            cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                            member_count = cursor.fetchone()[0]
                            alliances_with_counts.append((alliance_id, name, member_count))

                    view = AllianceSelectView(alliances_with_counts, self.cog)
                    
                    async def select_callback(interaction: discord.Interaction):
                        alliance_id = int(view.current_select.values[0])
                        await interaction.response.send_modal(AddMemberModal(alliance_id))

                    view.callback = select_callback
                    await button_interaction.response.send_message(
                        embed=select_embed,
                        view=view,
                        ephemeral=True
                    )

                except Exception as e:
                    self.log_message(f"Error in add_member_button: {e}")
                    await button_interaction.response.send_message(
                        "An error occurred while processing your request.", 
                        ephemeral=True
                    )

            @discord.ui.button(
                label="Remove Member",
                emoji="➖",
                style=discord.ButtonStyle.danger,
                custom_id="remove_member",
                row=0
            )
            async def remove_member_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    with sqlite3.connect('db/settings.sqlite') as settings_db:
                        cursor = settings_db.cursor()
                        cursor.execute("SELECT is_initial FROM admin WHERE id = ?", (button_interaction.user.id,))
                        admin_result = cursor.fetchone()
                        
                        if not admin_result:
                            await button_interaction.response.send_message(
                                "❌ You are not authorized to use this command.", 
                                ephemeral=True
                            )
                            return
                            
                        is_initial = admin_result[0]

                    alliances, special_alliances, is_global = await self.cog.get_admin_alliances(
                        button_interaction.user.id, 
                        button_interaction.guild_id
                    )
                    
                    if not alliances:
                        await button_interaction.response.send_message(
                            "❌ Your authorized alliance was not found.", 
                            ephemeral=True
                        )
                        return

                    special_alliance_text = ""
                    if special_alliances:
                        special_alliance_text = "\n\n**Special Access Alliances**\n"
                        special_alliance_text += "━━━━━━━━━━━━━━━━━━━━━━\n"
                        for _, name in special_alliances:
                            special_alliance_text += f"🔸 {name}\n"
                        special_alliance_text += "━━━━━━━━━━━━━━━━━━━━━━"

                    select_embed = discord.Embed(
                        title="🗑️ Alliance Selection - Member Deletion",
                        description=(
                            "Please select an alliance to remove members:\n\n"
                            "**Permission Details**\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👤 **Access Level:** `{'Global Admin' if is_initial == 1 else 'Server Admin'}`\n"
                            f"🔍 **Access Type:** `{'All Alliances' if is_initial == 1 else 'Server + Special Access'}`\n"
                            f"📊 **Available Alliances:** `{len(alliances)}`\n"
                            "━━━━━━━━━━━━━━━━━━━━━━"
                            f"{special_alliance_text}"
                        ),
                        color=discord.Color.red()
                    )

                    alliances_with_counts = []
                    for alliance_id, name in alliances:
                        with sqlite3.connect('db/users.sqlite') as users_db:
                            cursor = users_db.cursor()
                            cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                            member_count = cursor.fetchone()[0]
                            alliances_with_counts.append((alliance_id, name, member_count))

                    view = AllianceSelectView(alliances_with_counts, self.cog)
                    
                    async def select_callback(interaction: discord.Interaction):
                        alliance_id = int(view.current_select.values[0])
                        
                        with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                            cursor = alliance_db.cursor()
                            cursor.execute("SELECT name FROM alliance_list WHERE alliance_id = ?", (alliance_id,))
                            alliance_name = cursor.fetchone()[0]
                        
                        with sqlite3.connect('db/users.sqlite') as users_db:
                            cursor = users_db.cursor()
                            cursor.execute("""
                                SELECT fid, nickname, furnace_lv 
                                FROM users 
                                WHERE alliance = ? 
                                ORDER BY furnace_lv DESC, nickname
                            """, (alliance_id,))
                            members = cursor.fetchall()
                            
                        if not members:
                            await interaction.response.send_message(
                                "❌ No members found in this alliance.", 
                                ephemeral=True
                            )
                            return

                        max_fl = max(member[2] for member in members)
                        avg_fl = sum(member[2] for member in members) / len(members)

                        member_embed = discord.Embed(
                            title=f"👥 {alliance_name} -  Member Selection",
                            description=(
                                "```ml\n"
                                "Alliance Statistics\n"
                                "══════════════════════════\n"
                                f"📊 Total Member     : {len(members)}\n"
                                f"⚔️ Highest Level    : {self.cog.level_mapping.get(max_fl, str(max_fl))}\n"
                                f"📈 Average Level    : {self.cog.level_mapping.get(int(avg_fl), str(int(avg_fl)))}\n"
                                "══════════════════════════\n"
                                "```\n"
                                "Select the member you want to delete:"
                            ),
                            color=discord.Color.red()
                        )

                        member_view = MemberSelectView(members, alliance_name, self.cog)
                        
                        async def member_callback(member_interaction: discord.Interaction):
                            selected_value = member_view.current_select.values[0]
                            
                            if selected_value == "all":
                                confirm_embed = discord.Embed(
                                    title="⚠️ Confirmation Required",
                                    description=f"A total of **{len(members)}** members will be deleted.\nDo you confirm?",
                                    color=discord.Color.red()
                                )
                                
                                confirm_view = discord.ui.View()
                                confirm_button = discord.ui.Button(
                                    label="✅ Confirm", 
                                    style=discord.ButtonStyle.danger, 
                                    custom_id="confirm_all"
                                )
                                cancel_button = discord.ui.Button(
                                    label="❌ Cancel", 
                                    style=discord.ButtonStyle.secondary, 
                                    custom_id="cancel_all"
                                )
                                
                                confirm_view.add_item(confirm_button)
                                confirm_view.add_item(cancel_button)

                                async def confirm_callback(confirm_interaction: discord.Interaction):
                                    if confirm_interaction.data["custom_id"] == "confirm_all":
                                        with sqlite3.connect('db/users.sqlite') as users_db:
                                            cursor = users_db.cursor()
                                            cursor.execute("SELECT fid, nickname FROM users WHERE alliance = ?", (alliance_id,))
                                            removed_members = cursor.fetchall()
                                            cursor.execute("DELETE FROM users WHERE alliance = ?", (alliance_id,))
                                            users_db.commit()
                                        
                                        try:
                                            with sqlite3.connect('db/settings.sqlite') as settings_db:
                                                cursor = settings_db.cursor()
                                                cursor.execute("""
                                                    SELECT channel_id 
                                                    FROM alliance_logs 
                                                    WHERE alliance_id = ?
                                                """, (alliance_id,))
                                                alliance_log_result = cursor.fetchone()
                                                
                                                if alliance_log_result and alliance_log_result[0]:
                                                    log_embed = discord.Embed(
                                                        title="🗑️ Mass Member Removal",
                                                        description=(
                                                            f"**Alliance:** {alliance_name}\n"
                                                            f"**Administrator:** {confirm_interaction.user.name} (`{confirm_interaction.user.id}`)\n"
                                                            f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                                                            f"**Total Members Removed:** {len(removed_members)}\n\n"
                                                            "**Removed Members:**\n"
                                                            "```\n" + 
                                                            "\n".join([f"FID{idx+1}: {fid}" for idx, (fid, _) in enumerate(removed_members[:20])]) +
                                                            (f"\n... ve {len(removed_members) - 20} FID more" if len(removed_members) > 20 else "") +
                                                            "\n```"
                                                        ),
                                                        color=discord.Color.red()
                                                    )
                                                    
                                                    try:
                                                        alliance_channel_id = int(alliance_log_result[0])
                                                        alliance_log_channel = self.bot.get_channel(alliance_channel_id)
                                                        if alliance_log_channel:
                                                            await alliance_log_channel.send(embed=log_embed)
                                                    except Exception as e:
                                                        self.log_message(f"Alliance Log Sending Error: {e}")
                                        except Exception as e:
                                            self.log_message(f"Log record error: {e}")
                                        
                                        success_embed = discord.Embed(
                                            title="✅ Members Deleted",
                                            description=f"A total of **{len(removed_members)}** members have been successfully deleted.",
                                            color=discord.Color.green()
                                        )
                                        await confirm_interaction.response.edit_message(embed=success_embed, view=None)
                                    else:
                                        cancel_embed = discord.Embed(
                                            title="❌ Operation Cancelled",
                                            description="Member deletion operation has been cancelled.",
                                            color=discord.Color.orange()
                                        )
                                        await confirm_interaction.response.edit_message(embed=cancel_embed, view=None)

                                confirm_button.callback = confirm_callback
                                cancel_button.callback = confirm_callback
                                
                                await member_interaction.response.edit_message(
                                    embed=confirm_embed,
                                    view=confirm_view
                                )
                            
                            else:
                                try:
                                    selected_fid = selected_value
                                    with sqlite3.connect('db/users.sqlite') as users_db:
                                        cursor = users_db.cursor()
                                        cursor.execute("SELECT nickname FROM users WHERE fid = ?", (selected_fid,))
                                        nickname = cursor.fetchone()[0]
                                        
                                        cursor.execute("DELETE FROM users WHERE fid = ?", (selected_fid,))
                                        users_db.commit()
                                    
                                    try:
                                        with sqlite3.connect('db/settings.sqlite') as settings_db:
                                            cursor = settings_db.cursor()
                                            cursor.execute("""
                                                SELECT channel_id 
                                                FROM alliance_logs 
                                                WHERE alliance_id = ?
                                            """, (alliance_id,))
                                            alliance_log_result = cursor.fetchone()
                                            
                                            if alliance_log_result and alliance_log_result[0]:
                                                log_embed = discord.Embed(
                                                    title="🗑️ Member Removed",
                                                    description=(
                                                        f"**Alliance:** {alliance_name}\n"
                                                        f"**Administrator:** {member_interaction.user.name} (`{member_interaction.user.id}`)\n"
                                                        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                                                        f"**Removed Member:**\n"
                                                        f"👤 **Name:** {nickname}\n"
                                                        f"🆔 **FID:** {selected_fid}"
                                                    ),
                                                    color=discord.Color.red()
                                                )
                                                
                                                try:
                                                    alliance_channel_id = int(alliance_log_result[0])
                                                    alliance_log_channel = self.bot.get_channel(alliance_channel_id)
                                                    if alliance_log_channel:
                                                        await alliance_log_channel.send(embed=log_embed)
                                                except Exception as e:
                                                    self.log_message(f"Alliance Log Sending Error: {e}")
                                    except Exception as e:
                                        self.log_message(f"Log record error: {e}")
                                    
                                    success_embed = discord.Embed(
                                        title="✅ Member Deleted",
                                        description=f"**{nickname}** has been successfully deleted.",
                                        color=discord.Color.green()
                                    )
                                    await member_interaction.response.edit_message(embed=success_embed, view=None)
                                    
                                except Exception as e:
                                    self.log_message(f"Error in member removal: {e}")
                                    await member_interaction.response.send_message(
                                        "❌ An error occurred during member removal.",
                                        ephemeral=True
                                    )

                        member_view.callback = member_callback
                        await interaction.response.edit_message(
                            embed=member_embed,
                            view=member_view
                        )

                    view.callback = select_callback
                    await button_interaction.response.send_message(
                        embed=select_embed,
                        view=view,
                        ephemeral=True
                    )

                except Exception as e:
                    self.log_message(f"Error in remove_member_button: {e}")
                    await button_interaction.response.send_message(
                        "❌ An error occurred during the member deletion process.",
                        ephemeral=True
                    )

            @discord.ui.button(
                label="View Members",
                emoji="👥",
                style=discord.ButtonStyle.primary,
                custom_id="view_members",
                row=0
            )
            async def view_members_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    with sqlite3.connect('db/settings.sqlite') as settings_db:
                        cursor = settings_db.cursor()
                        cursor.execute("SELECT is_initial FROM admin WHERE id = ?", (button_interaction.user.id,))
                        admin_result = cursor.fetchone()
                        
                        if not admin_result:
                            await button_interaction.response.send_message(
                                "❌ You do not have permission to use this command.", 
                                ephemeral=True
                            )
                            return
                            
                        is_initial = admin_result[0]

                    alliances, special_alliances, is_global = await self.cog.get_admin_alliances(
                        button_interaction.user.id, 
                        button_interaction.guild_id
                    )
                    
                    if not alliances:
                        await button_interaction.response.send_message(
                            "❌ No alliance found that you have permission for.", 
                            ephemeral=True
                        )
                        return

                    special_alliance_text = ""
                    if special_alliances:
                        special_alliance_text = "\n\n**Special Access Alliances**\n"
                        special_alliance_text += "━━━━━━━━━━━━━━━━━━━━━━\n"
                        for _, name in special_alliances:
                            special_alliance_text += f"🔸 {name}\n"
                        special_alliance_text += "━━━━━━━━━━━━━━━━━━━━━━"

                    select_embed = discord.Embed(
                        title="👥 Alliance Selection",
                        description=(
                            "Please select an alliance to view members:\n\n"
                            "**Permission Details**\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👤 **Access Level:** `{'Global Admin' if is_initial == 1 else 'Server Admin'}`\n"
                            f"🔍 **Access Type:** `{'All Alliances' if is_initial == 1 else 'Server + Special Access'}`\n"
                            f"📊 **Available Alliances:** `{len(alliances)}`\n"
                            "━━━━━━━━━━━━━━━━━━━━━━"
                            f"{special_alliance_text}"
                        ),
                        color=discord.Color.blue()
                    )

                    alliances_with_counts = []
                    for alliance_id, name in alliances:
                        with sqlite3.connect('db/users.sqlite') as users_db:
                            cursor = users_db.cursor()
                            cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                            member_count = cursor.fetchone()[0]
                            alliances_with_counts.append((alliance_id, name, member_count))

                    view = AllianceSelectView(alliances_with_counts, self.cog)
                    
                    async def select_callback(interaction: discord.Interaction):
                        alliance_id = int(view.current_select.values[0])
                        
                        with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                            cursor = alliance_db.cursor()
                            cursor.execute("SELECT name FROM alliance_list WHERE alliance_id = ?", (alliance_id,))
                            alliance_name = cursor.fetchone()[0]
                        
                        with sqlite3.connect('db/users.sqlite') as users_db:
                            cursor = users_db.cursor()
                            cursor.execute("""
                                SELECT fid, nickname, furnace_lv
                                FROM users 
                                WHERE alliance = ? 
                                ORDER BY furnace_lv DESC, nickname
                            """, (alliance_id,))
                            members = cursor.fetchall()
                        
                        if not members:
                            await interaction.response.send_message(
                                "❌ No members found in this alliance.", 
                                ephemeral=True
                            )
                            return

                        max_fl = max(member[2] for member in members)
                        avg_fl = sum(member[2] for member in members) / len(members)

                        public_embed = discord.Embed(
                            title=f"👥 {alliance_name} - Member List",
                            description=(
                                "```ml\n"
                                "Alliance Statistics\n"
                                "══════════════════════════\n"
                                f"📊 Total Members    : {len(members)}\n"
                                f"⚔️ Highest Level    : {self.cog.level_mapping.get(max_fl, str(max_fl))}\n"
                                f"📈 Average Level    : {self.cog.level_mapping.get(int(avg_fl), str(int(avg_fl)))}\n"
                                "══════════════════════════\n"
                                "```\n"
                                "**Member List**\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                            ),
                            color=discord.Color.blue()
                        )

                        members_per_page = 15
                        member_chunks = [members[i:i + members_per_page] for i in range(0, len(members), members_per_page)]
                        embeds = []

                        for page, chunk in enumerate(member_chunks):
                            embed = public_embed.copy()
                            
                            member_list = ""
                            for idx, (fid, nickname, furnace_lv) in enumerate(chunk, start=page * members_per_page + 1):
                                level = self.cog.level_mapping.get(furnace_lv, str(furnace_lv))
                                member_list += f"**{idx:02d}.** 👤 {nickname}\n└ ⚔️ `FC: {level}`\n\n"

                            embed.description += member_list
                            
                            if len(member_chunks) > 1:
                                embed.set_footer(text=f"Page {page + 1}/{len(member_chunks)}")
                            
                            embeds.append(embed)

                        pagination_view = PaginationView(embeds, interaction.user.id)
                        
                        await interaction.response.edit_message(
                            content="✅ Member list has been generated and posted below.",
                            embed=None,
                            view=None
                        )
                        
                        message = await interaction.channel.send(
                            embed=embeds[0],
                            view=pagination_view if len(embeds) > 1 else None
                        )
                        
                        if pagination_view:
                            pagination_view.message = message

                    view.callback = select_callback
                    await button_interaction.response.send_message(
                        embed=select_embed,
                        view=view,
                        ephemeral=True
                    )

                except Exception as e:
                    self.log_message(f"Error in view_members_button: {e}")
                    if not button_interaction.response.is_done():
                        await button_interaction.response.send_message(
                            "❌ An error occurred while displaying the member list.",
                            ephemeral=True
                        )

            @discord.ui.button(
                label="Main Menu", 
                emoji="🏠", 
                style=discord.ButtonStyle.secondary,
                row=2
            )
            async def main_menu_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                await self.cog.show_main_menu(interaction)

            @discord.ui.button(label="Transfer Member", emoji="🔄", style=discord.ButtonStyle.primary)
            async def transfer_member_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    with sqlite3.connect('db/settings.sqlite') as settings_db:
                        cursor = settings_db.cursor()
                        cursor.execute("SELECT is_initial FROM admin WHERE id = ?", (button_interaction.user.id,))
                        admin_result = cursor.fetchone()
                        
                        if not admin_result:
                            await button_interaction.response.send_message(
                                "❌ You do not have permission to use this command.", 
                                ephemeral=True
                            )
                            return
                            
                        is_initial = admin_result[0]

                    alliances, special_alliances, is_global = await self.cog.get_admin_alliances(
                        button_interaction.user.id, 
                        button_interaction.guild_id
                    )
                    
                    if not alliances:
                        await button_interaction.response.send_message(
                            "❌ No alliance found with your permissions.", 
                            ephemeral=True
                        )
                        return

                    special_alliance_text = ""
                    if special_alliances:
                        special_alliance_text = "\n\n**Special Access Alliances**\n"
                        special_alliance_text += "━━━━━━━━━━━━━━━━━━━━━━\n"
                        for _, name in special_alliances:
                            special_alliance_text += f"🔸 {name}\n"
                        special_alliance_text += "━━━━━━━━━━━━━━━━━━━━━━"

                    
                    select_embed = discord.Embed(
                        title="🔄 Alliance Selection - Member Transfer",
                        description=(
                            "Select the **source** alliance from which you want to transfer members:\n\n"
                            "**Permission Details**\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👤 **Access Level:** `{'Global Admin' if is_initial == 1 else 'Server Admin'}`\n"
                            f"🔍 **Access Type:** `{'All Alliances' if is_initial == 1 else 'Server + Special Access'}`\n"
                            f"📊 **Available Alliances:** `{len(alliances)}`\n"
                            "━━━━━━━━━━━━━━━━━━━━━━"
                            f"{special_alliance_text}"
                        ),
                        color=discord.Color.blue()
                    )

                    alliances_with_counts = []
                    for alliance_id, name in alliances:
                        with sqlite3.connect('db/users.sqlite') as users_db:
                            cursor = users_db.cursor()
                            cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                            member_count = cursor.fetchone()[0]
                            alliances_with_counts.append((alliance_id, name, member_count))

                    view = AllianceSelectView(alliances_with_counts, self.cog)
                    
                    async def source_callback(interaction: discord.Interaction):
                        try:
                            source_alliance_id = int(view.current_select.values[0])
                            
                            
                            with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                                cursor = alliance_db.cursor()
                                cursor.execute("SELECT name FROM alliance_list WHERE alliance_id = ?", (source_alliance_id,))
                                source_alliance_name = cursor.fetchone()[0]
                            
                            with sqlite3.connect('db/users.sqlite') as users_db:
                                cursor = users_db.cursor()
                                cursor.execute("""
                                    SELECT fid, nickname, furnace_lv 
                                    FROM users 
                                    WHERE alliance = ? 
                                    ORDER BY furnace_lv DESC, nickname
                                """, (source_alliance_id,))
                                members = cursor.fetchall()

                            if not members:
                                await interaction.response.send_message(
                                    "❌ No members found in this alliance.", 
                                    ephemeral=True
                                )
                                return

                            max_fl = max(member[2] for member in members)
                            avg_fl = sum(member[2] for member in members) / len(members)

                            
                            member_embed = discord.Embed(
                                title=f"👥 {source_alliance_name} - Member Selection",
                                description=(
                                    "```ml\n"
                                    "Alliance Statistics\n"
                                    "══════════════════════════\n"
                                    f"📊 Total Members    : {len(members)}\n"
                                    f"⚔️ Highest Level    : {self.cog.level_mapping.get(max_fl, str(max_fl))}\n"
                                    f"📈 Average Level    : {self.cog.level_mapping.get(int(avg_fl), str(int(avg_fl)))}\n"
                                    "══════════════════════════\n"
                                    "```\n"
                                    "Select the member to transfer:\n\n"
                                    "**Selection Methods**\n"
                                    "1️⃣ Select member from menu below\n"
                                    "2️⃣ Click 'Select by FID' button and enter FID\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━"
                                ),
                                color=discord.Color.blue()
                            )

                            member_view = MemberSelectView(members, source_alliance_name, self.cog)
                            
                            async def member_callback(member_interaction: discord.Interaction):
                                selected_fid = int(member_view.current_select.values[0])
                                
                                
                                with sqlite3.connect('db/users.sqlite') as users_db:
                                    cursor = users_db.cursor()
                                    cursor.execute("SELECT nickname FROM users WHERE fid = ?", (selected_fid,))
                                    selected_member_name = cursor.fetchone()[0]

                                
                                target_embed = discord.Embed(
                                    title="🎯 Target Alliance Selection",
                                    description=(
                                        f"Select target alliance to transfer "
                                        f"member **{selected_member_name}**:"
                                    ),
                                    color=discord.Color.blue()
                                )

                                target_options = [
                                    discord.SelectOption(
                                        label=f"{name[:50]}",
                                        value=str(alliance_id),
                                        description=f"ID: {alliance_id} | Members: {count}",
                                        emoji="🏰"
                                    ) for alliance_id, name, count in alliances_with_counts
                                    if alliance_id != source_alliance_id
                                ]

                                target_select = discord.ui.Select(
                                    placeholder="🎯 Select target alliance...",
                                    options=target_options
                                )
                                
                                target_view = discord.ui.View()
                                target_view.add_item(target_select)

                                async def target_callback(target_interaction: discord.Interaction):
                                    target_alliance_id = int(target_select.values[0])
                                    
                                    try:
                                        
                                        with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                                            cursor = alliance_db.cursor()
                                            cursor.execute("SELECT name FROM alliance_list WHERE alliance_id = ?", (target_alliance_id,))
                                            target_alliance_name = cursor.fetchone()[0]

                                        with sqlite3.connect('db/users.sqlite') as users_db:
                                            cursor = users_db.cursor()
                                            cursor.execute(
                                                "UPDATE users SET alliance = ? WHERE fid = ?",
                                                (target_alliance_id, selected_fid)
                                            )
                                            users_db.commit()

                                        
                                        success_embed = discord.Embed(
                                            title="✅ Transfer Successful",
                                            description=(
                                                f"👤 **Member:** {selected_member_name}\n"
                                                f"🆔 **FID:** {selected_fid}\n"
                                                f"📤 **Source:** {source_alliance_name}\n"
                                                f"📥 **Target:** {target_alliance_name}"
                                            ),
                                            color=discord.Color.green()
                                        )
                                        
                                        await target_interaction.response.edit_message(
                                            embed=success_embed,
                                            view=None
                                        )
                                        
                                    except Exception as e:
                                        self.log_message(f"Transfer error: {e}")
                                        error_embed = discord.Embed(
                                            title="❌ Error",
                                            description="An error occurred during the transfer operation.",
                                            color=discord.Color.red()
                                        )
                                        await target_interaction.response.edit_message(
                                            embed=error_embed,
                                            view=None
                                        )

                                target_select.callback = target_callback
                                await member_interaction.response.edit_message(
                                    embed=target_embed,
                                    view=target_view
                                )

                            member_view.callback = member_callback
                            await interaction.response.edit_message(
                                embed=member_embed,
                                view=member_view
                            )

                        except Exception as e:
                            self.log_message(f"Source callback error: {e}")
                            await interaction.response.send_message(
                                "❌ An error occurred. Please try again.",
                                ephemeral=True
                            )

                    view.callback = source_callback
                    await button_interaction.response.send_message(
                        embed=select_embed,
                        view=view,
                        ephemeral=True
                    )

                except Exception as e:
                    self.log_message(f"Error in transfer_member_button: {e}")
                    await button_interaction.response.send_message(
                        "❌ An error occurred during the transfer operation.",
                        ephemeral=True
                    )

        view = MemberOperationsView(self)
        await interaction.response.edit_message(embed=embed, view=view)

    async def add_member(self, interaction: discord.Interaction):
        self.c_alliance.execute("SELECT alliance_id, name FROM alliance_list")
        alliances = self.c_alliance.fetchall()
        alliance_options = [discord.SelectOption(label=name, value=str(alliance_id)) for alliance_id, name in alliances]

        select = discord.ui.Select(placeholder="Select an alliance", options=alliance_options)
        view = discord.ui.View()
        view.add_item(select)

        async def select_callback(select_interaction: discord.Interaction):
            alliance_id = select.values[0]
            await select_interaction.response.send_modal(AddMemberModal(alliance_id))

        select.callback = select_callback
        await interaction.response.send_message("Please select an alliance:", view=view, ephemeral=True)

    async def remove_member(self, interaction: discord.Interaction):
        self.c_alliance.execute("SELECT alliance_id, name FROM alliance_list")
        alliances = self.c_alliance.fetchall()
        alliance_options = [discord.SelectOption(label=name, value=str(alliance_id)) for alliance_id, name in alliances]

        select = discord.ui.Select(placeholder="Select an alliance", options=alliance_options)
        view = discord.ui.View()
        view.add_item(select)

        async def select_callback(select_interaction: discord.Interaction):
            alliance_id = select.values[0]
            
            
            self.c_users.execute("SELECT fid, nickname FROM users WHERE alliance = ?", (alliance_id,))
            members = self.c_users.fetchall()
            
            if not members:
                await select_interaction.response.send_message("No members found in this alliance.", ephemeral=True)
                return

            
            member_options = [
                discord.SelectOption(
                    label=f"{nickname[:80]}",  
                    value=str(fid),
                    description=f"FID: {fid}"
                ) for fid, nickname in members
            ]
            
            
            member_options.insert(0, discord.SelectOption(
                label="ALL MEMBERS",
                value="all",
                description="⚠️ Selecting this will remove all members!"
            ))

            member_select = discord.ui.Select(
                placeholder="Select member to remove",
                options=member_options
            )
            member_view = discord.ui.View()
            member_view.add_item(member_select)

            async def member_select_callback(member_interaction: discord.Interaction):
                selected_value = member_select.values[0]
                
                if selected_value == "all":
                    
                    embed = discord.Embed(
                        title="⚠️ Confirmation Required",
                        description=f"Total **{len(members)}** members will be removed.\nDo you confirm?",
                        color=discord.Color.red()
                    )
                    
                    confirm_view = discord.ui.View()
                    confirm_view.add_item(discord.ui.Button(label="✅ Confirm", style=discord.ButtonStyle.success, custom_id="confirm_all"))
                    confirm_view.add_item(discord.ui.Button(label="❌ Cancel", style=discord.ButtonStyle.danger, custom_id="cancel_all"))

                    async def button_callback(button_interaction: discord.Interaction):
                        try:
                            if button_interaction.data["custom_id"] == "confirm_all":
                                
                                fid_list = [str(fid) for fid, _ in members]
                                self.c_users.execute("DELETE FROM users WHERE alliance = ?", (alliance_id,))
                                self.conn_users.commit()
                                
                                result_embed = discord.Embed(
                                    title="✅ Members Removed",
                                    description=f"Total **{len(members)}** members removed.\n\n**Removed FIDs:**\n{', '.join(fid_list)}",
                                    color=discord.Color.green()
                                )
                                await button_interaction.response.edit_message(embed=result_embed, view=None)
                            else:
                                
                                cancel_embed = discord.Embed(
                                    title="❌ Operation Cancelled",
                                    description="Member removal operation has been cancelled.",
                                    color=discord.Color.orange()
                                )
                                await button_interaction.response.edit_message(embed=cancel_embed, view=None)
                        except Exception as e:
                            self.log_message(f"Error in button operation: {e}")

                    
                    for button in confirm_view.children:
                        button.callback = button_callback

                    await member_interaction.response.edit_message(embed=embed, view=confirm_view)
                
                else:
                    try:
                        selected_fid = selected_value
                        self.c_users.execute("SELECT nickname FROM users WHERE fid = ?", (selected_fid,))
                        nickname = self.c_users.fetchone()[0]
                        
                        self.c_users.execute("DELETE FROM users WHERE fid = ?", (selected_fid,))
                        self.conn_users.commit()
                        
                        result_embed = discord.Embed(
                            title="✅ Member Removed",
                            description=f"**{nickname}** (FID: {selected_fid}) has been successfully removed.",
                            color=discord.Color.green()
                        )
                        await member_interaction.response.edit_message(embed=result_embed, view=None)
                    except Exception as e:
                        self.log_message(f"Error in member removal: {e}")

            member_select.callback = member_select_callback
            await select_interaction.response.edit_message(content=None, view=member_view)

        select.callback = select_callback
        await interaction.response.send_message("Please select an alliance:", view=view, ephemeral=True)

    async def add_user(self, interaction: discord.Interaction, alliance_id: str, ids: str):
        self.c_alliance.execute("SELECT name FROM alliance_list WHERE alliance_id = ?", (alliance_id,))
        alliance_name = self.c_alliance.fetchone()
        if alliance_name:
            alliance_name = alliance_name[0]
        else:
            await interaction.response.send_message("Alliance not found.", ephemeral=True)
            return

        if not await self.is_admin(interaction.user.id):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return
        
        # Check if an operation is already running
        if self.operation_lock.locked():
            # Add to queue
            queue_position = len(self.operation_queue) + 1
            operation_info = {
                'interaction': interaction,
                'alliance_id': alliance_id,
                'alliance_name': alliance_name,
                'ids': ids,
                'position': queue_position
            }
            self.operation_queue.append(operation_info)
            
            queue_embed = discord.Embed(
                title="⏳ Operation Queued",
                description=(
                    f"Another member addition operation is currently in progress.\n\n"
                    f"**Your operation has been queued:**\n"
                    f"📍 Queue Position: `{queue_position}`\n"
                    f"🏰 Alliance: {alliance_name}\n"
                    f"👥 Members to add: {len(ids.split(',') if ',' in ids else ids.split('\n'))}\n\n"
                    f"You will be notified when your operation starts."
                ),
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=queue_embed, ephemeral=True)
            return

        # Acquire the lock for this operation
        async with self.operation_lock:
            await self._process_add_user(interaction, alliance_id, alliance_name, ids)
            
        # After completing, check if there are queued operations
        await self._process_queued_operations()

    async def _process_queued_operations(self):
        """Process any queued operations after the current one completes"""
        while self.operation_queue:
            next_operation = self.operation_queue.pop(0)
            
            # Update remaining operations' positions
            for i, op in enumerate(self.operation_queue):
                op['position'] = i + 1
            
            # Notify the user their operation is starting
            start_embed = discord.Embed(
                title="🚀 Operation Starting",
                description=(
                    f"Your member addition operation is now starting!\n\n"
                    f"🏰 Alliance: {next_operation['alliance_name']}\n"
                    f"👥 Members to add: {len(next_operation['ids'].split(',') if ',' in next_operation['ids'] else next_operation['ids'].split('\n'))}"
                ),
                color=discord.Color.green()
            )
            
            try:
                # Send a new message to notify the user
                await next_operation['interaction'].followup.send(embed=start_embed, ephemeral=True)
                
                # Process the operation
                async with self.operation_lock:
                    await self._process_add_user(
                        next_operation['interaction'],
                        next_operation['alliance_id'],
                        next_operation['alliance_name'],
                        next_operation['ids']
                    )
            except Exception as e:
                self.log_message(f"Error processing queued operation: {str(e)}")
                continue

    async def _process_add_user(self, interaction: discord.Interaction, alliance_id: str, alliance_name: str, ids: str):
        """Process the actual user addition operation"""
        # Handle both comma-separated and newline-separated FIDs
        if '\n' in ids:
            ids_list = [fid.strip() for fid in ids.split('\n') if fid.strip()]
        else:
            ids_list = [fid.strip() for fid in ids.split(",") if fid.strip()]

        total_users = len(ids_list)
        embed = discord.Embed(
            title="👥 User Addition Progress", 
            description=f"Processing {total_users} members...\n\n**Progress:** `0/{total_users}`", 
            color=discord.Color.blue()
        )
        embed.add_field(
            name=f"✅ Successfully Added (0/{total_users})", 
            value="-", 
            inline=False
        )
        embed.add_field(
            name=f"❌ Failed (0/{total_users})", 
            value="-", 
            inline=False
        )
        embed.add_field(
            name=f"⚠️ Already Exists (0/{total_users})", 
            value="-", 
            inline=False
        )

        # Check if this is a queued operation (already has a response)
        if interaction.response.is_done():
            # This is a queued operation, send as followup
            message = await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            # This is a direct operation
            await interaction.response.send_message(embed=embed, ephemeral=True)
            message = await interaction.original_response()
        
        # Reset rate limit tracking for this operation
        self.api1_requests = []
        self.api2_requests = []
        
        # Check API availability before starting
        embed.description = "🔍 Checking API availability..."
        await message.edit(embed=embed)
        
        api_status = await self._check_apis_availability()
        
        if api_status["api1_available"] and api_status["api2_available"]:
            self.dual_api_mode = True
            self.available_apis = [1, 2]
            self.request_delay = 1.0  # 1 second delay for dual mode (1 member/second)
            mode_text = "✅ Dual-API mode active (1 member/second)"
        elif api_status["api1_available"]:
            self.dual_api_mode = False
            self.available_apis = [1]
            self.request_delay = 2.0  # 1 member every 2 seconds for single API
            mode_text = "⚠️ Single-API mode (1 member/2 seconds) - API 2 unavailable"
        elif api_status["api2_available"]:
            self.dual_api_mode = False
            self.available_apis = [2]
            self.request_delay = 2.0
            mode_text = "⚠️ Single-API mode (1 member/2 seconds) - API 1 unavailable"
        else:
            # Both APIs down
            embed.description = "❌ Both APIs are unavailable. Cannot proceed."
            embed.color = discord.Color.red()
            await message.edit(embed=embed)
            return
        
        # Update embed with mode information
        queue_info = f"\n📋 **Operations in queue:** {len(self.operation_queue)}" if self.operation_queue else ""
        embed.description = f"Processing {total_users} members...\n{mode_text}{queue_info}\n\n**Progress:** `0/{total_users}`"
        embed.color = discord.Color.blue()
        await message.edit(embed=embed)

        added_count = 0
        error_count = 0 
        already_exists_count = 0
        added_users = []
        error_users = []
        already_exists_users = []

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_file_path = os.path.join(self.log_directory, 'add_memberlog.txt')
        
        try:
            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                log_file.write(f"\n{'='*50}\n")
                log_file.write(f"Date: {timestamp}\n")
                log_file.write(f"Administrator: {interaction.user.name} (ID: {interaction.user.id})\n")
                log_file.write(f"Alliance: {alliance_name} (ID: {alliance_id})\n")
                log_file.write(f"FIDs to Process: {ids.replace(chr(10), ', ')}\n")
                log_file.write(f"Total Members to Process: {total_users}\n")
                log_file.write(f"API Mode: {mode_text}\n")
                log_file.write(f"Available APIs: {self.available_apis}\n")
                log_file.write(f"Operations in Queue: {len(self.operation_queue)}\n")
                log_file.write('-'*50 + '\n')

            # Create SSL context and session outside the loop for efficiency
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            
            async with aiohttp.ClientSession(connector=connector) as session:
                index = 0
                while index < len(ids_list):
                    fid = ids_list[index]
                    try:
                        # Check rate limits and get available API
                        api_result = self._get_available_api()
                        
                        if api_result is None or (isinstance(api_result, tuple) and api_result[0] is None):
                            # Both APIs at limit, need to wait
                            wait_time = api_result[1] if isinstance(api_result, tuple) else self._get_wait_time()
                            queue_info = f"\n📋 **Operations in queue:** {len(self.operation_queue)}" if self.operation_queue else ""
                            embed.description = f"⚠️ Rate limit reached on {'both APIs' if self.dual_api_mode else 'API'}. Waiting {wait_time:.1f} seconds...{queue_info}"
                            embed.color = discord.Color.orange()
                            await message.edit(embed=embed)
                            
                            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                log_file.write(f"Rate limit reached - Waiting {wait_time:.1f} seconds\n")
                            
                            await asyncio.sleep(wait_time)
                            embed.color = discord.Color.blue()
                            continue  # Retry this request
                        
                        # Get the API number to use
                        api_num = api_result if isinstance(api_result, int) else api_result
                        api_url = self.api1_url if api_num == 1 else self.api2_url
                        
                        # Update progress with API info
                        api_info = f" (API {api_num})" if self.dual_api_mode else ""
                        queue_info = f"\n📋 **Operations in queue:** {len(self.operation_queue)}" if self.operation_queue else ""
                        embed.description = f"Processing {total_users} members...\n{mode_text}{queue_info}\n\n**Progress:** `{index + 1}/{total_users}`{api_info}"
                        await message.edit(embed=embed)
                        
                        # Prepare request
                        current_time = int(time.time() * 1000)
                        form = f"fid={fid}&time={current_time}"
                        sign = hashlib.md5((form + SECRET).encode('utf-8')).hexdigest()
                        form = f"sign={sign}&{form}"
                        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
                        
                        async with session.post(api_url, headers=headers, data=form) as response:
                            # Record the API request
                            self._record_api_request(api_num)
                            
                            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                log_file.write(f"\nAPI{api_num} Response for FID {fid}:\n")
                                log_file.write(f"Status Code: {response.status}\n")
                                log_file.write(f"URL: {api_url}\n")
                            
                            if response.status == 429:
                                # This shouldn't happen with our rate limiting, but handle it anyway
                                with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                    log_file.write(f"Unexpected rate limit on API{api_num}\n")
                                
                                # Don't increment index, retry with other API
                                continue
                            
                            if response.status == 200:
                                data = await response.json()
                                with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                    log_file.write(f"API Response Data: {str(data)}\n")
                                
                                if not data.get('data'):
                                    with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                        log_file.write(f"ERROR: No data found for FID {fid}\n")
                                    error_count += 1
                                    if fid not in error_users:
                                        error_users.append(fid)
                                    with open(self.log_file, 'a', encoding='utf-8') as f:
                                        f.write(f"[{timestamp}] No data found for fid: {fid}\n")
                                        f.write(f"[{timestamp}] API Response: {str(data)}\n")
                                    
                                    embed.set_field_at(
                                        1,
                                        name=f"❌ Failed ({error_count}/{total_users})",
                                        value="Error list cannot be displayed due to exceeding 70 users" if len(error_users) > 70 
                                        else ", ".join(error_users) or "-",
                                        inline=False
                                    )
                                    await message.edit(embed=embed)
                                    index += 1
                                    continue

                                nickname = data['data'].get('nickname')
                                furnace_lv = data['data'].get('stove_lv', 0)
                                stove_lv_content = data['data'].get('stove_lv_content', None)
                                kid = data['data'].get('kid', None)

                                if nickname:
                                    self.c_users.execute("SELECT * FROM users WHERE fid=?", (fid,))
                                    result = self.c_users.fetchone()

                                    if result is None:
                                        try:
                                            self.c_users.execute("""
                                                INSERT INTO users (fid, nickname, furnace_lv, kid, stove_lv_content, alliance)
                                                VALUES (?, ?, ?, ?, ?, ?)
                                            """, (fid, nickname, furnace_lv, kid, stove_lv_content, alliance_id))
                                            self.conn_users.commit()
                                            
                                            with open(self.log_file, 'a', encoding='utf-8') as f:
                                                f.write(f"[{timestamp}] Successfully added member - FID: {fid}, Nickname: {nickname}, Level: {furnace_lv}\n")
                                                f.write(f"[{timestamp}] API Response: {str(data)}\n")
                                            
                                            added_count += 1
                                            added_users.append((fid, nickname))
                                            
                                            embed.set_field_at(
                                                0,
                                                name=f"✅ Successfully Added ({added_count}/{total_users})",
                                                value="User list cannot be displayed due to exceeding 70 users" if len(added_users) > 70 
                                                else ", ".join([n for _, n in added_users]) or "-",
                                                inline=False
                                            )
                                            await message.edit(embed=embed)
                                            
                                        except Exception as e:
                                            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                                log_file.write(f"ERROR: Database error for FID {fid}: {str(e)}\n")
                                            error_count += 1
                                            error_users.append(fid)
                                            
                                            embed.set_field_at(
                                                1,
                                                name=f"❌ Failed ({error_count}/{total_users})",
                                                value="Error list cannot be displayed due to exceeding 70 users" if len(error_users) > 70 
                                                else ", ".join(error_users) or "-",
                                                inline=False
                                            )
                                            await message.edit(embed=embed)
                                    else:
                                        with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                            log_file.write(f"WARNING: Member already exists - {nickname} (FID: {fid})\n")
                                        already_exists_count += 1
                                        already_exists_users.append((fid, nickname))
                                        
                                        embed.set_field_at(
                                            2,
                                            name=f"⚠️ Already Exists ({already_exists_count}/{total_users})",
                                            value="Existing user list cannot be displayed due to exceeding 70 users" if len(already_exists_users) > 70 
                                            else ", ".join([n for _, n in already_exists_users]) or "-",
                                            inline=False
                                        )
                                        await message.edit(embed=embed)
                                else:
                                    error_count += 1
                                    error_users.append(fid)
                            else:
                                # Handle non-200 responses (other than 429)
                                with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                    log_file.write(f"ERROR: Unexpected status {response.status} for FID {fid}\n")
                                error_count += 1
                                if fid not in error_users:
                                    error_users.append(fid)
                                embed.set_field_at(
                                    1,
                                    name=f"❌ Failed ({error_count}/{total_users})",
                                    value="Error list cannot be displayed due to exceeding 70 users" if len(error_users) > 70 
                                    else ", ".join(error_users) or "-",
                                    inline=False
                                )
                                await message.edit(embed=embed)
                        
                        # Add delay between requests based on mode
                        if index < len(ids_list) - 1:  # Don't delay after the last request
                            await asyncio.sleep(self.request_delay)
                        
                        index += 1

                    except Exception as e:
                        with open(log_file_path, 'a', encoding='utf-8') as log_file:
                            log_file.write(f"ERROR: Request failed for FID {fid}: {str(e)}\n")
                        error_count += 1
                        error_users.append(fid)
                        await message.edit(embed=embed)
                        index += 1

            embed.set_field_at(0, name=f"✅ Successfully Added ({added_count}/{total_users})",
                value="User list cannot be displayed due to exceeding 70 users" if len(added_users) > 70 
                else ", ".join([nickname for _, nickname in added_users]) or "-",
                inline=False
            )
            
            embed.set_field_at(1, name=f"❌ Failed ({error_count}/{total_users})",
                value="Error list cannot be displayed due to exceeding 70 users" if len(error_users) > 70 
                else ", ".join(error_users) or "-",
                inline=False
            )
            
            embed.set_field_at(2, name=f"⚠️ Already Exists ({already_exists_count}/{total_users})",
                value="Existing user list cannot be displayed due to exceeding 70 users" if len(already_exists_users) > 70 
                else ", ".join([nickname for _, nickname in already_exists_users]) or "-",
                inline=False
            )

            await message.edit(embed=embed)

            try:
                with sqlite3.connect('db/settings.sqlite') as settings_db:
                    cursor = settings_db.cursor()
                    cursor.execute("""
                        SELECT channel_id 
                        FROM alliance_logs 
                        WHERE alliance_id = ?
                    """, (alliance_id,))
                    alliance_log_result = cursor.fetchone()
                    
                    if alliance_log_result and alliance_log_result[0]:
                        log_embed = discord.Embed(
                            title="👥 Members Added to Alliance",
                            description=(
                                f"**Alliance:** {alliance_name}\n"
                                f"**Administrator:** {interaction.user.name} (`{interaction.user.id}`)\n"
                                f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"**API Mode:** {mode_text}\n\n"
                                f"**Results:**\n"
                                f"✅ Successfully Added: {added_count}\n"
                                f"❌ Failed: {error_count}\n"
                                f"⚠️ Already Exists: {already_exists_count}\n\n"
                                "**Added FIDs:**\n"
                                f"```\n{', '.join(ids_list)}\n```"
                            ),
                            color=discord.Color.green()
                        )

                        try:
                            alliance_channel_id = int(alliance_log_result[0])
                            alliance_log_channel = self.bot.get_channel(alliance_channel_id)
                            if alliance_log_channel:
                                await alliance_log_channel.send(embed=log_embed)
                        except Exception as e:
                            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                log_file.write(f"ERROR: Alliance Log Sending Error: {str(e)}\n")

            except Exception as e:
                with open(log_file_path, 'a', encoding='utf-8') as log_file:
                    log_file.write(f"ERROR: Log record error: {str(e)}\n")

            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                log_file.write(f"\nFinal Results:\n")
                log_file.write(f"Successfully Added: {added_count}\n")
                log_file.write(f"Failed: {error_count}\n")
                log_file.write(f"Already Exists: {already_exists_count}\n")
                log_file.write(f"API Mode: {mode_text}\n")
                log_file.write(f"API1 Requests: {len(self.api1_requests)}\n")
                log_file.write(f"API2 Requests: {len(self.api2_requests)}\n")
                log_file.write(f"{'='*50}\n")

        except Exception as e:
            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                log_file.write(f"CRITICAL ERROR: {str(e)}\n")
                log_file.write(f"{'='*50}\n")

        # Calculate total processing time
        end_time = datetime.now()
        start_time = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        processing_time = (end_time - start_time).total_seconds()
        
        queue_info = f"\n📋 **Operations still in queue:** {len(self.operation_queue)}" if self.operation_queue else ""
        
        embed.title = "✅ User Addition Completed"
        embed.description = (
            f"Process completed for {total_users} members.\n"
            f"**API Mode:** {mode_text}\n"
            f"**Processing Time:** {processing_time:.1f} seconds{queue_info}"
        )
        embed.color = discord.Color.green()
        await message.edit(embed=embed)

    async def is_admin(self, user_id):
        try:
            
            with sqlite3.connect('db/settings.sqlite') as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM admin WHERE id = ?", (user_id,))
                result = cursor.fetchone()
                is_admin = result is not None
                return is_admin
        except Exception as e:
            self.log_message(f"Error in admin check: {str(e)}")
            self.log_message(f"Error details: {str(e.__class__.__name__)}")
            return False

    def cog_unload(self):
        
        self.conn_users.close()
        self.conn_alliance.close()

    async def get_admin_alliances(self, user_id: int, guild_id: int):
        try:
            
            with sqlite3.connect('db/settings.sqlite') as settings_db:
                cursor = settings_db.cursor()
                cursor.execute("SELECT is_initial FROM admin WHERE id = ?", (user_id,))
                admin_result = cursor.fetchone()
                
                if not admin_result:
                    self.log_message(f"User {user_id} is not an admin")
                    return [], [], False
                    
                is_initial = admin_result[0]
                
            if is_initial == 1:
                
                with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                    cursor = alliance_db.cursor()
                    cursor.execute("SELECT alliance_id, name FROM alliance_list ORDER BY name")
                    alliances = cursor.fetchall()
                    return alliances, [], True
            
            
            server_alliances = []
            special_alliances = []
            
            
            with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                cursor = alliance_db.cursor()
                cursor.execute("""
                    SELECT DISTINCT alliance_id, name 
                    FROM alliance_list 
                    WHERE discord_server_id = ?
                    ORDER BY name
                """, (guild_id,))
                server_alliances = cursor.fetchall()
            
            
            with sqlite3.connect('db/settings.sqlite') as settings_db:
                cursor = settings_db.cursor()
                cursor.execute("""
                    SELECT alliances_id 
                    FROM adminserver 
                    WHERE admin = ?
                """, (user_id,))
                special_alliance_ids = cursor.fetchall()
                
            
            if special_alliance_ids:
                with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                    cursor = alliance_db.cursor()
                    placeholders = ','.join('?' * len(special_alliance_ids))
                    cursor.execute(f"""
                        SELECT DISTINCT alliance_id, name
                        FROM alliance_list
                        WHERE alliance_id IN ({placeholders})
                        ORDER BY name
                    """, [aid[0] for aid in special_alliance_ids])
                    special_alliances = cursor.fetchall()
            
            all_alliances = list({(aid, name) for aid, name in (server_alliances + special_alliances)})
            
            if not all_alliances and not special_alliances:
                return [], [], False
            
            return all_alliances, special_alliances, False
                
        except Exception as e:
            return [], [], False

    async def handle_button_interaction(self, interaction: discord.Interaction):
        custom_id = interaction.data["custom_id"]
        
        if custom_id == "main_menu":
            await self.show_main_menu(interaction)
    
    async def show_main_menu(self, interaction: discord.Interaction):
        try:
            alliance_cog = self.bot.get_cog("Alliance")
            if alliance_cog:
                await alliance_cog.show_main_menu(interaction)
            else:
                await interaction.response.send_message(
                    "❌ An error occurred while returning to main menu.",
                    ephemeral=True
                )
        except Exception as e:
            self.log_message(f"[ERROR] Main Menu error in member operations: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error occurred while returning to main menu.", 
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ An error occurred while returning to main menu.",
                    ephemeral=True
                )

class AddMemberModal(discord.ui.Modal):
    def __init__(self, alliance_id):
        super().__init__(title="Add Member")
        self.alliance_id = alliance_id
        self.add_item(discord.ui.TextInput(
            label="Enter FIDs (comma or newline separated)", 
            placeholder="Comma: 12345,67890,54321\nNewline:\n12345\n67890\n54321",
            style=discord.TextStyle.paragraph
        ))

    async def on_submit(self, interaction: discord.Interaction):
        try:

            
            ids = self.children[0].value
            await interaction.client.get_cog("AllianceMemberOperations").add_user(
                interaction, 
                self.alliance_id, 
                ids
            )
        except Exception as e:
            self.log_message(f"ERROR: Modal submit error - {str(e)}")
            await interaction.response.send_message(
                "An error occurred. Please try again.", 
                ephemeral=True
            )

class RemoveMemberModal(discord.ui.Modal):
    def __init__(self, alliance_id):
        super().__init__(title="Remove Member")
        self.alliance_id = alliance_id
        self.add_item(discord.ui.InputText(label="Enter IDs (comma-separated)", placeholder="e.g., 12345,67890"))

    async def callback(self, interaction: discord.Interaction):
        ids = self.children[0].value
        await interaction.client.get_cog("AllianceMemberOperations").remove_user(interaction, self.alliance_id, ids)


class AllianceSelectView(discord.ui.View):
    def __init__(self, alliances_with_counts, cog=None, page=0):
        super().__init__(timeout=7200)
        self.alliances = alliances_with_counts
        self.cog = cog
        self.page = page
        self.max_page = (len(alliances_with_counts) - 1) // 25 if alliances_with_counts else 0
        self.current_select = None
        self.callback = None
        self.member_dict = {}
        self.selected_alliance_id = None
        self.update_select_menu()

    def update_select_menu(self):
        for item in self.children[:]:
            if isinstance(item, discord.ui.Select):
                self.remove_item(item)

        start_idx = self.page * 25
        end_idx = min(start_idx + 25, len(self.alliances))
        current_alliances = self.alliances[start_idx:end_idx]

        select = discord.ui.Select(
            placeholder=f"🏰 Select an alliance... (Page {self.page + 1}/{self.max_page + 1})",
            options=[
                discord.SelectOption(
                    label=f"{name[:50]}",
                    value=str(alliance_id),
                    description=f"ID: {alliance_id} | Members: {count}",
                    emoji="🏰"
                ) for alliance_id, name, count in current_alliances
            ]
        )
        
        async def select_callback(interaction: discord.Interaction):
            self.current_select = select
            if self.callback:
                await self.callback(interaction)
        
        select.callback = select_callback
        self.add_item(select)
        self.current_select = select

        if hasattr(self, 'prev_button'):
            self.prev_button.disabled = self.page == 0
        if hasattr(self, 'next_button'):
            self.next_button.disabled = self.page == self.max_page

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self.update_select_menu()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_page, self.page + 1)
        self.update_select_menu()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Select by FID", emoji="🔍", style=discord.ButtonStyle.secondary)
    async def fid_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            
            if self.current_select and self.current_select.values:
                self.selected_alliance_id = self.current_select.values[0]
            
            modal = FIDSearchModal(
                selected_alliance_id=self.selected_alliance_id,
                alliances=self.alliances,
                callback=self.callback
            )
            await interaction.response.send_modal(modal)
        except Exception as e:
            self.log_message(f"FID button error: {e}")
            await interaction.response.send_message(
                "❌ An error has occurred. Please try again.",
                ephemeral=True
            )

class FIDSearchModal(discord.ui.Modal):
    def __init__(self, selected_alliance_id=None, alliances=None, callback=None):
        super().__init__(title="Search Members with FID")
        self.selected_alliance_id = selected_alliance_id
        self.alliances = alliances
        self.callback = callback
        
        self.add_item(discord.ui.TextInput(
            label="Member ID",
            placeholder="Example: 12345",
            min_length=1,
            max_length=20,
            required=True
        ))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            fid = self.children[0].value.strip()
            
            
            with sqlite3.connect('db/users.sqlite') as users_db:
                cursor = users_db.cursor()
                cursor.execute("""
                    SELECT fid, nickname, furnace_lv, alliance
                    FROM users 
                    WHERE fid = ?
                """, (fid,))
                user_result = cursor.fetchone()
                
                if not user_result:
                    await interaction.response.send_message(
                        "❌ No member with this FID was found.",
                        ephemeral=True
                    )
                    return

                fid, nickname, furnace_lv, current_alliance_id = user_result

                
                with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                    cursor = alliance_db.cursor()
                    cursor.execute("SELECT name FROM alliance_list WHERE alliance_id = ?", (current_alliance_id,))
                    current_alliance_name = cursor.fetchone()[0]

                embed = discord.Embed(
                    title="✅ Member Found - Transfer Process",
                    description=(
                        f"**Member Information:**\n"
                        f"👤 **Name:** {nickname}\n"
                        f"🆔 **FID:** {fid}\n"
                        f"⚔️ **Level:** {furnace_lv}\n"
                        f"🏰 **Current Alliance:** {current_alliance_name}\n\n"
                        "**Transfer Process**\n"
                        "Please select the alliance you want to transfer the member to:"
                    ),
                    color=discord.Color.blue()
                )

                select = discord.ui.Select(
                    placeholder="🎯 Choose the target alliance...",
                    options=[
                        discord.SelectOption(
                            label=f"{name[:50]}",
                            value=str(alliance_id),
                            description=f"ID: {alliance_id}",
                            emoji="🏰"
                        ) for alliance_id, name, _ in self.alliances
                        if alliance_id != current_alliance_id  
                    ]
                )
                
                view = discord.ui.View()
                view.add_item(select)

                async def select_callback(select_interaction: discord.Interaction):
                    target_alliance_id = int(select.values[0])
                    
                    try:
                        with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                            cursor = alliance_db.cursor()
                            cursor.execute("SELECT name FROM alliance_list WHERE alliance_id = ?", (target_alliance_id,))
                            target_alliance_name = cursor.fetchone()[0]

                        
                        with sqlite3.connect('db/users.sqlite') as users_db:
                            cursor = users_db.cursor()
                            cursor.execute(
                                "UPDATE users SET alliance = ? WHERE fid = ?",
                                (target_alliance_id, fid)
                            )
                            users_db.commit()

                        
                        success_embed = discord.Embed(
                            title="✅ Transfer Successful",
                            description=(
                                f"👤 **Member:** {nickname}\n"
                                f"🆔 **FID:** {fid}\n"
                                f"📤 **Source:** {current_alliance_name}\n"
                                f"📥 **Target:** {target_alliance_name}"
                            ),
                            color=discord.Color.green()
                        )
                        
                        await select_interaction.response.edit_message(
                            embed=success_embed,
                            view=None
                        )
                        
                    except Exception as e:
                        self.log_message(f"Transfer error: {e}")
                        error_embed = discord.Embed(
                            title="❌ Error",
                            description="An error occurred during the transfer operation.",
                            color=discord.Color.red()
                        )
                        await select_interaction.response.edit_message(
                            embed=error_embed,
                            view=None
                        )

                select.callback = select_callback
                await interaction.response.send_message(
                    embed=embed,
                    view=view,
                    ephemeral=True
                )

        except Exception as e:
            self.log_message(f"FID search error: {e}")
            print(f"Error details: {str(e.__class__.__name__)}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error has occurred. Please try again.",
                    ephemeral=True
                )

class MemberSelectView(discord.ui.View):
    def __init__(self, members, source_alliance_name, cog, page=0):
        super().__init__(timeout=7200)
        self.members = members
        self.source_alliance_name = source_alliance_name
        self.cog = cog
        self.page = page
        self.max_page = (len(members) - 1) // 25
        self.current_select = None
        self.callback = None
        self.member_dict = {str(fid): nickname for fid, nickname, _ in members}
        self.selected_alliance_id = None
        self.alliances = None
        self.update_select_menu()

    def update_select_menu(self):
        for item in self.children[:]:
            if isinstance(item, discord.ui.Select):
                self.remove_item(item)

        start_idx = self.page * 25
        end_idx = min(start_idx + 25, len(self.members))
        current_members = self.members[start_idx:end_idx]

        options = []
        
        if self.page == 0:
            options.append(discord.SelectOption(
                label="ALL MEMBERS",
                value="all",
                description=f"⚠️ Delete all {len(self.members)} members!",
                emoji="⚠️"
            ))

        remaining_slots = 25 - len(options)
        member_options = [
            discord.SelectOption(
                label=f"{nickname[:50]}",
                value=str(fid),
                description=f"FID: {fid} | FC: {self.cog.level_mapping.get(furnace_lv, str(furnace_lv))}",
                emoji="👤"
            ) for fid, nickname, furnace_lv in current_members[:remaining_slots]
        ]
        options.extend(member_options)

        select = discord.ui.Select(
            placeholder=f"👤 Select member to transfer... (Page {self.page + 1}/{self.max_page + 1})",
            options=options
        )
        
        async def select_callback(interaction: discord.Interaction):
            self.current_select = select
            if self.callback:
                await self.callback(interaction)
        
        select.callback = select_callback
        self.add_item(select)
        self.current_select = select

        if hasattr(self, 'prev_button'):
            self.prev_button.disabled = self.page == 0
        if hasattr(self, 'next_button'):
            self.next_button.disabled = self.page == self.max_page

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self.update_select_menu()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_page, self.page + 1)
        self.update_select_menu()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Select by FID", emoji="🔍", style=discord.ButtonStyle.secondary)
    async def fid_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            
            if self.current_select and self.current_select.values:
                self.selected_alliance_id = self.current_select.values[0]
            
            modal = FIDSearchModal(
                selected_alliance_id=self.selected_alliance_id,
                alliances=self.alliances,
                callback=self.callback
            )
            await interaction.response.send_modal(modal)
        except Exception as e:
            self.log_message(f"FID button error: {e}")
            await interaction.response.send_message(
                "❌ An error has occurred. Please try again.",
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(AllianceMemberOperations(bot))