# wows-gamevox-bot

Self-hosted World of Warships stats & auto-posting bot for **GameVox** (Discord API v10 compatible), targeting **NA / EU / ASIA**.

Built with:
- **Python 3.12 + `uv`**
- **`discord.py`** pointed at GameVox REST (`https://app-api.gamevox.com/api/v10`) & Gateway (`wss://gateway.gamevox.com`)
- **SQLite (WAL mode)** + **[`russellromney/honker`](https://github.com/russellromney/honker)** for atomic transactional outbox queues & delayed jobs (zero Redis/RabbitMQ)
- **Official Wargaming Developer API** (zero dependency on Yuyuko's closed backend; optional tokenless `ship.rank` lookup from Yuyuko's public endpoint with local SQLite fallback)
- **Playwright Chromium** + owned Chinese/English HTML/CSS stat cards (`zh` default, `en` supported)
- **Division/Squad Auto-Post Deduplication** — when 2–3 tracked players finish the same `pvp_div2` or `pvp_div3` match, their results are coalesced into **one combined Division Match Card** and posted once per channel.

---

## Commands

| Command | Description |
|---|---|
| `wws bind <na\|eu\|asia> <nickname>` | Bind WoWS account & enable tracking |
| `wws me` / `wws <realm> <nickname>` | Render player overview card |
| `wws ship <ship_name>` | Render single-ship stats card |
| `wws recent [days]` | Render local recent performance card (default 7d) |
| `wws last` / `wws recents` | Render latest single-game or combined division match card |
| `wws track on\|off` | Enable/disable auto battle posting in the current channel |
| `wws channel` | Set current channel as default battle report channel |
| `wws ship.rank <realm> <ship_name>` | Ship leaderboard (Yuyuko public global + local fallback) |
| `wws clan <realm> <TAG>` | Render clan overview card |
| `wws roll [tier] [dd\|ca\|bb\|cv\|ss]` | Random ship picker |
| `wws lang <zh\|en>` | Switch language (`zh` default) |
| `wws help` | Show command help |

---

## 1Panel Deployment (Oracle Cloud ARM / x86_64)

1. In **1Panel → Containers → Compose → Create Compose**, paste [`docker-compose.yml`](./docker-compose.yml).
2. Create a `.env` file in that Compose directory (copy from [`.env.example`](./.env.example)) and set:
   ```env
   GAMEVOX_BOT_TOKEN=<your_gamevox_bot_token>
   WG_APPLICATION_ID=<your_wg_developer_application_id>
   DEFAULT_GUILD_ID=1487625003808419840
   DEFAULT_AUTO_POST_CHANNEL_ID=1574077276956221440
   DEFAULT_LANGUAGE=zh
   ```
3. Start the Compose stack in 1Panel.

### Automated CI/CD (Native Dual-Platform GHCR + SSH Deploy)

Pushing to `main` triggers [`.github/workflows/build-and-deploy.yml`](./.github/workflows/build-and-deploy.yml):
1. Runs `ruff` + `pytest`.
2. Builds **`linux/amd64`** (`ubuntu-24.04`) and **`linux/arm64`** (`ubuntu-24.04-arm`) **natively in parallel** (no QEMU!) and merges them into `ghcr.io/ec061/yuyuko-discord:latest`.
3. SSHes into your ARM VPS and runs `docker compose pull && docker compose up -d --remove-orphans` in your 1Panel Compose directory.

Configure these GitHub repository secrets to enable automatic SSH deploy:
- `VPS_HOST` — Oracle Cloud ARM VPS IP/hostname
- `VPS_USER` — SSH username
- `VPS_PORT` — SSH port (default `22`)
- `VPS_SSH_KEY` — Private SSH key
- `VPS_COMPOSE_DIR` — Path to the 1Panel Compose folder (e.g., `/opt/1panel/docker/compose/wows-gamevox-bot`)

---

## Upgradeability & Upstream Monitoring

- **Game updates (new ships / PR values):** Auto-synced every 6 hours via official WG Encyclopedia API and `wows-numbers.com` (zero deploys needed).
- **Dependencies (`honker`, `discord.py`, `playwright`, Docker base):** Automated weekly PRs via [`.github/dependabot.yml`](./.github/dependabot.yml).
- **Upstream Watcher:** [`.github/workflows/upstream-watch.yml`](./.github/workflows/upstream-watch.yml) runs daily to watch `wows-yuyuko` for new repos/API changes, verify the `/public/rank/yuyuko_ship/na` & `wows-numbers.com` contracts, and watch `russellromney/honker` releases — opening deduplicated `upstream-watch` GitHub Issues when anything changes.
