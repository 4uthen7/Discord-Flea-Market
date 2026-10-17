"""ボタン・フォーム（モーダル）の処理と、取引フロー本体。

すべてのボタンは `fm:<action>:<id>` という custom_id を持ち、
`ActionButton`（DynamicItem）が受け取って HANDLERS に振り分ける。
custom_id に ID を埋め込んでいるので、Bot を再起動しても古いボタンが動き続ける。
"""

from __future__ import annotations

import logging
import os
import unicodedata
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import urlparse

import discord

from . import ui
from .ui import action_button, make_view, yen

if TYPE_CHECKING:
    from .db import Database

log = logging.getLogger(__name__)

ERROR_TEXT = "⚠️ エラーが発生しました。時間をおいてもう一度お試しください。"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_PRICE = 10_000_000

Handler = Callable[[discord.Interaction, int], Awaitable[Any]]


# ======================================================================
# 共通ヘルパー
# ======================================================================
def db_of(interaction: discord.Interaction) -> Database:
    return interaction.client.db  # type: ignore[attr-defined]


async def reply(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    *,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
) -> None:
    """本人だけに見えるメッセージで返信する（応答済みなら followup）。"""
    kwargs: dict[str, Any] = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


def parse_price(text: str) -> Optional[int]:
    normalized = unicodedata.normalize("NFKC", text)
    for ch in (",", "¥", "円", " "):
        normalized = normalized.replace(ch, "")
    if not normalized.isdecimal():
        return None
    value = int(normalized)
    return value if 1 <= value <= MAX_PRICE else None


def is_valid_url(text: str) -> bool:
    parsed = urlparse(text)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and " " not in text and len(text) <= 512


async def notify(
    client: discord.Client,
    user_id: int,
    listing: Mapping[str, Any],
    *,
    embed: discord.Embed,
    view: Optional[discord.ui.View] = None,
) -> bool:
    """DMで通知する。DMを拒否している人には出品チャンネルでメンションして知らせる。"""
    kwargs: dict[str, Any] = {"embed": embed}
    if view is not None:
        kwargs["view"] = view
    try:
        user = client.get_user(user_id) or await client.fetch_user(user_id)
        await user.send(**kwargs)
        return True
    except discord.HTTPException:
        log.info("DM to %s failed; falling back to listing channel", user_id)

    if listing.get("channel_id"):
        try:
            await client.get_partial_messageable(listing["channel_id"]).send(
                content=f"<@{user_id}> DMを受け取れない設定のため、こちらでお知らせします。",
                allowed_mentions=discord.AllowedMentions(users=True),
                **kwargs,
            )
        except discord.HTTPException:
            log.warning("Could not notify user %s about listing %s", user_id, listing["id"])
    return False


async def _forum_thread(client: discord.Client, channel_id: int) -> Optional[discord.Thread]:
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
        return channel
    return None


async def sync_forum_thread(thread: discord.Thread, listing: Mapping[str, Any]) -> None:
    """フォーラム投稿のタイトル・ステータスタグを更新し、終了した出品は閉じる。"""
    forum = thread.parent
    assert isinstance(forum, discord.ForumChannel)
    tags = [t for t in thread.applied_tags if t.name not in ui.STATUS_TAG_NAMES]
    if tag := ui.status_tag(forum, listing["status"]):
        tags.append(tag)
    kwargs: dict[str, Any] = {"applied_tags": tags[:5]}
    name = ui.thread_name(listing)
    if thread.name != name:  # 名前の変更はレート制限が厳しいので変わったときだけ
        kwargs["name"] = name
    if listing["status"] in ("sold", "withdrawn"):
        kwargs.update(archived=True, locked=True)
    try:
        await thread.edit(**kwargs)
    except discord.Forbidden:
        # 「スレッドの管理」権限がないとロックできないので、ロックなしでもう一度
        kwargs.pop("locked", None)
        try:
            await thread.edit(**kwargs)
        except discord.HTTPException:
            log.warning("Could not update forum thread %s", thread.id)
    except discord.HTTPException:
        log.warning("Could not update forum thread %s", thread.id)


