"""GameVox Bot client built on discord.py (Discord API v10 compatible)."""

from __future__ import annotations

import io
import random
import re
import time
import uuid
from datetime import UTC, datetime, timedelta

import discord
import yarl
from loguru import logger

from wows_bot.clients.wg_api import WGApiClient, WGApiError
from wows_bot.clients.yuyuko_rank import YuyukoRankClient
from wows_bot.config import Language, Realm, Settings, normalize_realm
from wows_bot.db.repository import Repository, ShipMeta
from wows_bot.i18n import I18n
from wows_bot.services.pr import calculate_pr_single_ship, classify_pr
from wows_bot.services.renderer import CardRenderer
from wows_bot.services.stats_builder import (
    build_player_overview,
    build_recent_stats_view,
    compact_ship_snapshot,
    extract_mode_bucket,
    summarize_mode_across_ships,
)

MENTION_RE = re.compile(r"<@!?(\d+)>")
_GAMEVOX_PATCHED = False


def _sanitize_echo(text: str, max_len: int = 80) -> str:
    """Strip backticks, newlines, and control chars before echoing user input in chat."""
    cleaned = text.replace("`", "").replace("\r", " ").replace("\n", " ").strip()
    return cleaned[:max_len]


def patch_discord_for_gamevox(settings: Settings) -> None:
    """Point discord.py REST + Gateway endpoints to GameVox (Discord API v10)
    and apply defensive payload compatibility shims for fields GameVox omits.
    """
    global _GAMEVOX_PATCHED
    discord.http.Route.BASE = settings.gamevox_api_base.rstrip("/")
    discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(settings.gamevox_gateway_url)

    if not _GAMEVOX_PATCHED:
        # 1. AppInfo: GameVox omits `owner` and may omit optional metadata fields
        orig_appinfo_init = discord.AppInfo.__init__

        def _compat_appinfo_init(self, state, data):
            if isinstance(data, dict):
                patched = {
                    "description": "",
                    "icon": None,
                    "bot_public": False,
                    "bot_require_code_grant": False,
                    "verify_key": "",
                    **data,
                }
                if not patched.get("owner"):
                    patched["owner"] = {
                        "id": str(patched.get("id") or "0"),
                        "username": str(patched.get("name") or "gamevox_owner"),
                        "discriminator": "0",
                        "avatar": None,
                    }
                data = patched
            return orig_appinfo_init(self, state, data)

        discord.AppInfo.__init__ = _compat_appinfo_init  # type: ignore[method-assign]

        # 2. Channels: GameVox voice/text/category/forum channels can omit bitrate, user_limit, position
        from discord.channel import CategoryChannel, ForumChannel, TextChannel, VocalGuildChannel

        orig_vocal_update = VocalGuildChannel._update

        def _compat_vocal_update(self, guild, data):
            if isinstance(data, dict):
                data = {"bitrate": 64000, "user_limit": 0, "position": 0, **data}
                if data.get("position") is None:
                    data["position"] = 0
                if data.get("bitrate") is None:
                    data["bitrate"] = 64000
                if data.get("user_limit") is None:
                    data["user_limit"] = 0
            return orig_vocal_update(self, guild, data)

        VocalGuildChannel._update = _compat_vocal_update  # type: ignore[method-assign]

        for ch_cls in (TextChannel, CategoryChannel, ForumChannel):
            orig_ch_update = ch_cls._update

            def _make_ch_update(orig_fn):
                def _compat_ch_update(self, guild, data):
                    if isinstance(data, dict) and data.get("position") is None:
                        data = {**data, "position": 0}
                    return orig_fn(self, guild, data)

                return _compat_ch_update

            ch_cls._update = _make_ch_update(orig_ch_update)  # type: ignore[method-assign]

        # 3. BaseUser / Member: defensively default discriminator, avatar, roles, flags
        from discord.member import Member
        from discord.user import BaseUser

        orig_user_update = BaseUser._update

        def _compat_user_update(self, data):
            if isinstance(data, dict):
                data = {"discriminator": "0", "avatar": None, **data}
            return orig_user_update(self, data)

        BaseUser._update = _compat_user_update  # type: ignore[method-assign]

        orig_member_init = Member.__init__

        def _compat_member_init(self, *, data, guild, state):
            if isinstance(data, dict):
                data = {"roles": [], "flags": 0, **data}
                if data.get("flags") is None:
                    data["flags"] = 0
                if data.get("roles") is None:
                    data["roles"] = []
            return orig_member_init(self, data=data, guild=guild, state=state)

        Member.__init__ = _compat_member_init  # type: ignore[method-assign]

        # 4. Message & Attachment: prevent KeyError AFTER an image is already posted (avoids duplicate retries!)
        orig_msg_init = discord.Message.__init__

        def _compat_msg_init(self, *, state, channel, data):
            if isinstance(data, dict):
                data = {"type": 0, "content": "", **data}
                if data.get("content") is None:
                    data["content"] = ""
                if data.get("type") is None:
                    data["type"] = 0
            return orig_msg_init(self, state=state, channel=channel, data=data)

        discord.Message.__init__ = _compat_msg_init  # type: ignore[method-assign]

        orig_att_init = discord.Attachment.__init__

        def _compat_att_init(self, *, data, state):
            if isinstance(data, dict):
                url_val = str(data.get("url") or "")
                data = {
                    "size": 0,
                    "filename": "image.png",
                    "url": url_val,
                    "proxy_url": data.get("proxy_url") or url_val,
                    **data,
                }
                if not data.get("proxy_url"):
                    data["proxy_url"] = url_val
            return orig_att_init(self, data=data, state=state)

        discord.Attachment.__init__ = _compat_att_init  # type: ignore[method-assign]

        # 5. READY payload: ensure resume_gateway_url & application.flags exist
        from discord.state import ConnectionState

        orig_parse_ready = ConnectionState.parse_ready

        def _compat_parse_ready(self, data):
            if isinstance(data, dict):
                if not data.get("resume_gateway_url"):
                    data = {**data, "resume_gateway_url": str(discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY)}
                app = data.get("application")
                if isinstance(app, dict) and app.get("flags") is None:
                    data = {**data, "application": {**app, "flags": 0}}
            return orig_parse_ready(self, data)

        ConnectionState.parse_ready = _compat_parse_ready  # type: ignore[method-assign]

        _GAMEVOX_PATCHED = True

    logger.info(
        "Patched discord.py endpoints -> REST={}, Gateway={}",
        discord.http.Route.BASE,
        discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY,
    )


