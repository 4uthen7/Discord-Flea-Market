"""Discord フリマBot のエントリーポイント。"""

from __future__ import annotations

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

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
        # 再起動後も過去に送ったボタンが動くように登録
        self.add_dynamic_items(ActionButton)
        await self.load_extension("fleamarket.commands")

        if self.sync_guild_id:
            # 開発用：特定サーバーにだけ即時反映
            guild = discord.Object(id=self.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("環境変数 DISCORD_TOKEN が設定されていません（.env.example を参照）")
    guild_id = os.getenv("GUILD_ID")
    db = Database(os.getenv("DATABASE_PATH", "fleamarket.db"))
    bot = FleaMarketBot(db, int(guild_id) if guild_id else None)
    bot.run(token, root_logger=True)


if __name__ == "__main__":
    main()