async def refresh_listing(client: discord.Client, listing_id: int) -> None:
    """出品ページのメッセージ（フォーラムなら投稿のタイトル・タグも）を最新の状態に書き換える。"""
    db: Database = client.db  # type: ignore[attr-defined]
    listing = db.get_listing(listing_id)
    if listing is None or not listing["message_id"]:
        return
    thread = await _forum_thread(client, listing["channel_id"])
    message = client.get_partial_messageable(listing["channel_id"]).get_partial_message(listing["message_id"])
    kwargs: dict[str, Any] = {
        "embeds": ui.listing_embeds(listing, db.rating_summary(listing["seller_id"])),
        "view": ui.listing_view(listing),
    }
    if thread is not None:
        kwargs["content"] = ui.forum_summary(listing)
        kwargs["allowed_mentions"] = discord.AllowedMentions.none()
        if thread.archived and not thread.locked:
            # しばらく発言がなく自動アーカイブされた投稿は、いったん開き直さないと編集できない
            try:
                await thread.edit(archived=False)
            except discord.HTTPException:
                log.warning("Could not unarchive forum thread %s", thread.id)
    try:
        # アーカイブ後は編集できないので、メッセージ → スレッドの順に更新する
        await message.edit(**kwargs)
    except discord.HTTPException:
        log.warning("Could not refresh listing message #%s", listing_id)
    if thread is not None:
        await sync_forum_thread(thread, listing)


class ConfirmView(discord.ui.View):
    """「本当に〜しますか？」用の一時的な確認ボタン。"""

    def __init__(
        self,
        user_id: int,
        label: str,
        on_confirm: Callable[[discord.Interaction], Awaitable[Any]],
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.success,
    ) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.on_confirm = on_confirm
        confirm = discord.ui.Button(label=label, style=style)
        confirm.callback = self._confirm
        cancel = discord.ui.Button(label="やめる", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        try:
            await self.on_confirm(interaction)
        except Exception:
            log.exception("Confirm callback failed")
            await reply(interaction, ERROR_TEXT)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="操作をやめました。", embed=None, view=None)


class BaseModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal %s failed", type(self).__name__, exc_info=error)
        await reply(interaction, ERROR_TEXT)


def buyable_error(listing: Optional[Mapping[str, Any]], user_id: int) -> Optional[str]:
    if listing is None:
        return "この出品は見つかりませんでした。"
    if listing["seller_id"] == user_id:
        return "自分の出品は購入・交渉できません。"
    if listing["status"] == "trading":
        return "この商品は現在ほかの方と取引中です。"
    if listing["status"] != "open":
        return "この出品は終了しています。"
    return None


async def send_profile(interaction: discord.Interaction, user_id: int) -> None:
    client = interaction.client
    user: Optional[discord.abc.User] = interaction.guild.get_member(user_id) if interaction.guild else None
    if user is None:
        user = client.get_user(user_id) or await client.fetch_user(user_id)
    db = db_of(interaction)
    await reply(interaction, embed=ui.profile_embed(user, db.rating_summary(user_id), db.recent_ratings(user_id)))


