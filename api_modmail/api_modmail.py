import json
import logging
import os
import re
import time
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.rankgun.works"
DEFAULT_EMBED_COLOR = 0xBB1B1A
CONFIG_PATH = os.getenv(
    "API_MODMAIL_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
)
CONFIG_KEYS = (
    "api_key", "api_url", "group_id", "max_rank", "min_rank",
    "required_role_id", "requester_min_rank",
)


class APIModmail(commands.Cog):
    """Modmail plugin for Roblox group rank management through an external API."""

    def __init__(self, bot):
        self.bot = bot
        # Environment variables remain optional fallbacks; config.json is the persistent source.
        self.api_key = os.getenv("API_KEY", "").strip()
        self.api_url = os.getenv("API_URL", DEFAULT_API_URL).rstrip("/")
        self.group_id = self._read_int("ROBLOX_GROUP_ID", 12345678)
        self.max_rank = self._read_int("RANK_MAX_RANK", 123)
        self.min_rank = self._read_int("RANK_MIN_RANK", 0)
        self.required_role_id = self._read_optional_int("RANK_ROLE_ID")
        self.requester_min_rank = self._read_int("REQUESTER_MIN_RANK", 1)
        self.embed_color = DEFAULT_EMBED_COLOR
        self._load_config()
        self._roles_cache = []
        self._roles_cache_at = 0.0
        self._member_rank_cache = {}
        self._member_rank_cache_at = {}

    @staticmethod
    def _read_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _read_optional_int(name: str) -> Optional[int]:
        raw = os.getenv(name, "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            log.warning("Ignoring invalid integer environment variable %s", name)
            return None

    def _load_config(self):
        """Load saved settings from config.json, retaining environment/default fallbacks."""
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("config.json must contain a JSON object")
            if data.get("api_key"):
                self.api_key = str(data["api_key"]).strip()
            if data.get("api_url"):
                self.api_url = str(data["api_url"]).rstrip("/")
            for key, attr in (
                ("group_id", "group_id"),
                ("max_rank", "max_rank"),
                ("min_rank", "min_rank"),
                ("requester_min_rank", "requester_min_rank"),
            ):
                if key in data:
                    setattr(self, attr, int(data[key]))
            if "required_role_id" in data:
                value = data["required_role_id"]
                self.required_role_id = int(value) if value not in (None, "", 0, "0") else None
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.error("Could not load API Modmail config at %s: %s", CONFIG_PATH, exc)

    def _save_config(self):
        """Atomically save settings so a partial write cannot corrupt the config."""
        directory = os.path.dirname(CONFIG_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {
            "api_key": self.api_key,
            "api_url": self.api_url,
            "group_id": self.group_id,
            "max_rank": self.max_rank,
            "min_rank": self.min_rank,
            "required_role_id": self.required_role_id,
            "requester_min_rank": self.requester_min_rank,
        }
        temp_path = CONFIG_PATH + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\\n")
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, CONFIG_PATH)
            try:
                os.chmod(CONFIG_PATH, 0o600)
            except OSError:
                pass
        except OSError as exc:
            log.exception("Could not save API Modmail config at %s", CONFIG_PATH)
            raise RuntimeError(
                "Settings changed in memory but could not be saved to disk. "
                "Check that the plugin folder is writable and persistent."
            ) from exc

    def _clear_caches(self):
        self._roles_cache = []
        self._roles_cache_at = 0.0
        self._member_rank_cache.clear()
        self._member_rank_cache_at.clear()

    async def _request_json(self, method: str, url: str, **kwargs):
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, **kwargs) as response:
                body = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"API request failed (HTTP {response.status}).")
                if not body:
                    return {}
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise RuntimeError("The API returned invalid JSON.") from exc

    async def resolve_roblox_user_id(self, input_value: str) -> Optional[int]:
        input_value = input_value.strip()
        if input_value.isdigit():
            return int(input_value)
        data = await self._request_json(
            "POST",
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [input_value], "excludeBannedUsers": False},
        )
        user = (data.get("data") or [None])[0]
        return int(user["id"]) if user and user.get("id") else None

    async def resolve_roblox_username(self, user_id: int) -> str:
        try:
            data = await self._request_json(
                "GET", f"https://users.roblox.com/v1/users/{int(user_id)}"
            )
            return data.get("name") or str(user_id)
        except Exception:
            return str(user_id)

    async def fetch_group_roles(self, force_refresh: bool = False):
        if not force_refresh and self._roles_cache and time.monotonic() - self._roles_cache_at < 60:
            return self._roles_cache
        data = await self._request_json(
            "GET", f"https://groups.roblox.com/v1/groups/{self.group_id}/roles"
        )
        self._roles_cache = data.get("roles") or []
        self._roles_cache_at = time.monotonic()
        return self._roles_cache

    async def fetch_user_group_rank(self, user_id: int) -> Optional[dict]:
        cache_key = int(user_id)
        now = time.monotonic()
        if cache_key in self._member_rank_cache and now - self._member_rank_cache_at.get(cache_key, 0) < 60:
            return self._member_rank_cache[cache_key]
        try:
            data = await self._request_json(
                "GET", f"https://groups.roblox.com/v2/users/{cache_key}/groups/roles"
            )
        except Exception as exc:
            raise RuntimeError("Couldn't verify the Roblox group rank.") from exc
        result = None
        for membership in data.get("data", []):
            group = membership.get("group") or {}
            role = membership.get("role") or {}
            if int(group.get("id", -1)) == self.group_id:
                result = {"rank": int(role.get("rank", 0)), "name": str(role.get("name", "Unknown")),
                          "id": int(role.get("id", 0))}
                break
        self._member_rank_cache[cache_key] = result
        self._member_rank_cache_at[cache_key] = now
        return result

    @staticmethod
    def find_role(value: str, roles: list):
        value = value.strip()
        if value.isdigit():
            match = next((r for r in roles if int(r.get("rank", -1)) == int(value)), None)
            if match:
                return match
        lowered = value.casefold()
        exact = next((r for r in roles if str(r.get("name", "")).casefold() == lowered), None)
        if exact:
            return exact
        return next((r for r in roles if lowered and lowered in str(r.get("name", "")).casefold()), None)

    def has_discord_role_permission(self, ctx: commands.Context) -> bool:
        if self.required_role_id is None:
            return False
        return any(getattr(role, "id", None) == self.required_role_id
                   for role in getattr(ctx.author, "roles", []))

    async def _is_admin(self, ctx: commands.Context) -> bool:
        """Plugin configuration is restricted to Discord administrators."""
        permissions = getattr(ctx.author, "guild_permissions", None)
        return bool(permissions and permissions.administrator)

    async def verify_requester(self, ctx: commands.Context):
        member = ctx.author
        candidate_names = []
        for candidate in (getattr(member, "nick", None), getattr(member, "display_name", None),
                          getattr(member, "name", None)):
            candidate = str(candidate or "").strip()
            if candidate and candidate not in candidate_names:
                candidate_names.append(candidate)
        for candidate in list(candidate_names):
            cleaned = candidate.strip("[](){}<> ")
            if cleaned and cleaned not in candidate_names:
                candidate_names.append(cleaned)
        for candidate in candidate_names:
            try:
                user_id = await self.resolve_roblox_user_id(candidate)
            except Exception:
                continue
            if not user_id:
                continue
            group_role = await self.fetch_user_group_rank(user_id)
            if group_role is not None:
                return {"user_id": user_id, "username": await self.resolve_roblox_username(user_id),
                        "role": group_role, "matched_name": candidate}
        raise RuntimeError(
            "I couldn't match your server nickname to a Roblox account in the configured group. "
            "Set your server nickname to your exact Roblox username and try again."
        )

    async def update_user_rank(self, user_id: int, rank_number: int, reason: str):
        if not self.api_key:
            raise RuntimeError("API key is not configured. Use `?api setkey <key>`.")
        return await self._request_json(
            "POST", f"{self.api_url}/api/roblox/setrank",
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json={"userId": int(user_id), "rankId": int(rank_number), "reason": reason or None},
        )

    @commands.group(name="api", invoke_without_command=True)
    @commands.guild_only()
    async def api(self, ctx: commands.Context):
        """Configuration and diagnostics commands."""
        await ctx.send(
            "**API commands**\n"
            "`?api setup` — show setup status and instructions\n"
            "`?api config` — show current non-secret configuration\n"
            "`?api setgroup <group_id>`\n"
            "`?api setrole <Discord role ID|@role>`\n"
            "`?api setminrank <rank>` — lowest rank permitted for assignments\n"
            "`?api setmaxrank <rank>` — highest rank permitted for assignments\n"
            "`?api setkey <API key>` — store API key in memory for this bot session\n"
            "`?api test` — test API key, endpoint and group roles\n"
            "`?search <username or user ID>` — inspect a Roblox user\n"
            "`?rank <username or user ID> <rank name or number> | <reason>`\n"
            "`?ranks` — list configured group ranks"
        )

    @api.command(name="setup")
    async def api_setup(self, ctx):
        await ctx.send(
            "**API Modmail setup**\n"
            "1. Run `?api setgroup <Roblox group ID>`.\n"
            "2. Run `?api setrole @Role` to restrict rank commands.\n"
            "3. Run `?api setminrank <rank>` and `?api setmaxrank <rank>`.\n"
            "4. Run `?api setkey <API key>` (kept in memory until restart).\n"
            "5. Run `?api test`, then `?api config`.\n"
            "Configuration commands require Discord Administrator permission. "
            "Never share your API key in a public channel; use a private staff channel."
        )

    @api.command(name="config")
    async def api_config(self, ctx):
        await ctx.send(
            f"**API configuration**\n"
            f"Endpoint: `{self.api_url}`\n"
            f"Roblox group ID: `{self.group_id}`\n"
            f"Minimum assignable rank: `{self.min_rank}`\n"
            f"Maximum assignable rank: `{self.max_rank}`\n"
            f"Discord role restriction: `{self.required_role_id if self.required_role_id is not None else 'NOT SET'}`\n"
            f"Requester minimum rank: `{self.requester_min_rank}`\n"
            f"API key: `{'configured' if self.api_key else 'NOT SET'}`"
        )

    @api.command(name="setgroup")
    async def api_setgroup(self, ctx, group_id: int):
        if not await self._is_admin(ctx):
            return await ctx.send("Only Discord administrators can change API configuration.")
        if group_id <= 0:
            return await ctx.send("Group ID must be a positive integer.")
        self.group_id = group_id
        self._clear_caches()
        try:
            self._save_config()
        except RuntimeError as exc:
            return await ctx.send(str(exc))
        await ctx.send(f"Roblox group ID saved as `{group_id}`.")

    @api.command(name="setrole")
    async def api_setrole(self, ctx, *, role_value: str):
        if not await self._is_admin(ctx):
            return await ctx.send("Only Discord administrators can change API configuration.")
        role_value = role_value.strip()
        role_id = None
        match = re.fullmatch(r"<@&(\d+)>", role_value)
        if match:
            role_id = int(match.group(1))
        elif role_value.isdigit():
            role_id = int(role_value)
        else:
            role = discord.utils.get(ctx.guild.roles, name=role_value)
            role_id = role.id if role else None
        if role_id is None or ctx.guild.get_role(role_id) is None:
            return await ctx.send("Couldn't find that role. Mention it (`@Role`) or provide its numeric ID.")
        self.required_role_id = role_id
        try:
            self._save_config()
        except RuntimeError as exc:
            return await ctx.send(str(exc))
        await ctx.send(f"Rank commands are now restricted to Discord role <@&{role_id}>. Setting saved.")

    @api.command(name="setminrank")
    async def api_setminrank(self, ctx, rank: int):
        if not await self._is_admin(ctx):
            return await ctx.send("Only Discord administrators can change API configuration.")
        if not 0 <= rank <= 255:
            return await ctx.send("Rank must be between 0 and 255.")
        self.min_rank = rank
        self._clear_caches()
        try:
            self._save_config()
        except RuntimeError as exc:
            return await ctx.send(str(exc))
        await ctx.send(f"Minimum assignable Roblox rank saved as `{rank}`.")

    @api.command(name="setmaxrank")
    async def api_setmaxrank(self, ctx, rank: int):
        if not await self._is_admin(ctx):
            return await ctx.send("Only Discord administrators can change API configuration.")
        if not 0 <= rank <= 255:
            return await ctx.send("Rank must be between 0 and 255.")
        if rank < self.min_rank:
            return await ctx.send(f"Maximum rank cannot be below the current minimum (`{self.min_rank}`).")
        self.max_rank = rank
        self._clear_caches()
        try:
            self._save_config()
        except RuntimeError as exc:
            return await ctx.send(str(exc))
        await ctx.send(f"Maximum assignable Roblox rank saved as `{rank}`.")

    @api.command(name="setkey")
    async def api_setkey(self, ctx, *, key: str):
        if not await self._is_admin(ctx):
            return await ctx.send("Only Discord administrators can change API configuration.")
        key = key.strip()
        if not key or len(key) > 500:
            return await ctx.send("Provide a valid non-empty API key.")
        self.api_key = key
        try:
            self._save_config()
        except RuntimeError as exc:
            return await ctx.send(str(exc))
        await ctx.send(
            "API key saved to the plugin's local config file. Keep the file private and "
            "never post this command in a public channel. If you exposed the key, rotate it."
        )

    @api.command(name="test")
    async def api_test(self, ctx):
        if not await self._is_admin(ctx):
            return await ctx.send("Only Discord administrators can run API diagnostics.")
        checks = []
        if not self.api_key:
            checks.append("❌ API key is not configured")
        else:
            checks.append("✅ API key is present (not displayed)")
        try:
            roles = await self.fetch_group_roles(force_refresh=True)
            checks.append(f"✅ Roblox group reachable; {len(roles)} role(s) returned")
        except Exception as exc:
            checks.append(f"❌ Roblox group lookup failed: {str(exc)[:180]}")
        try:
            await self._request_json("GET", f"{self.api_url}/")
            checks.append("✅ API endpoint root responded")
        except Exception as exc:
            # A 404 at the root can be normal; report endpoint reachability without exposing response body.
            if "HTTP 404" in str(exc) or "HTTP 405" in str(exc):
                checks.append("⚠️ API host is reachable, but its root route is not available")
            else:
                checks.append(f"❌ API host test failed: {str(exc)[:180]}")
        await ctx.send("**API test results**\n" + "\n".join(checks))

    @commands.command(name="ranks", aliases=["apiranks"])
    @commands.guild_only()
    async def api_ranks(self, ctx: commands.Context):
        if not self.has_discord_role_permission(ctx):
            return await ctx.send("You do not have permission to use this command.")
        try:
            roles = await self.fetch_group_roles()
        except Exception:
            return await ctx.send("I couldn't fetch the Roblox group ranks. Check the group ID and try again.")
        roles = [r for r in roles if self.min_rank <= int(r.get("rank", 0)) <= self.max_rank]
        if not roles:
            return await ctx.send("No ranks were returned within the configured minimum/maximum range.")
        lines = [f"`{r.get('rank', '?')}` — {discord.utils.escape_markdown(str(r.get('name', 'Unnamed')))}"
                 for r in roles[:40]]
        if len(roles) > 40:
            lines.append(f"…and {len(roles) - 40} more.")
        embed = discord.Embed(title="Roblox Group Ranks", description="\n".join(lines), color=self.embed_color)
        embed.set_footer(text=f"Group ID: {self.group_id}")
        await ctx.send(embed=embed)

    @commands.command(name="search")
    @commands.guild_only()
    async def search(self, ctx, *, username_or_id: str):
        if not self.has_discord_role_permission(ctx):
            return await ctx.send("You do not have permission to use this command.")
        try:
            user_id = await self.resolve_roblox_user_id(username_or_id)
            if not user_id:
                return await ctx.send(f"Could not find Roblox user `{discord.utils.escape_markdown(username_or_id)}`.")
            username = await self.resolve_roblox_username(user_id)
            membership = await self.fetch_user_group_rank(user_id)
            embed = discord.Embed(title="Roblox User Lookup", color=self.embed_color)
            embed.add_field(name="Username", value=username, inline=True)
            embed.add_field(name="User ID", value=str(user_id), inline=True)
            if membership:
                embed.add_field(name="Group Rank", value=f"{membership['name']} ({membership['rank']})", inline=False)
            else:
                embed.add_field(name="Group Membership", value="Not a member of the configured group", inline=False)
            await ctx.send(embed=embed)
        except Exception as exc:
            await ctx.send(f"Lookup failed: {str(exc)[:200]}")

    @commands.command(name="rank")
    @commands.guild_only()
    async def rank(self, ctx: commands.Context, username: str, *, details: str):
        """Set a Roblox group rank. Format: ?rank username rank name or number | reason"""
        if not self.has_discord_role_permission(ctx):
            return await ctx.send("You do not have permission to use this command.")
        if "|" not in details:
            return await ctx.send("Include `|` between the target rank and reason.\nExample: `?rank BuilderUser Senior Builder | Promotion approved`")
        rank_input, reason = (part.strip() for part in details.split("|", 1))
        if not rank_input or not reason:
            return await ctx.send("Both a target rank and a reason are required.")
        status_message = await ctx.send("Verifying your Roblox group rank and the target member…")
        try:
            requester = await self.verify_requester(ctx)
            requester_rank = int(requester["role"]["rank"])
            if requester_rank < self.requester_min_rank:
                return await status_message.edit(content="Your Roblox group rank is below the minimum allowed to use this command.")
            user_id = await self.resolve_roblox_user_id(username)
            if not user_id:
                return await status_message.edit(content=f"Could not find Roblox user `{discord.utils.escape_markdown(username)}`.")
            roles = await self.fetch_group_roles()
            target_role = self.find_role(rank_input, roles)
            if not target_role or not self.min_rank <= int(target_role.get("rank", 0)) <= self.max_rank:
                return await status_message.edit(content=f"Rank `{discord.utils.escape_markdown(rank_input)}` not found or outside configured range ({self.min_rank}–{self.max_rank}).")
            target_rank_number = int(target_role["rank"])
            if requester_rank <= target_rank_number:
                return await status_message.edit(content=f"Permission denied: your Roblox rank ({requester_rank}) must be higher than the rank you are assigning ({target_rank_number}).")
            target_current_role = await self.fetch_user_group_rank(user_id)
            if target_current_role is None:
                return await status_message.edit(content="The target user is not a member of the configured Roblox group.")
            if requester_rank <= int(target_current_role["rank"]):
                return await status_message.edit(content=f"Permission denied: your Roblox rank ({requester_rank}) must be higher than the target member's current rank ({target_current_role['rank']}).")
            resolved_username = await self.resolve_roblox_username(user_id)
            await self.update_user_rank(user_id, target_rank_number, reason)
            embed = discord.Embed(title="Roblox Rank Updated", color=self.embed_color)
            embed.add_field(name="Target Username", value=resolved_username, inline=True)
            embed.add_field(name="New Rank", value=str(target_role.get("name", "Unnamed")), inline=True)
            embed.add_field(name="Rank Number", value=str(target_rank_number), inline=True)
            embed.add_field(name="Reason", value=reason[:1024], inline=False)
            embed.add_field(name="Requested By", value=f"{ctx.author.mention} — {requester['username']} ({requester['role']['name']}, rank {requester_rank})", inline=False)
            embed.set_footer(text=f"Target Roblox user ID: {user_id}")
            embed.timestamp = discord.utils.utcnow()
            await status_message.edit(content=None, embed=embed)
            log.info("%s (%s, rank %s) changed Roblox user %s (%s) to %s (%s)",
                     ctx.author, requester["username"], requester_rank, resolved_username, user_id,
                     target_role.get("name"), target_rank_number)
        except Exception as exc:
            log.warning("API rank update failed: %s", exc)
            await status_message.edit(content=f"Rank update blocked/failed: {str(exc)[:300]}")


async def setup(bot):
    await bot.add_cog(APIModmail(bot))
