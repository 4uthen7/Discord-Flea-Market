"""Discord フリマBot のエントリーポイント。"""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

from fleamarket import ui
from fleamarket.actions import ActionButton
from fleamarket.db import Database

log = logging.getLogger("fleamarket")


class FleaMarketBot(commands.Bot):
    def __init__(self, db: Database, guild_id: int | None) -> None:
        # スラッシュコマンドとボタンだけで動くので特権インテントは不要
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.db = db
        self.sync_guild_id = guild_id

    async def setup_hook(self) -> None:
        # Render ではポートを早めに開けておかないとデプロイ失敗扱いになる
        port = os.getenv("PORT")
        if port:
            await self.start_health_server(int(port))
        # 再起動後も過去に送ったボタンが動くように登録
        self.add_dynamic_items(ActionButton)
        await self.load_extension("fleamarket.commands")

        if self.sync_guild_id:
            # 開発用：特定サーバーにだけ即時反映（他のサーバーにはコマンドが出ないので、公開時は GUILD_ID を消す）
            guild = discord.Object(id=self.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

        external_url = os.getenv("RENDER_EXTERNAL_URL")
        if external_url:
            self.loop.create_task(self.keep_awake(external_url))

    async def start_health_server(self, port: int) -> None:
        """Render の Web Service はポートを開いていないとデプロイに失敗するため、生存確認用の HTTP サーバーを立てる。"""

        async def health(_: web.Request) -> web.Response:
            return web.Response(text="ok" if self.is_ready() else "starting")

        app = web.Application()
        app.router.add_get("/", health)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        log.info("Health server listening on :%d", port)

    async def keep_awake(self, url: str) -> None:
        """Render 無料プランは15分アクセスがないとスリープするので、自分の公開URLに定期的にアクセスする。"""
        await self.wait_until_ready()
        async with aiohttp.ClientSession() as session:
            while not self.is_closed():
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        log.debug("Keep-awake ping: %s", resp.status)
                except Exception:
                    log.warning("Keep-awake ping failed", exc_info=True)
                await asyncio.sleep(10 * 60)

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (%s) in %d servers", self.user, self.user.id, len(self.guilds))
        log.info("Invite URL: %s", ui.invite_url(self.user.id))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """新しいサーバーに招待されたら、使い方を案内する。"""
        log.info("Joined guild %s (%s)", guild.name, guild.id)
        candidates = [guild.system_channel, *guild.text_channels]
        for channel in candidates:
            if channel is not None and channel.permissions_for(guild.me).send_messages:
                try:
                    await channel.send(embed=ui.welcome_embed())
                except discord.HTTPException:
                    continue
                return


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("環境変数 DISCORD_TOKEN が設定されていません（.env.example を参照）")
    guild_id = os.getenv("GUILD_ID")
    db = Database(os.getenv("DATABASE_URL") or os.getenv("DATABASE_PATH", "fleamarket.db"))
    bot = FleaMarketBot(db, int(guild_id) if guild_id else None)
    bot.run(token, root_logger=True)


if __name__ == "__main__":
    main()