# ======================================================================
# 出品
# ======================================================================
class ListingModal(BaseModal):
    """出品フォーム。listing を渡すと編集フォームになる。"""

    def __init__(self, listing: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(title="出品内容を編集" if listing else "📦 商品を出品する", timeout=900)
        self.listing = listing
        self.name = discord.ui.TextInput(
            placeholder="例）Nintendo Switch 有機ELモデル ホワイト",
            max_length=80,
            default=listing["title"] if listing else None,
        )
        self.price = discord.ui.TextInput(
            placeholder="例）25000",
            max_length=12,
            default=str(listing["price"]) if listing else None,
        )
        self.condition = discord.ui.Select(
            placeholder="商品の状態を選んでください",
            options=[
                discord.SelectOption(label=c, value=c, default=bool(listing and listing["condition"] == c))
                for c in ui.CONDITIONS
            ],
        )
        self.description = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1500,
            placeholder="商品の説明・付属品・発送方法（例：アズカリ／送料込み）・発送までの日数など",
            default=listing["description"] if listing else None,
        )
        self.add_item(discord.ui.Label(text="商品名", component=self.name))
        self.add_item(discord.ui.Label(text="価格（円）", description="数字のみ（全角OK）", component=self.price))
        self.add_item(discord.ui.Label(text="商品の状態", component=self.condition))
        self.add_item(
            discord.ui.Label(
                text="商品の説明", description="発送方法や付属品も書いておくと取引がスムーズです", component=self.description
            )
        )
        self.images: Optional[discord.ui.FileUpload] = None
        if listing is None:
            self.images = discord.ui.FileUpload(required=False, max_values=4)
            self.add_item(discord.ui.Label(text="商品画像（任意・最大4枚）", component=self.images))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        price = parse_price(self.price.value)
        if price is None:
            return await reply(interaction, f"⚠️ 価格は 1〜{MAX_PRICE:,} の数字で入力してください。")
        title = self.name.value.strip()
        condition = self.condition.values[0]
        description = self.description.value.strip()
        if self.listing is None:
            await self._create(interaction, title, price, condition, description)
        else:
            await self._edit(interaction, title, price, condition, description)

    async def _create(self, interaction: discord.Interaction, title: str, price: int, condition: str, description: str) -> None:
        guild = interaction.guild
        if guild is None:
            return await reply(interaction, "出品はサーバー内で行ってください。")
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = db_of(interaction)

        channel: Any = None
        market_channel_id = db.get_market_channel(guild.id)
        if market_channel_id:
            channel = guild.get_channel_or_thread(market_channel_id)
        if channel is None:
            channel = interaction.channel
            # フォーラムの投稿内で /出品 した場合は、そのフォーラムに新しい投稿を作る
            if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
                channel = channel.parent

        files: list[discord.File] = []
        names: list[str] = []
        for index, attachment in enumerate(self.images.values if self.images else [], start=1):
            ext = os.path.splitext(attachment.filename)[1].lower()
            if ext not in IMAGE_EXTS:
                if not (attachment.content_type or "").startswith("image/"):
                    continue
                ext = ".png"
            name = f"item{index}{ext}"
            try:
                files.append(await attachment.to_file(filename=name))
                names.append(name)
            except discord.HTTPException:
                log.warning("Failed to download attachment %s", attachment.url)

        listing_id = db.create_listing(
            guild_id=guild.id,
            seller_id=interaction.user.id,
            title=title,
            price=price,
            condition=condition,
            description=description,
            images=names,
        )
        listing = db.get_listing(listing_id)
        assert listing is not None
        embeds = ui.listing_embeds(listing, db.rating_summary(interaction.user.id))
        try:
            if isinstance(channel, discord.ForumChannel):
                # フォーラムなら 1商品 = 1投稿 にする
                tag = ui.status_tag(channel, "open")
                created = await channel.create_thread(
                    name=ui.thread_name(listing),
                    auto_archive_duration=10080,
                    content=ui.forum_summary(listing),
                    embeds=embeds,
                    files=files,
                    view=ui.listing_view(listing),
                    applied_tags=[tag] if tag else [],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                message = created.message
                try:
                    # 投稿への質問が出品者に通知されるよう、出品者を投稿に参加させる
                    await created.thread.add_user(interaction.user)
                except discord.HTTPException:
                    pass
            else:
                message = await channel.send(embeds=embeds, files=files, view=ui.listing_view(listing))
        except (discord.HTTPException, AttributeError):
            db.delete_listing(listing_id)
            await interaction.followup.send(
                "⚠️ 出品ページを投稿できませんでした。Botがフリマチャンネルに投稿できるか管理者に確認してください。",
                ephemeral=True,
            )
            return
        db.set_listing_message(listing_id, message.channel.id, message.id)
        await interaction.followup.send(f"✅ 出品しました！\n{message.jump_url}", ephemeral=True)

    async def _edit(self, interaction: discord.Interaction, title: str, price: int, condition: str, description: str) -> None:
        assert self.listing is not None
        if interaction.user.id != self.listing["seller_id"]:
            return await reply(interaction, "出品者のみ編集できます。")
        if not db_of(interaction).update_listing(
            self.listing["id"], title=title, price=price, condition=condition, description=description
        ):
            return await reply(interaction, "⚠️ 取引中・終了済みの出品は編集できません。")
        await reply(interaction, "✅ 出品内容を更新しました。")
        await refresh_listing(interaction.client, self.listing["id"])


async def handle_sell(interaction: discord.Interaction, _: int) -> None:
    if interaction.guild is None:
        return await reply(interaction, "出品はサーバー内で行ってください。")
    await interaction.response.send_modal(ListingModal())


async def handle_manage(interaction: discord.Interaction, listing_id: int) -> None:
    db = db_of(interaction)
    listing = db.get_listing(listing_id)
    if listing is None:
        return await reply(interaction, "この出品は見つかりませんでした。")
    user = interaction.user
    is_seller = user.id == listing["seller_id"]
    is_admin = isinstance(user, discord.Member) and user.guild_permissions.manage_guild
    if not is_seller and not is_admin:
        return await reply(interaction, "このメニューは出品者だけが使えます。")

    is_open = listing["status"] == "open"
    view = discord.ui.View(timeout=300)
    edit_button = discord.ui.Button(label="編集", emoji="✏️", style=discord.ButtonStyle.primary, disabled=not (is_open and is_seller))
    withdraw_button = discord.ui.Button(label="出品を取り下げる", emoji="🗑️", style=discord.ButtonStyle.danger, disabled=not is_open)

    async def on_edit(i: discord.Interaction) -> None:
        latest = db.get_listing(listing_id)
        if latest is None or latest["status"] != "open":
            return await i.response.edit_message(content="⚠️ 取引中・終了済みの出品は編集できません。", view=None)
        await i.response.send_modal(ListingModal(latest))

    async def on_withdraw(i: discord.Interaction) -> None:
        if not db.withdraw_listing(listing_id):
            return await i.response.edit_message(content="⚠️ 取引中・終了済みの出品は取り下げできません。", view=None)
        await i.response.edit_message(content="🗑️ 出品を取り下げました。", view=None)
        await refresh_listing(i.client, listing_id)

    edit_button.callback = on_edit
    withdraw_button.callback = on_withdraw
    view.add_item(edit_button)
    view.add_item(withdraw_button)

    notes = {
        "open": "出品内容の編集・取り下げができます。",
        "trading": "現在取引中のため、編集・取り下げはできません。\n取引のキャンセルはDMに届いた取引メッセージから行えます。",
        "sold": "この出品は取引が完了しています。",
        "withdrawn": "この出品は取り下げ済みです。",
    }
    await reply(interaction, f"⚙️ **{listing['title']}**\n{notes[listing['status']]}", view=view)


async def handle_profile(interaction: discord.Interaction, user_id: int) -> None:
    await send_profile(interaction, user_id)


# ======================================================================
# 購入 → 取引ページ送付 → 受け取り → 評価
# ======================================================================
def seller_trade_view(trade_id: int) -> discord.ui.View:
    return make_view(
        action_button("tradeurl", trade_id, "取引ページURLを送る", emoji="📄", style=discord.ButtonStyle.primary),
        action_button("tradecancel", trade_id, "取引をキャンセル", emoji="✖️", style=discord.ButtonStyle.danger),
    )


def rate_view(trade_id: int) -> discord.ui.View:
    return make_view(action_button("rate", trade_id, "取引相手を評価する", emoji="⭐", style=discord.ButtonStyle.primary))


async def start_trade(
    interaction: discord.Interaction,
    listing_id: int,
    buyer_id: int,
    price: int,
    *,
    offer_id: Optional[int] = None,
) -> Optional[int]:
    """取引を開始し、出品者・購入者の両方にDMを送る。売り切れなどで開始できなければ None。"""
    db = db_of(interaction)
    client = interaction.client
    trade_id = db.start_trade(listing_id, buyer_id, price, offer_id)
    if trade_id is None:
        return None
    await refresh_listing(client, listing_id)
    listing = db.get_listing(listing_id)
    trade = db.get_trade(trade_id)
    assert listing is not None and trade is not None

    via_offer = offer_id is not None
    seller_embed = ui.trade_embed(
        "🤝 値下げ交渉が成立しました" if via_offer else "🛒 購入リクエストが届きました",
        f"<@{buyer_id}> さんが **{yen(price)}** で購入を希望しています。\n\n"
        "**次にやること**\n"
        "１. アズカリで取引ページを作成する\n"
        "２. 下の「📄 取引ページURLを送る」を押してURLを貼り付ける\n"
        "→ 購入者にDMで自動的に届きます",
        listing,
        trade,
    )
    await notify(client, trade["seller_id"], listing, embed=seller_embed, view=seller_trade_view(trade_id))

    buyer_embed = ui.trade_embed(
        "🤝 値下げ交渉が成立しました" if via_offer else "🛒 購入リクエストを送信しました",
        "出品者がアズカリで取引ページを作成すると、このDMにURLが届きます。\nしばらくお待ちください。",
        listing,
        trade,
    )
    await notify(
        client,
        buyer_id,
        listing,
        embed=buyer_embed,
        view=make_view(
            action_button("tradecancel", trade_id, "取引をキャンセル", emoji="✖️", style=discord.ButtonStyle.danger)
        ),
    )
    return trade_id


async def handle_buy(interaction: discord.Interaction, listing_id: int) -> None:
    db = db_of(interaction)
    listing = db.get_listing(listing_id)
    if error := buyable_error(listing, interaction.user.id):
        return await reply(interaction, error)
    assert listing is not None
    shown_price = listing["price"]

    async def on_confirm(i: discord.Interaction) -> None:
        latest = db.get_listing(listing_id)
        if (error := buyable_error(latest, i.user.id)) is None and latest["price"] != shown_price:  # type: ignore[index]
            error = "⚠️ 価格が変更されました。もう一度出品ページから購入してください。"
        if error:
            return await i.response.edit_message(content=error, embed=None, view=None)
        await i.response.edit_message(content="⏳ 処理中です…", embed=None, view=None)
        trade_id = await start_trade(i, listing_id, i.user.id, shown_price)
        if trade_id is None:
            await i.edit_original_response(content="⚠️ タッチの差でほかの方が購入したか、出品が終了しました。")
        else:
            await i.edit_original_response(
                content="✅ 購入リクエストを送りました！\n出品者が取引ページ（アズカリ）を作成すると、DMでお知らせします。"
            )

    embed = discord.Embed(
        title="🛒 購入の確認",
        description=(
            f"**{listing['title']}** を **{yen(shown_price)}** で購入しますか？\n\n"
            "確定すると出品者にDMで通知され、商品は「取引中」になります。"
        ),
        color=ui.MARKET_COLOR,
    )
    await reply(interaction, embed=embed, view=ConfirmView(interaction.user.id, "購入を確定する", on_confirm))


class TradeUrlModal(BaseModal, title="📄 取引ページを送る"):
    url = discord.ui.TextInput(label="アズカリの取引ページURL", placeholder="https://...", max_length=512)
    message = discord.ui.TextInput(
        label="購入者へのメッセージ（任意）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="例）ご購入ありがとうございます！明日発送予定です。",
    )

    def __init__(self, trade_id: int) -> None:
        super().__init__(timeout=900)
        self.trade_id = trade_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = self.url.value.strip()
        if not is_valid_url(url):
            return await reply(interaction, "⚠️ URLは https:// から始まる形で入力してください。")
        db = db_of(interaction)
        trade = db.get_trade(self.trade_id)
        if trade is None or trade["seller_id"] != interaction.user.id:
            return await reply(interaction, "この操作は出品者のみ可能です。")
        resend = trade["status"] == "page_sent"
        if not db.set_trade_url(self.trade_id, url):
            return await reply(interaction, "⚠️ この取引はすでに終了しています。")

        await reply(interaction, "✅ 購入者に取引ページを送りました。発送の準備をお願いします！")
        listing = db.get_listing(trade["listing_id"])
        assert listing is not None
        embed = ui.trade_embed(
            "📄 取引ページが届きました" + ("（再送）" if resend else ""),
            "出品者がアズカリで取引ページを作成しました。\n"
            "「🔗 取引ページを開く」から手続きを進めてください。\n\n"
            "商品が届いたら **「📦 受け取りました」** を押してください。\n"
            "出品ページが掲載終了になり、お互いに評価できるようになります。",
            listing,
            db.get_trade(self.trade_id),  # type: ignore[arg-type]
            color=discord.Color.blurple(),
        )
        if self.message.value.strip():
            embed.add_field(name="💌 出品者からのメッセージ", value=self.message.value.strip(), inline=False)
        view = make_view(
            discord.ui.Button(label="取引ページを開く", emoji="🔗", url=url),
            action_button("recv", self.trade_id, "受け取りました", emoji="📦", style=discord.ButtonStyle.success),
            action_button("tradecancel", self.trade_id, "取引をキャンセル", emoji="✖️", style=discord.ButtonStyle.danger),
        )
        await notify(interaction.client, trade["buyer_id"], listing, embed=embed, view=view)


async def handle_tradeurl(interaction: discord.Interaction, trade_id: int) -> None:
    trade = db_of(interaction).get_trade(trade_id)
    if trade is None or trade["seller_id"] != interaction.user.id:
        return await reply(interaction, "この操作は出品者のみ可能です。")
    if trade["status"] not in ("requested", "page_sent"):
        return await reply(interaction, "この取引はすでに終了しています。")
    await interaction.response.send_modal(TradeUrlModal(trade_id))


async def handle_recv(interaction: discord.Interaction, trade_id: int) -> None:
    db = db_of(interaction)
    trade = db.get_trade(trade_id)
    if trade is None or trade["buyer_id"] != interaction.user.id:
        return await reply(interaction, "受け取りの報告は購入者のみ可能です。")
    if trade["status"] == "requested":
        return await reply(interaction, "まだ取引ページが届いていません。")
    if not db.complete_trade(trade_id):
        return await reply(interaction, "この取引はすでに終了しています。")

    # 取引ページへのリンクだけ残し、操作ボタンは外す
    keep: list[discord.ui.Item] = []
    if trade["trade_url"]:
        keep.append(discord.ui.Button(label="取引ページを開く", emoji="🔗", url=trade["trade_url"]))
    await interaction.response.edit_message(view=make_view(*keep) if keep else None)
    await refresh_listing(interaction.client, trade["listing_id"])

    listing = db.get_listing(trade["listing_id"])
    trade = db.get_trade(trade_id)
    assert listing is not None and trade is not None
    buyer_embed = ui.trade_embed(
        "🎉 取引が完了しました",
        "受け取りありがとうございました！出品ページは掲載終了になりました。\n"
        "よろしければ出品者の評価をお願いします（任意）。",
        listing,
        trade,
        color=discord.Color.green(),
    )
    await interaction.followup.send(
        embed=buyer_embed, view=rate_view(trade_id), ephemeral=interaction.guild is not None
    )
    seller_embed = ui.trade_embed(
        "🎉 購入者が受け取りを確認しました",
        "取引が完了し、出品ページは掲載終了になりました。お疲れさまでした！\n"
        "よろしければ購入者の評価をお願いします（任意）。",
        listing,
        trade,
        color=discord.Color.green(),
    )
    await notify(interaction.client, trade["seller_id"], listing, embed=seller_embed, view=rate_view(trade_id))


async def handle_tradecancel(interaction: discord.Interaction, trade_id: int) -> None:
    db = db_of(interaction)
    trade = db.get_trade(trade_id)
    user_id = interaction.user.id
    if trade is None or user_id not in (trade["seller_id"], trade["buyer_id"]):
        return await reply(interaction, "この取引の当事者のみ操作できます。")
    if trade["status"] not in ("requested", "page_sent"):
        return await reply(interaction, "この取引はすでに終了しています。")

    async def on_confirm(i: discord.Interaction) -> None:
        if not db.cancel_trade(trade_id):
            return await i.response.edit_message(content="この取引はすでに終了しています。", embed=None, view=None)
        await i.response.edit_message(
            content="✖️ 取引をキャンセルしました。商品は再び「出品中」に戻りました。", embed=None, view=None
        )
        await refresh_listing(i.client, trade["listing_id"])
        listing = db.get_listing(trade["listing_id"])
        assert listing is not None
        other = trade["buyer_id"] if user_id == trade["seller_id"] else trade["seller_id"]
        embed = ui.trade_embed(
            "✖️ 取引がキャンセルされました",
            f"<@{user_id}> さんが取引をキャンセルしました。",
            listing,
            db.get_trade(trade_id),  # type: ignore[arg-type]
            color=discord.Color.red(),
        )
        await notify(i.client, other, listing, embed=embed)

    await reply(
        interaction,
        "本当にこの取引をキャンセルしますか？\n商品は「出品中」に戻り、相手にも通知されます。",
        view=ConfirmView(user_id, "キャンセルする", on_confirm, style=discord.ButtonStyle.danger),
    )


class RatingModal(BaseModal, title="⭐ 取引相手を評価する"):
    def __init__(self, trade_id: int) -> None:
        super().__init__(timeout=900)
        self.trade_id = trade_id
        self.score = discord.ui.Select(
            placeholder="評価を選んでください",
            options=[
                discord.SelectOption(label="★★★★★ とても良い", value="5"),
                discord.SelectOption(label="★★★★☆ 良い", value="4"),
                discord.SelectOption(label="★★★☆☆ ふつう", value="3"),
                discord.SelectOption(label="★★☆☆☆ 残念", value="2"),
                discord.SelectOption(label="★☆☆☆☆ 悪い", value="1"),
            ],
        )
        self.comment = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
            placeholder="例）迅速で丁寧な対応ありがとうございました！",
        )
        self.add_item(discord.ui.Label(text="評価", component=self.score))
        self.add_item(discord.ui.Label(text="コメント（任意）", component=self.comment))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = db_of(interaction)
        trade = db.get_trade(self.trade_id)
        rater_id = interaction.user.id
        if trade is None or rater_id not in (trade["seller_id"], trade["buyer_id"]):
            return await reply(interaction, "この取引の当事者のみ評価できます。")
        rater_is_seller = rater_id == trade["seller_id"]
        ratee_id = trade["buyer_id"] if rater_is_seller else trade["seller_id"]
        ratee_role = "buyer" if rater_is_seller else "seller"
        score = int(self.score.values[0])
        comment = self.comment.value.strip()
        if not db.add_rating(
            trade_id=self.trade_id,
            rater_id=rater_id,
            ratee_id=ratee_id,
            ratee_role=ratee_role,
            score=score,
            comment=comment,
        ):
            return await reply(interaction, "この取引はすでに評価済みです。")

        await interaction.response.edit_message(content=f"✅ 評価を送信しました：{ui.stars(score)}", view=None)

        listing = db.get_listing(trade["listing_id"])
        assert listing is not None
        embed = discord.Embed(
            title="⭐ 評価が届きました",
            description=f"<@{rater_id}> さんから **{ui.stars(score)}** の評価が届きました。",
            color=ui.MARKET_COLOR,
        )
        embed.add_field(name="商品", value=f"{listing['title']}（#{listing['id']}）", inline=False)
        if comment:
            embed.add_field(name="コメント", value=comment, inline=False)
        items: list[discord.ui.Item] = [action_button("profile", ratee_id, "自分の評価を見る", emoji="📊")]
        if not db.has_rated(self.trade_id, ratee_id):
            items.insert(0, action_button("rate", self.trade_id, "相手を評価する", emoji="⭐", style=discord.ButtonStyle.primary))
        await notify(interaction.client, ratee_id, listing, embed=embed, view=make_view(*items))

        # 出品者の評価が変わったら、その人の出品ページの★表示も更新する
        if ratee_role == "seller":
            for row in db.listings_by_seller(ratee_id, ("open", "trading"))[:10]:
                await refresh_listing(interaction.client, row["id"])