class WoWSGameVoxBot(discord.Client):
    def __init__(
        self,
        *,
        settings: Settings,
        repo: Repository,
        wg: WGApiClient,
        yuyuko_rank: YuyukoRankClient,
        renderer: CardRenderer,
        i18n: I18n,
        on_ready_hook=None,
    ) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=False, replied_user=True
            ),
        )

        self.settings = settings
        self.repo = repo
        self.wg = wg
        self.yuyuko_rank = yuyuko_rank
        self.renderer = renderer
        self.i18n = i18n
        self._on_ready_hook = on_ready_hook
        self._ready_once = False
        self._bot_mention_re: re.Pattern[str] | None = None
        self._cooldowns: dict[str, float] = {}

    def is_gateway_healthy(self) -> bool:
        return not self.is_closed() and self.is_ready()

    async def on_ready(self) -> None:
        logger.info("GameVox bot logged in as {} (id={})", self.user, getattr(self.user, "id", None))
        if self.user is not None:
            self._bot_mention_re = re.compile(rf"^<@!?{self.user.id}>\s*")
        if self.settings.default_guild_id and self.settings.default_auto_post_channel_id:
            self.repo.seed_guild_auto_post_channel_if_unset(
                self.settings.default_guild_id,
                self.settings.default_auto_post_channel_id,
                default_lang=self.settings.default_language,
            )
        if not self._ready_once and self._on_ready_hook is not None:
            await self._on_ready_hook()
            self._ready_once = True

    def _is_guild_admin(self, message: discord.Message) -> bool:
        user_id = str(message.author.id)
        if user_id in self.settings.admin_user_id_set:
            return True
        if message.guild is None:
            return False
        if message.guild.owner_id is not None and message.author.id == message.guild.owner_id:
            return True
        if isinstance(message.author, discord.Member):
            perms = message.author.guild_permissions
            return bool(perms.administrator or perms.manage_guild or perms.manage_channels)
        return False

    def _check_cooldown(self, user_id: str) -> float:
        now = time.monotonic()
        last = self._cooldowns.get(user_id, 0.0)
        remaining = self.settings.command_cooldown_seconds - (now - last)
        if remaining > 0:
            return remaining
        self._cooldowns[user_id] = now
        return 0.0

    async def _safe_reply(
        self,
        message: discord.Message,
        content: str | None = None,
        *,
        file: discord.File | None = None,
    ) -> None:
        try:
            ref = message.to_reference(fail_if_not_exists=False)
            await message.channel.send(content=content, file=file, reference=ref)
        except discord.HTTPException:
            if file is not None:
                file.fp.seek(0)
            await message.channel.send(content=content, file=file)

    async def deliver_image(self, channel_id: str, image_bytes: bytes, filename: str) -> str | None:
        """Send a rendered PNG/JPEG stat card to a GameVox text channel."""
        cid = int(channel_id)
        channel = self.get_channel(cid)
        if channel is None:
            channel = await self.fetch_channel(cid)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"Channel {channel_id} is not messageable")
        msg = await channel.send(file=discord.File(io.BytesIO(image_bytes), filename=filename))
        return str(msg.id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self.user is not None and message.author.id == self.user.id:
            return
        content = (message.content or "").strip()
        if not content:
            return

        prefix = self.settings.command_prefix.lower()
        if self._bot_mention_re is not None:
            content = self._bot_mention_re.sub("", content).strip()

        lowered = content.lower()
        if not (lowered == prefix or lowered.startswith(prefix + " ")):
            return

        raw_args = content[len(prefix) :].strip()
        guild_id = str(message.guild.id) if message.guild else None
        user_id = str(message.author.id)
        lang = self.repo.resolve_language(
            user_id=user_id,
            guild_id=guild_id,
            fallback=self.settings.default_language,
        )

        # Resolve optional @mention ONLY for read/admin commands
        mentioned_user_id: str | None = None
        mention_match = MENTION_RE.search(raw_args)
        if mention_match:
            mentioned_user_id = mention_match.group(1)
            raw_args = MENTION_RE.sub("", raw_args).strip()

        try:
            await self._dispatch(message, raw_args, user_id, mentioned_user_id, guild_id, lang)
        except WGApiError as exc:
            if "WG_APPLICATION_ID" in str(exc):
                await self._safe_reply(message, self.i18n.t("bot.wg_key_missing", lang=lang))
            else:
                err_id = uuid.uuid4().hex[:8]
                logger.warning("WG API error [{}]: {}", err_id, exc)
                await self._safe_reply(message, self.i18n.t("bot.error_generic", lang=lang, error_id=err_id))
        except Exception as exc:
            err_id = uuid.uuid4().hex[:8]
            logger.exception("Command failed [{}] ({})", err_id, type(exc).__name__)
            await self._safe_reply(message, self.i18n.t("bot.error_generic", lang=lang, error_id=err_id))

    async def _send_image_reply(
        self, message: discord.Message, image_bytes: bytes, stem: str
    ) -> None:
        filename = f"{stem}.{self.settings.image_file_ext}"
        await self._safe_reply(message, file=discord.File(io.BytesIO(image_bytes), filename=filename))

    async def _dispatch(
        self,
        message: discord.Message,
        raw_args: str,
        user_id: str,
        mentioned_user_id: str | None,
        guild_id: str | None,
        lang: Language,
    ) -> None:
        prefix = self.settings.command_prefix
        tokens = raw_args.split()
        if not tokens or tokens[0].lower() in ("help", "帮助"):
            await self._safe_reply(message, self.i18n.t("bot.help_text", lang=lang, prefix=prefix))
            return

        cmd = tokens[0].lower()
        rest = tokens[1:]

        # --- Language (personal for anyone; `server` requires guild admin) ---
        if cmd in ("lang", "language", "语言"):
            if not rest or rest[0].lower() not in ("zh", "en"):
                await self._safe_reply(message, self.i18n.t("bot.lang_invalid", lang=lang))
                return
            new_lang: Language = rest[0].lower()  # type: ignore[assignment]
            if len(rest) > 1 and rest[1].lower() in ("server", "guild", "服务器"):
                if not self._is_guild_admin(message) or not guild_id:
                    await self._safe_reply(message, self.i18n.t("bot.permission_denied", lang=lang))
                    return
                self.repo.upsert_guild_settings(
                    guild_id, default_lang=new_lang, fallback_lang=self.settings.default_language
                )
                await self._safe_reply(message, self.i18n.t("bot.lang_server_set", lang=new_lang))
                return
            self.repo.set_user_language(user_id, new_lang)
            await self._safe_reply(message, self.i18n.t("bot.lang_set", lang=new_lang))
            return

        # --- Set default auto-post channel (admin only) ---
        if cmd in ("channel", "setchannel", "设置频道"):
            if not guild_id or not self._is_guild_admin(message):
                await self._safe_reply(message, self.i18n.t("bot.permission_denied", lang=lang))
                return
            self.repo.upsert_guild_settings(
                guild_id,
                auto_post_channel_id=str(message.channel.id),
                fallback_lang=self.settings.default_language,
            )
            await self._safe_reply(message, self.i18n.t("bot.channel_set", lang=lang))
            return

        # --- Bind account (always binds caller's own GameVox user_id) ---
        if cmd in ("bind", "绑定"):
            if len(rest) < 2:
                await self._safe_reply(message, self.i18n.t("bot.bind_missing_args", lang=lang, prefix=prefix))
                return
            realm = normalize_realm(rest[0])
            if realm is None:
                await self._safe_reply(message, self.i18n.t("bot.bind_missing_args", lang=lang, prefix=prefix))
                return
            nick = " ".join(rest[1:]).strip()
            matches = await self.wg.search_account(realm, nick, limit=5)
            exact = next((m for m in matches if str(m.get("nickname", "")).lower() == nick.lower()), None)
            chosen = exact or (matches[0] if matches else None)
            if not chosen:
                await self._safe_reply(
                    message,
                    self.i18n.t(
                        "bot.bind_not_found",
                        lang=lang,
                        realm=self.i18n.t(f"realms.{realm}", lang=lang),
                        nickname=_sanitize_echo(nick),
                    ),
                )
                return

            aid = int(chosen["account_id"])
            real_nick = str(chosen["nickname"])
            gs = self.repo.get_guild_settings(guild_id) if guild_id else None
            # Only apply deployment-wide default channel when guild matches default_guild_id!
            default_ch = (
                self.settings.default_auto_post_channel_id
                if guild_id and guild_id == self.settings.default_guild_id
                else None
            )
            auto_ch = (gs["auto_post_channel_id"] if gs else None) or default_ch

            self.repo.bind_account(
                gamevox_user_id=user_id,
                realm=realm,
                account_id=aid,
                nickname=real_nick,
                guild_id=guild_id,
                auto_post_channel_id=auto_ch if guild_id else None,
            )
            if self.repo.dbm.poll_queue is not None:
                self.repo.dbm.poll_queue.enqueue(
                    {"type": "fetch_player_diff", "realm": realm, "account_id": aid, "nickname": real_nick}
                )
            await self._safe_reply(
                message,
                self.i18n.t(
                    "bot.bind_success",
                    lang=lang,
                    realm=self.i18n.t(f"realms.{realm}", lang=lang),
                    nickname=real_nick,
                    account_id=aid,
                ),
            )
            return

        # --- Toggle auto-post tracking (cross-user requires guild admin) ---
        if cmd in ("track", "追踪", "推送"):
            if mentioned_user_id and mentioned_user_id != user_id and not self._is_guild_admin(message):
                await self._safe_reply(message, self.i18n.t("bot.permission_denied", lang=lang))
                return
            target_uid = mentioned_user_id or user_id
            binding = self.repo.get_default_binding(target_uid)
            if binding is None:
                await self._safe_reply(message, self.i18n.t("bot.not_bound", lang=lang, prefix=prefix))
                return
            enabled = not (rest and rest[0].lower() in ("off", "0", "false", "关", "关闭"))
            if guild_id:
                # Non-admins can only toggle tracking in the guild's designated auto-post channel (if set)
                gs = self.repo.get_guild_settings(guild_id)
                designated_ch = gs["auto_post_channel_id"] if gs else None
                if (
                    enabled
                    and designated_ch
                    and str(message.channel.id) != str(designated_ch)
                    and not self._is_guild_admin(message)
                ):
                    await self._safe_reply(message, self.i18n.t("bot.permission_denied", lang=lang))
                    return
                self.repo.set_subscription(
                    realm=binding.realm,
                    account_id=binding.account_id,
                    nickname=binding.nickname,
                    guild_id=guild_id,
                    channel_id=str(message.channel.id),
                    user_id=user_id,
                    enabled=enabled,
                )
            key = "bot.track_enabled" if enabled else "bot.track_disabled"
            await self._safe_reply(
                message,
                self.i18n.t(
                    key,
                    lang=lang,
                    realm=self.i18n.t(f"realms.{binding.realm}", lang=lang),
                    nickname=binding.nickname,
                ),
            )
            return

        # --- Heavy / render commands: apply per-user cooldown ---
        remaining = self._check_cooldown(user_id)
        if remaining > 0:
            await self._safe_reply(
                message, self.i18n.t("bot.cooldown_active", lang=lang, seconds=max(1, int(round(remaining))))
            )
            return

        read_target_uid = mentioned_user_id or user_id

        # --- Last single battle / division match ---
        if cmd in ("last", "recents", "单局", "单场近期"):
            resolved = await self._resolve_target_account(rest, read_target_uid, lang)
            if isinstance(resolved, str):
                await self._safe_reply(message, resolved)
                return
            realm, aid, nick = resolved
            group = self.repo.get_latest_match_group_for_account(realm, aid)
            if not group:
                await self._safe_reply(message, self.i18n.t("bot.no_last_match", lang=lang, nickname=nick))
                return
            ships_meta = self.repo.get_ships_map([r["ship_id"] for r in group["records"]])
            img = await self.renderer.render_match_group(group, ships_meta, lang=lang)
            await self._send_image_reply(message, img, f"match_{group['id']}")
            return

        # --- Daily recent ---
        if cmd in ("recent", "近期"):
            days = 7
            remaining_toks = list(rest)
            if remaining_toks and remaining_toks[-1].isdigit():
                days = max(1, min(90, int(remaining_toks.pop())))
            resolved = await self._resolve_target_account(remaining_toks, read_target_uid, lang)
            if isinstance(resolved, str):
                await self._safe_reply(message, resolved)
                return
            realm, aid, nick = resolved
            since_date = (datetime.now(tz=UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
            baseline = self.repo.get_daily_snapshot_on_or_before(realm, aid, since_date)
            if baseline is None:
                await self._safe_reply(
                    message, self.i18n.t("bot.no_recent_data", lang=lang, nickname=nick, days=days)
                )
                return
            raw_ships = await self.wg.get_account_ships_stats(realm, aid)
            cur_snaps = {int(e["ship_id"]): compact_ship_snapshot(e) for e in raw_ships if e.get("ship_id")}
            clan = await self.wg.get_account_clan(realm, aid)
            ships_meta = self.repo.get_ships_map(list(cur_snaps.keys()))
            expected_map = self.repo.get_pr_expected_map()
            view = build_recent_stats_view(
                realm=realm,
                account_id=aid,
                nickname=nick,
                clan_tag=str(clan["tag"]) if clan else "",
                days=days,
                since_date=baseline["snapshot_date"],
                current_snapshots=cur_snaps,
                baseline_ships=baseline["ships"],
                ships_meta=ships_meta,
                expected_map=expected_map,
                lang=lang,
            )
            if view is None:
                await self._safe_reply(
                    message, self.i18n.t("bot.no_recent_data", lang=lang, nickname=nick, days=days)
                )
                return
            img = await self.renderer.render_recent_summary(view, lang=lang)
            await self._send_image_reply(message, img, f"recent_{aid}_{days}d")
            return

        # --- Single ship stats ---
        if cmd in ("ship", "单船"):
            if not rest:
                await self._safe_reply(message, f"Usage: `{prefix} ship <ship_name>`")
                return
            realm_opt = normalize_realm(rest[0]) if len(rest) >= 3 else None
            if realm_opt:
                resolved = await self._resolve_target_account(rest[:2], read_target_uid, lang)
                ship_query = " ".join(rest[2:])
            else:
                resolved = await self._resolve_target_account([], read_target_uid, lang)
                ship_query = " ".join(rest)
            if isinstance(resolved, str):
                await self._safe_reply(message, resolved)
                return
            realm, aid, nick = resolved
            ship = await self._resolve_ship(message, ship_query, lang)
            if ship is None:
                return
            entries = await self.wg.get_account_ships_stats(realm, aid, ship_id=ship.ship_id)
            if not entries:
                await self._safe_reply(
                    message, self.i18n.t("bot.ship_not_found", lang=lang, query=ship.display_name(lang))
                )
                return
            entry = entries[0]
            clan = await self.wg.get_account_clan(realm, aid)
            expected_map = self.repo.get_pr_expected_map()
            pvp = summarize_mode_across_ships([entry], "pvp", expected_map)
            modes = {
                m: summarize_mode_across_ships([entry], m, expected_map)
                for m in ("pvp_solo", "pvp_div2", "pvp_div3", "rank_solo")
            }
            pvp_bucket = extract_mode_bucket(entry, "pvp")
            view = {
                "realm": realm,
                "account_id": aid,
                "nickname": nick,
                "clan_tag": str(clan["tag"]) if clan else "",
                "ship_name": ship.display_name(lang),
                "ship_tier": ship.tier,
                "ship_type": ship.ship_type,
                "ship_icon": ship.icon_medium,
                "pvp": pvp,
                "modes": modes,
                "max_damage": pvp_bucket["max_damage_dealt"],
                "max_frags": pvp_bucket["max_frags_battle"],
            }
            img = await self.renderer.render_ship_stats(view, lang=lang)
            await self._send_image_reply(message, img, f"ship_{aid}_{ship.ship_id}")
            return

        # --- Ship leaderboard ---
        if cmd in ("ship.rank", "rank", "排行榜"):
            if len(rest) < 2:
                await self._safe_reply(message, f"Usage: `{prefix} ship.rank <na|eu|asia> <ship_name>`")
                return
            realm = normalize_realm(rest[0])
            if realm is None:
                await self._safe_reply(message, f"Usage: `{prefix} ship.rank <na|eu|asia> <ship_name>`")
                return
            ship = await self._resolve_ship(message, " ".join(rest[1:]), lang)
            if ship is None:
                return
            entries = await self.yuyuko_rank.fetch_ship_rank(realm, ship.ship_id)
            is_global = entries is not None and len(entries) > 0
            if not is_global:
                entries = self.repo.get_local_ship_leaderboard(realm, ship.ship_id)
                expected = self.repo.get_pr_expected_map().get(ship.ship_id)
                for e in entries:
                    b = e["battles"]
                    e["pr"] = classify_pr(
                        calculate_pr_single_ship(
                            b, e["avg_damage"] * b, e["avg_frags"] * b, e["win_rate"] / 100.0 * b, expected
                        )
                    ).value
            if not entries:
                await self._safe_reply(
                    message, self.i18n.t("bot.ship_not_found", lang=lang, query=ship.display_name(lang))
                )
                return
            img = await self.renderer.render_ship_rank(
                realm=realm, ship=ship, entries=entries, is_global=is_global, lang=lang
            )
            await self._send_image_reply(message, img, f"rank_{realm}_{ship.ship_id}")
            return

        # --- Clan info ---
        if cmd in ("clan", "军团"):
            if len(rest) < 2:
                await self._safe_reply(message, f"Usage: `{prefix} clan <na|eu|asia> <TAG>`")
                return
            realm = normalize_realm(rest[0])
            if realm is None:
                await self._safe_reply(message, f"Usage: `{prefix} clan <na|eu|asia> <TAG>`")
                return
            tag = rest[1].strip()
            clans = await self.wg.search_clan(realm, tag, limit=5)
            chosen = next((c for c in clans if str(c.get("tag", "")).lower() == tag.lower()), None) or (
                clans[0] if clans else None
            )
            if not chosen:
                await self._safe_reply(
                    message,
                    self.i18n.t(
                        "bot.clan_not_found",
                        lang=lang,
                        realm=self.i18n.t(f"realms.{realm}", lang=lang),
                        tag=_sanitize_echo(tag),
                    ),
                )
                return
            detail = await self.wg.get_clan_info(realm, int(chosen["clan_id"]))
            if not detail:
                await self._safe_reply(
                    message,
                    self.i18n.t(
                        "bot.clan_not_found",
                        lang=lang,
                        realm=self.i18n.t(f"realms.{realm}", lang=lang),
                        tag=_sanitize_echo(tag),
                    ),
                )
                return
            members_raw = list((detail.get("members") or {}).values())
            members_raw.sort(key=lambda m: int(m.get("joined_at") or 0))
            members = [
                {
                    "account_name": m.get("account_name", ""),
                    "role": m.get("role", ""),
                    "joined_at_str": datetime.fromtimestamp(int(m.get("joined_at") or 0), tz=UTC).strftime("%Y-%m-%d"),
                }
                for m in members_raw
            ]
            created_ts = int(detail.get("created_at") or 0)
            view = {
                "realm": realm,
                "clan_id": detail.get("clan_id"),
                "tag": detail.get("tag", ""),
                "name": detail.get("name", ""),
                "description": detail.get("description", ""),
                "leader_name": detail.get("leader_name", ""),
                "members_count": detail.get("members_count", len(members)),
                "created_at_str": datetime.fromtimestamp(created_ts, tz=UTC).strftime("%Y-%m-%d") if created_ts else "-",
                "members": members,
            }
            img = await self.renderer.render_clan_info(view, lang=lang)
            await self._send_image_reply(message, img, f"clan_{realm}_{view['tag']}")
            return

        # --- Roll random ship ---
        if cmd in ("roll", "随机"):
            tier_filter: int | None = None
            type_filter: str | None = None
            for tok in rest:
                if tok.isdigit():
                    tier_filter = int(tok)
                else:
                    lt = tok.lower()
                    type_aliases = {
                        "dd": "Destroyer", "驱逐": "Destroyer",
                        "ca": "Cruiser", "cl": "Cruiser", "巡洋": "Cruiser",
                        "bb": "Battleship", "战列": "Battleship",
                        "cv": "AirCarrier", "航母": "AirCarrier",
                        "ss": "Submarine", "潜艇": "Submarine",
                    }
                    if lt in type_aliases:
                        type_filter = type_aliases[lt]
            ships = list(self.repo.get_ships_map().values())
            if tier_filter is not None:
                ships = [s for s in ships if s.tier == tier_filter]
            if type_filter is not None:
                ships = [s for s in ships if s.ship_type == type_filter]
            if not ships:
                await self._safe_reply(message, self.i18n.t("bot.roll_empty", lang=lang))
                return
            pick = random.choice(ships)
            await self._safe_reply(
                message,
                self.i18n.t(
                    "bot.roll_result",
                    lang=lang,
                    tier=pick.tier,
                    ship_name=pick.display_name(lang),
                    ship_type=self.i18n.t(f"ship_types.{pick.ship_type}", lang=lang),
                    nation=pick.nation,
                ),
            )
            return

        # --- Default: Player overview (`wws me` or `wws <realm> <nickname>`) ---
        query_tokens = [] if cmd == "me" else tokens
        resolved = await self._resolve_target_account(query_tokens, read_target_uid, lang)
        if isinstance(resolved, str):
            await self._safe_reply(message, resolved)
            return
        realm, aid, nick = resolved
        batch = await self.wg.get_accounts_info_batch(realm, [aid])
        acc_info = batch.get(aid)
        if not acc_info:
            await self._safe_reply(
                message,
                self.i18n.t(
                    "bot.bind_not_found",
                    lang=lang,
                    realm=self.i18n.t(f"realms.{realm}", lang=lang),
                    nickname=_sanitize_echo(nick),
                ),
            )
            return
        if acc_info.get("hidden_profile"):
            await self._safe_reply(message, self.i18n.t("bot.hidden_profile", lang=lang, nickname=nick))
            return
        ships_stats = await self.wg.get_account_ships_stats(realm, aid)
        clan = await self.wg.get_account_clan(realm, aid)
        ships_meta = self.repo.get_ships_map([int(s["ship_id"]) for s in ships_stats if s.get("ship_id")])
        expected_map = self.repo.get_pr_expected_map()
        view = build_player_overview(
            realm=realm,
            account_info=acc_info,
            ships_stats=ships_stats,
            clan_info=clan,
            ships_meta=ships_meta,
            expected_map=expected_map,
            lang=lang,
        )
        img = await self.renderer.render_player_overview(view, lang=lang)
        await self._send_image_reply(message, img, f"overview_{realm}_{aid}")

    async def _resolve_target_account(
        self,
        tokens: list[str],
        target_user_id: str,
        lang: Language,
    ) -> tuple[Realm, int, str] | str:
        if not tokens:
            binding = self.repo.get_default_binding(target_user_id)
            if binding is None:
                return self.i18n.t("bot.not_bound", lang=lang, prefix=self.settings.command_prefix)
            return binding.realm, binding.account_id, binding.nickname

        realm = normalize_realm(tokens[0])
        if realm is None or len(tokens) < 2:
            return self.i18n.t("bot.help_text", lang=lang, prefix=self.settings.command_prefix)
        nick = " ".join(tokens[1:]).strip()
        matches = await self.wg.search_account(realm, nick, limit=5)
        exact = next((m for m in matches if str(m.get("nickname", "")).lower() == nick.lower()), None)
        chosen = exact or (matches[0] if matches else None)
        if not chosen:
            return self.i18n.t(
                "bot.bind_not_found",
                lang=lang,
                realm=self.i18n.t(f"realms.{realm}", lang=lang),
                nickname=_sanitize_echo(nick),
            )
        return realm, int(chosen["account_id"]), str(chosen["nickname"])

    async def _resolve_ship(
        self, message: discord.Message, query: str, lang: Language
    ) -> ShipMeta | None:
        matches = self.repo.search_ships(query, limit=6)
        if not matches:
            await self._safe_reply(
                message, self.i18n.t("bot.ship_not_found", lang=lang, query=_sanitize_echo(query))
            )
            return None
        q_low = query.strip().lower()
        for m in matches:
            if m.name_en.lower() == q_low or m.name_zh.lower() == q_low:
                return m
        if len(matches) == 1:
            return matches[0]
        choices = "\n".join(f"• T{m.tier} {m.name_zh} ({m.name_en})" for m in matches)
        await self._safe_reply(message, self.i18n.t("bot.ship_multiple", lang=lang, choices=choices))
        return None
