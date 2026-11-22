"""GameVox Bot client built on discord.py (Discord API v10 compatible)."""

from __future__ import annotations

import io
import random
import re
from datetime import UTC, datetime, timedelta

import discord
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


def patch_discord_for_gamevox(settings: Settings) -> None:
    """Point discord.py REST + Gateway endpoints to GameVox (Discord API v10)."""
    discord.http.Route.BASE = settings.gamevox_api_base.rstrip("/")
    discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = settings.gamevox_gateway_url
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
        super().__init__(intents=intents)

        self.settings = settings
        self.repo = repo
        self.wg = wg
        self.yuyuko_rank = yuyuko_rank
        self.renderer = renderer
        self.i18n = i18n
        self._on_ready_hook = on_ready_hook
        self._ready_once = False

    async def on_ready(self) -> None:
        logger.info("GameVox bot logged in as {} (id={})", self.user, getattr(self.user, "id", None))
        if self.settings.default_guild_id and self.settings.default_auto_post_channel_id:
            self.repo.upsert_guild_settings(
                self.settings.default_guild_id,
                auto_post_channel_id=self.settings.default_auto_post_channel_id,
            )
        if not self._ready_once:
            self._ready_once = True
            if self._on_ready_hook is not None:
                await self._on_ready_hook()

    async def deliver_image(self, channel_id: str, image_bytes: bytes, filename: str) -> str | None:
        """Send a rendered PNG/JPEG stat card to a GameVox text channel."""
        cid = int(channel_id)
        channel = self.get_channel(cid)
        if channel is None:
            channel = await self.fetch_channel(cid)
        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError(f"Channel {channel_id} is not messageable")
        msg = await channel.send(file=discord.File(io.BytesIO(image_bytes), filename=filename))
        return str(msg.id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        content = (message.content or "").strip()
        if not content:
            return

        prefix = self.settings.command_prefix.lower()
        # Strip leading @bot mention if user @mentioned the bot
        if self.user is not None:
            bot_mention_re = re.compile(rf"^<@!?{self.user.id}>\s*")
            content = bot_mention_re.sub("", content).strip()

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

        # Resolve @other_user mentions into target_user_id
        target_user_id = user_id
        mention_match = MENTION_RE.search(raw_args)
        if mention_match:
            target_user_id = mention_match.group(1)
            raw_args = MENTION_RE.sub("", raw_args).strip()

        try:
            await self._dispatch(message, raw_args, user_id, target_user_id, guild_id, lang)
        except WGApiError as exc:
            if "WG_APPLICATION_ID" in str(exc):
                await message.reply(self.i18n.t("bot.wg_key_missing", lang=lang))
            else:
                await message.reply(self.i18n.t("bot.error_generic", lang=lang, error=str(exc)))
        except Exception as exc:
            logger.exception("Command failed for '{}': {}", content, exc)
            await message.reply(self.i18n.t("bot.error_generic", lang=lang, error=str(exc)))

    async def _send_image_reply(
        self, message: discord.Message, image_bytes: bytes, filename: str
    ) -> None:
        await message.reply(file=discord.File(io.BytesIO(image_bytes), filename=filename))

    async def _dispatch(
        self,
        message: discord.Message,
        raw_args: str,
        user_id: str,
        target_user_id: str,
        guild_id: str | None,
        lang: Language,
    ) -> None:
        prefix = self.settings.command_prefix
        tokens = raw_args.split()
        if not tokens or tokens[0].lower() in ("help", "帮助"):
            await message.reply(self.i18n.t("bot.help_text", lang=lang, prefix=prefix))
            return

        cmd = tokens[0].lower()
        rest = tokens[1:]

        # --- Language ---
        if cmd in ("lang", "language", "语言"):
            if not rest or rest[0].lower() not in ("zh", "en"):
                await message.reply(self.i18n.t("bot.lang_invalid", lang=lang))
                return
            new_lang: Language = rest[0].lower()  # type: ignore[assignment]
            updated = self.repo.set_user_language(user_id, new_lang)
            if not updated and guild_id or guild_id and len(rest) > 1 and rest[1].lower() == "server":
                self.repo.upsert_guild_settings(guild_id, default_lang=new_lang)
            await message.reply(self.i18n.t("bot.lang_set", lang=new_lang))
            return

        # --- Set default auto-post channel ---
        if cmd in ("channel", "setchannel", "设置频道"):
            if guild_id:
                self.repo.upsert_guild_settings(guild_id, auto_post_channel_id=str(message.channel.id))
                await message.reply(self.i18n.t("bot.channel_set", lang=lang))
            return

        # --- Bind account ---
        if cmd in ("bind", "绑定"):
            if len(rest) < 2:
                await message.reply(self.i18n.t("bot.bind_missing_args", lang=lang, prefix=prefix))
                return
            realm = normalize_realm(rest[0])
            if realm is None:
                await message.reply(self.i18n.t("bot.bind_missing_args", lang=lang, prefix=prefix))
                return
            nick = " ".join(rest[1:]).strip()
            matches = await self.wg.search_account(realm, nick, limit=5)
            exact = next((m for m in matches if str(m.get("nickname", "")).lower() == nick.lower()), None)
            chosen = exact or (matches[0] if matches else None)
            if not chosen:
                await message.reply(
                    self.i18n.t("bot.bind_not_found", lang=lang, realm=self.i18n.t(f"realms.{realm}", lang=lang), nickname=nick)
                )
                return

            aid = int(chosen["account_id"])
            real_nick = str(chosen["nickname"])
            gs = self.repo.get_guild_settings(guild_id) if guild_id else {}
            auto_ch = gs.get("auto_post_channel_id") or self.settings.default_auto_post_channel_id or str(message.channel.id)

            self.repo.bind_account(
                gamevox_user_id=user_id,
                realm=realm,
                account_id=aid,
                nickname=real_nick,
                guild_id=guild_id,
                auto_post_channel_id=auto_ch if guild_id else None,
            )
            # Trigger immediate baseline seed job in Honker poll queue
            if self.repo.dbm.poll_queue is not None:
                self.repo.dbm.poll_queue.enqueue(
                    {"type": "fetch_player_diff", "realm": realm, "account_id": aid, "nickname": real_nick}
                )
            await message.reply(
                self.i18n.t(
                    "bot.bind_success",
                    lang=lang,
                    realm=self.i18n.t(f"realms.{realm}", lang=lang),
                    nickname=real_nick,
                    account_id=aid,
                )
            )
            return

        # --- Toggle auto-post tracking in current channel ---
        if cmd in ("track", "追踪", "推送"):
            binding = self.repo.get_default_binding(target_user_id)
            if binding is None:
                await message.reply(self.i18n.t("bot.not_bound", lang=lang, prefix=prefix))
                return
            enabled = not (rest and rest[0].lower() in ("off", "0", "false", "关", "关闭"))
            if guild_id:
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
            await message.reply(
                self.i18n.t(
                    key,
                    lang=lang,
                    realm=self.i18n.t(f"realms.{binding.realm}", lang=lang),
                    nickname=binding.nickname,
                )
            )
            return

        # --- Last single battle / division match ---
        if cmd in ("last", "recents", "单局", "单场近期"):
            resolved = await self._resolve_target_account(rest, target_user_id, lang)
            if isinstance(resolved, str):
                await message.reply(resolved)
                return
            realm, aid, nick = resolved
            group = self.repo.get_latest_match_group_for_account(realm, aid)
            if not group:
                await message.reply(self.i18n.t("bot.no_last_match", lang=lang, nickname=nick))
                return
            ships_meta = self.repo.get_ships_map([r["ship_id"] for r in group["records"]])
            img = await self.renderer.render_match_group(group, ships_meta, lang=lang)
            await self._send_image_reply(message, img, f"match_{group['id']}.png")
            return

        # --- Daily recent ---
        if cmd in ("recent", "近期"):
            days = 7
            remaining = list(rest)
            if remaining and remaining[-1].isdigit():
                days = max(1, min(90, int(remaining.pop())))
            resolved = await self._resolve_target_account(remaining, target_user_id, lang)
            if isinstance(resolved, str):
                await message.reply(resolved)
                return
            realm, aid, nick = resolved
            since_date = (datetime.now(tz=UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
            baseline = self.repo.get_daily_snapshot_on_or_before(realm, aid, since_date)
            if baseline is None:
                await message.reply(self.i18n.t("bot.no_recent_data", lang=lang, nickname=nick, days=days))
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
                await message.reply(self.i18n.t("bot.no_recent_data", lang=lang, nickname=nick, days=days))
                return
            img = await self.renderer.render_recent_summary(view, lang=lang)
            await self._send_image_reply(message, img, f"recent_{aid}_{days}d.png")
            return

        # --- Single ship stats ---
        if cmd in ("ship", "单船"):
            if not rest:
                await message.reply(f"Usage: `{prefix} ship <ship_name>`")
                return
            # Check if user passed <realm> <nick> <ship> or just <ship>
            realm_opt = normalize_realm(rest[0]) if len(rest) >= 3 else None
            if realm_opt:
                resolved = await self._resolve_target_account(rest[:2], target_user_id, lang)
                ship_query = " ".join(rest[2:])
            else:
                resolved = await self._resolve_target_account([], target_user_id, lang)
                ship_query = " ".join(rest)
            if isinstance(resolved, str):
                await message.reply(resolved)
                return
            realm, aid, nick = resolved
            ship = await self._resolve_ship(message, ship_query, lang)
            if ship is None:
                return
            entries = await self.wg.get_account_ships_stats(realm, aid, ship_id=ship.ship_id)
            if not entries:
                await message.reply(self.i18n.t("bot.ship_not_found", lang=lang, query=ship.display_name(lang)))
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
            await self._send_image_reply(message, img, f"ship_{aid}_{ship.ship_id}.png")
            return

        # --- Ship leaderboard (Yuyuko public tokenless + local SQLite fallback) ---
        if cmd in ("ship.rank", "rank", "排行榜"):
            if len(rest) < 2:
                await message.reply(f"Usage: `{prefix} ship.rank <na|eu|asia> <ship_name>`")
                return
            realm = normalize_realm(rest[0])
            if realm is None:
                await message.reply(f"Usage: `{prefix} ship.rank <na|eu|asia> <ship_name>`")
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
                await message.reply(self.i18n.t("bot.ship_not_found", lang=lang, query=ship.display_name(lang)))
                return
            img = await self.renderer.render_ship_rank(
                realm=realm, ship=ship, entries=entries, is_global=is_global, lang=lang
            )
            await self._send_image_reply(message, img, f"rank_{realm}_{ship.ship_id}.png")
            return

        # --- Clan info ---
        if cmd in ("clan", "军团"):
            if len(rest) < 2:
                await message.reply(f"Usage: `{prefix} clan <na|eu|asia> <TAG>`")
                return
            realm = normalize_realm(rest[0])
            if realm is None:
                await message.reply(f"Usage: `{prefix} clan <na|eu|asia> <TAG>`")
                return
            tag = rest[1].strip()
            clans = await self.wg.search_clan(realm, tag, limit=5)
            chosen = next((c for c in clans if str(c.get("tag", "")).lower() == tag.lower()), None) or (
                clans[0] if clans else None
            )
            if not chosen:
                await message.reply(
                    self.i18n.t("bot.clan_not_found", lang=lang, realm=self.i18n.t(f"realms.{realm}", lang=lang), tag=tag)
                )
                return
            detail = await self.wg.get_clan_info(realm, int(chosen["clan_id"]))
            if not detail:
                await message.reply(
                    self.i18n.t("bot.clan_not_found", lang=lang, realm=self.i18n.t(f"realms.{realm}", lang=lang), tag=tag)
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
            await self._send_image_reply(message, img, f"clan_{realm}_{view['tag']}.png")
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
                await message.reply(self.i18n.t("bot.roll_empty", lang=lang))
                return
            pick = random.choice(ships)
            await message.reply(
                self.i18n.t(
                    "bot.roll_result",
                    lang=lang,
                    tier=pick.tier,
                    ship_name=pick.display_name(lang),
                    ship_type=self.i18n.t(f"ship_types.{pick.ship_type}", lang=lang),
                    nation=pick.nation,
                )
            )
            return

        # --- Default: Player overview (`wws me` or `wws <realm> <nickname>`) ---
        query_tokens = [] if cmd == "me" else tokens
        resolved = await self._resolve_target_account(query_tokens, target_user_id, lang)
        if isinstance(resolved, str):
            await message.reply(resolved)
            return
        realm, aid, nick = resolved
        batch = await self.wg.get_accounts_info_batch(realm, [aid])
        acc_info = batch.get(aid)
        if not acc_info:
            await message.reply(
                self.i18n.t("bot.bind_not_found", lang=lang, realm=self.i18n.t(f"realms.{realm}", lang=lang), nickname=nick)
            )
            return
        if acc_info.get("hidden_profile"):
            await message.reply(self.i18n.t("bot.hidden_profile", lang=lang, nickname=nick))
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
        await self._send_image_reply(message, img, f"overview_{realm}_{aid}.png")

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
                nickname=nick,
            )
        return realm, int(chosen["account_id"]), str(chosen["nickname"])

    async def _resolve_ship(
        self, message: discord.Message, query: str, lang: Language
    ) -> ShipMeta | None:
        matches = self.repo.search_ships(query, limit=6)
        if not matches:
            await message.reply(self.i18n.t("bot.ship_not_found", lang=lang, query=query))
            return None
        q_low = query.strip().lower()
        for m in matches:
            if m.name_en.lower() == q_low or m.name_zh.lower() == q_low:
                return m
        if len(matches) == 1:
            return matches[0]
        choices = "\n".join(f"• T{m.tier} {m.name_zh} ({m.name_en})" for m in matches)
        await message.reply(self.i18n.t("bot.ship_multiple", lang=lang, choices=choices))
        return None