async def handle_rate(interaction: discord.Interaction, trade_id: int) -> None:
    db = db_of(interaction)
    trade = db.get_trade(trade_id)
    if trade is None or interaction.user.id not in (trade["seller_id"], trade["buyer_id"]):
        return await reply(interaction, "この取引の当事者のみ評価できます。")
    if trade["status"] != "completed":
        return await reply(interaction, "評価は取引完了後にできます。")
    if db.has_rated(trade_id, interaction.user.id):
        return await reply(interaction, "この取引はすでに評価済みです。")
    await interaction.response.send_modal(RatingModal(trade_id))


# ======================================================================
# 値下げ交渉
# ======================================================================
def offer_view(offer_id: int) -> discord.ui.View:
    return make_view(
        action_button("ofaccept", offer_id, "承諾して取引開始", emoji="✅", style=discord.ButtonStyle.success),
        action_button("ofcounter", offer_id, "別の価格を提案", emoji="↩️", style=discord.ButtonStyle.primary),
        action_button("ofreject", offer_id, "お断りする", emoji="✖️", style=discord.ButtonStyle.danger),
    )


def offer_parties(offer: Mapping[str, Any], listing: Mapping[str, Any]) -> tuple[int, int]:
    """(提案した人, 返答する人) を返す。"""
    if offer["proposed_by"] == "buyer":
        return offer["buyer_id"], listing["seller_id"]
    return listing["seller_id"], offer["buyer_id"]


