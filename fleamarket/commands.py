"""スラッシュコマンド。"""

from __future__ import annotations

from typing import Any, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from . import ui
from .actions import ListingModal, db_of, reply, send_profile


class FleaMarket(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="フリマ設置", description="フリマのチャンネル（フォーラム推奨）を設定し、出品ボタン付きの案内を設置します")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.rename(channel="チャンネル")
    @app_commands.describe(channel="出品先のフォーラム or テキストチャンネル（省略すると今いるチャンネル）")
    async def setup_market(
        self,
        interaction: discord.Interaction,
        channel: Optional[Union[discord.ForumChannel, discord.TextChannel]] = None,
    ) -> None:
        assert interaction.guild is not None
        target: Any = channel or interaction.channel
        if isinstance(target, discord.Thread) and isinstance(target.parent, discord.ForumChannel):
            target = target.parent  # フォーラムの投稿内で実行されたらそのフォーラムを使う
        if not isinstance(target, (discord.ForumChannel, discord.TextChannel)):
            return await reply(interaction, "⚠️ フォーラムかテキストチャンネルを指定してください。")

        await interaction.response.defer(ephemeral=True, thinking=True)
        db_of(interaction).set_market_channel(interaction.guild.id, target.id)
        sell_view = ui.make_view(
            ui.action_button("sell", 0, "出品する", emoji="📦", style=discord.ButtonStyle.success),
        )

        if isinstance(target, discord.TextChannel):
            try:
                await target.send(embed=ui.panel_embed(), view=sell_view)
            except discord.HTTPException:
                await interaction.followup.send("⚠️ 案内を投稿できませんでした。Botの権限を確認してください。", ephemeral=True)
                return
            await interaction.followup.send(f"✅ {target.mention} をフリマに設定しました。出品ページはここに投稿されます。", ephemeral=True)
            return

        notes: list[str] = []
        missing = [
            discord.ForumTag(name=name, emoji=emoji)
            for name, emoji in ui.STATUS_TAGS.values()
            if discord.utils.get(target.available_tags, name=name) is None
        ]
        if missing:
            try:
                target = await target.edit(available_tags=[*target.available_tags, *missing])
            except discord.HTTPException:
                notes.append("・ステータスタグ（出品中／取引中／SOLD／取り下げ）を作成できませんでした。Botに「チャンネルの管理」権限を付けて再実行するか、手動で同名のタグを作ってください。")

        try:
            created = await target.create_thread(
                name="📌 出品はこちら・使い方",
                embed=ui.panel_embed(),
                view=sell_view,
            )
            try:
                await created.thread.edit(pinned=True)
            except discord.HTTPException:
                notes.append("・案内の投稿をピン留めできませんでした（Botに「スレッドの管理」権限が必要です）。")
            panel = created.thread.mention
        except discord.HTTPException:
            panel = None
            notes.append("・案内の投稿を作成できませんでした。`/出品` コマンドからは出品できます。")

        text = f"✅ {target.mention} をフリマに設定しました。出品すると1商品ずつ新しい投稿が作られます。"
        if panel:
            text += f"\n案内: {panel}"
        if notes:
            text += "\n\n" + "\n".join(notes)
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="出品", description="フォームに入力して商品を出品します")
    @app_commands.guild_only()
    async def sell(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ListingModal())

    @app_commands.command(name="評価", description="メンバーの取引評価を確認します")
    @app_commands.rename(member="メンバー")
    @app_commands.describe(member="評価を見たいメンバー（省略すると自分）")
    async def rating(self, interaction: discord.Interaction, member: Optional[discord.User] = None) -> None:
        # サーバー内ならそのサーバーでの評価、DM なら全体の評価を表示する
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

    @app_commands.command(name="フリマ招待", description="このフリマBotを自分のサーバーに入れるための招待リンクを表示します")
    async def invite(self, interaction: discord.Interaction) -> None:
        assert self.bot.user is not None
        view = ui.make_view(discord.ui.Button(label="サーバーに追加する", emoji="➕", url=ui.invite_url(self.bot.user.id)))
        await reply(interaction, "下のボタンから、あなたのサーバーにフリマBotを追加できます。", view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FleaMarket(bot))
