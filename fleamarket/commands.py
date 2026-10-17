"""スラッシュコマンド。"""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import ui
from .actions import ListingModal, db_of, reply, send_profile


class FleaMarket(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="フリマ設置", description="このチャンネルをフリマにして、出品ボタン付きの案内を設置します")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setup_market(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and interaction.channel is not None
        db_of(interaction).set_market_channel(interaction.guild.id, interaction.channel.id)
        view = ui.make_view(
            ui.action_button("sell", 0, "出品する", emoji="📦", style=discord.ButtonStyle.success),
        )
        await interaction.channel.send(embed=ui.panel_embed(), view=view)  # type: ignore[union-attr]
        await reply(interaction, "✅ このチャンネルをフリマに設定しました。出品ページはここに投稿されます。")

    @app_commands.command(name="出品", description="フォームに入力して商品を出品します")
    @app_commands.guild_only()
    async def sell(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ListingModal())

    @app_commands.command(name="評価", description="メンバーの取引評価を確認します")
    @app_commands.rename(member="メンバー")
    @app_commands.describe(member="評価を見たいメンバー（省略すると自分）")
    async def rating(self, interaction: discord.Interaction, member: Optional[discord.User] = None) -> None:
        await send_profile(interaction, (member or interaction.user).id)

    @app_commands.command(name="マイ取引", description="自分の出品中の商品と進行中の取引を確認します")
    async def my_trades(self, interaction: discord.Interaction) -> None:
        db = db_of(interaction)
        user_id = interaction.user.id
        embed = discord.Embed(title="📋 マイ取引", color=ui.MARKET_COLOR)

        trades = db.active_trades_for_user(user_id)
        if trades:
            lines = []
            for t in trades[:15]:
                role = "販売" if t["seller_id"] == user_id else "購入"
                partner = t["buyer_id"] if role == "販売" else t["seller_id"]
                lines.append(
                    f"`#{t['id']}` 【{role}】{t['title']} — {ui.yen(t['price'])}\n"
                    f"└ {ui.TRADE_STATUS[t['status']]}・相手 <@{partner}>"
                )
            embed.add_field(name="🔄 進行中の取引", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="🔄 進行中の取引", value="ありません", inline=False)

        listings = db.listings_by_seller(user_id, ("open",))
        if listings:
            lines = []
            for row in listings[:15]:
                link = (
                    f"https://discord.com/channels/{row['guild_id']}/{row['channel_id']}/{row['message_id']}"
                    if row["message_id"]
                    else ""
                )
                lines.append(f"`#{row['id']}` [{row['title']}]({link}) — {ui.yen(row['price'])}")
            embed.add_field(name="🟢 出品中", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="🟢 出品中", value="ありません", inline=False)

        embed.set_footer(text="取引の操作はDMに届いたメッセージのボタンから行えます")
        await reply(interaction, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FleaMarket(bot))