def offer_error(
    db: Database, offer_id: int, user_id: int
) -> tuple[Optional[str], Optional[Any], Optional[dict[str, Any]]]:
    offer = db.get_offer(offer_id)
    listing = db.get_listing(offer["listing_id"]) if offer else None
    if offer is None or listing is None:
        return "この交渉は見つかりませんでした。", None, None
    if offer_parties(offer, listing)[1] != user_id:
        return "この交渉に返答できるのは相手の方だけです。", offer, listing
    if offer["status"] != "pending":
        return "この交渉はすでに終了しています。", offer, listing
    if listing["status"] != "open":
        return "この商品はすでに取引中か、出品が終了しています。", offer, listing
    return None, offer, listing


class OfferModal(BaseModal, title="💬 値下げ交渉"):
    price = discord.ui.TextInput(label="希望価格（円）", placeholder="例）20000", max_length=12)
    message = discord.ui.TextInput(
        label="出品者へのメッセージ（任意）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
        placeholder="例）即決できます！よろしくお願いします。",
    )

    def __init__(self, listing_id: int) -> None:
        super().__init__(timeout=900)
        self.listing_id = listing_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = db_of(interaction)
        listing = db.get_listing(self.listing_id)
        if error := buyable_error(listing, interaction.user.id):
            return await reply(interaction, error)
        assert listing is not None
        price = parse_price(self.price.value)
        if price is None or price >= listing["price"]:
            return await reply(interaction, f"⚠️ 現在の価格（{yen(listing['price'])}）より低い金額を数字で入力してください。")

        offer_id = db.create_offer(listing["id"], interaction.user.id, price, self.message.value.strip(), "buyer")
        await reply(interaction, f"✅ 出品者に {yen(price)} で値下げ交渉を送りました。返事はDMで届きます。")
        offer = db.get_offer(offer_id)
        assert offer is not None
        await notify(
            interaction.client,
            listing["seller_id"],
            listing,
            embed=ui.offer_embed("💬 値下げ交渉が届きました", listing, offer, from_user_id=interaction.user.id),
            view=offer_view(offer_id),
        )


class CounterOfferModal(BaseModal, title="↩️ 別の価格を提案"):
    price = discord.ui.TextInput(label="提案する価格（円）", placeholder="例）22000", max_length=12)
    message = discord.ui.TextInput(
        label="メッセージ（任意）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
        placeholder="例）22,000円でしたらすぐにお譲りできます！",
    )

    def __init__(self, offer_id: int) -> None:
        super().__init__(timeout=900)
        self.offer_id = offer_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        price = parse_price(self.price.value)
        if price is None:
            return await reply(interaction, f"⚠️ 価格は 1〜{MAX_PRICE:,} の数字で入力してください。")
        db = db_of(interaction)
        error, offer, listing = offer_error(db, self.offer_id, interaction.user.id)
        if error:
            return await reply(interaction, error)
        assert offer is not None and listing is not None
        if not db.resolve_offer(self.offer_id, "countered"):
            return await reply(interaction, "この交渉はすでに終了しています。")

        responder_is_seller = interaction.user.id == listing["seller_id"]
        new_offer_id = db.create_offer(
            listing["id"],
            offer["buyer_id"],
            price,
            self.message.value.strip(),
            "seller" if responder_is_seller else "buyer",
        )
        await interaction.response.edit_message(content=f"↩️ {yen(price)} を提案しました。返事はDMで届きます。", view=None)
        new_offer = db.get_offer(new_offer_id)
        assert new_offer is not None
        other = offer["buyer_id"] if responder_is_seller else listing["seller_id"]
        await notify(
            interaction.client,
            other,
            listing,
            embed=ui.offer_embed("↩️ 価格の提案が届きました", listing, new_offer, from_user_id=interaction.user.id),
            view=offer_view(new_offer_id),
        )


async def handle_nego(interaction: discord.Interaction, listing_id: int) -> None:
    listing = db_of(interaction).get_listing(listing_id)
    if error := buyable_error(listing, interaction.user.id):
        return await reply(interaction, error)
    await interaction.response.send_modal(OfferModal(listing_id))


async def handle_ofaccept(interaction: discord.Interaction, offer_id: int) -> None:
    db = db_of(interaction)
    error, offer, listing = offer_error(db, offer_id, interaction.user.id)
    if error:
        return await reply(interaction, error)
    assert offer is not None and listing is not None
    if not db.resolve_offer(offer_id, "accepted"):
        return await reply(interaction, "この交渉はすでに終了しています。")
    await interaction.response.edit_message(content=f"✅ {yen(offer['price'])} で承諾しました。取引を開始します。", view=None)
    trade_id = await start_trade(interaction, listing["id"], offer["buyer_id"], offer["price"], offer_id=offer_id)
    if trade_id is None:
        await interaction.followup.send("⚠️ この商品はすでに取引中か、出品が終了しています。")


async def handle_ofreject(interaction: discord.Interaction, offer_id: int) -> None:
    db = db_of(interaction)
    error, offer, listing = offer_error(db, offer_id, interaction.user.id)
    if error:
        return await reply(interaction, error)
    assert offer is not None and listing is not None
    if not db.resolve_offer(offer_id, "rejected"):
        return await reply(interaction, "この交渉はすでに終了しています。")
    await interaction.response.edit_message(content="✖️ この交渉をお断りしました。", view=None)
    proposer_id, _ = offer_parties(offer, listing)
    await notify(
        interaction.client,
        proposer_id,
        listing,
        embed=ui.offer_embed("😢 交渉はお断りされました", listing, offer, from_user_id=proposer_id),
    )


async def handle_ofcounter(interaction: discord.Interaction, offer_id: int) -> None:
    error, _, _ = offer_error(db_of(interaction), offer_id, interaction.user.id)
    if error:
        return await reply(interaction, error)
    await interaction.response.send_modal(CounterOfferModal(offer_id))


# ======================================================================
# ディスパッチャ
# ======================================================================
HANDLERS: dict[str, Handler] = {
    "sell": handle_sell,
    "buy": handle_buy,
    "nego": handle_nego,
    "manage": handle_manage,
    "profile": handle_profile,
    "tradeurl": handle_tradeurl,
    "recv": handle_recv,
    "tradecancel": handle_tradecancel,
    "rate": handle_rate,
    "ofaccept": handle_ofaccept,
    "ofreject": handle_ofreject,
    "ofcounter": handle_ofcounter,
}


class ActionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"fm:(?P<action>[a-z]+):(?P<id>\d+)"):
    def __init__(self, item: discord.ui.Button, action: str, target_id: int) -> None:
        super().__init__(item)
        self.action = action
        self.target_id = target_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any, /
    ) -> "ActionButton":
        return cls(item, match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = HANDLERS.get(self.action)
        if handler is None:
            return await reply(interaction, "このボタンは現在使えません。")
        try:
            await handler(interaction, self.target_id)
        except Exception:
            log.exception("Action %s:%s failed", self.action, self.target_id)
            await reply(interaction, ERROR_TEXT)
