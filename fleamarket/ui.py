"""Embed とボタンの見た目をまとめたモジュール。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Optional

import discord

CONDITIONS = [
    "新品・未使用",
    "未使用に近い",
    "目立った傷や汚れなし",
    "やや傷や汚れあり",
    "傷や汚れあり",
    "全体的に状態が悪い",
]

LISTING_STATUS = {
    "open": ("🟢 出品中", discord.Color.green()),
    "trading": ("🟡 取引中", discord.Color.gold()),
    "sold": ("🔴 SOLD（掲載終了）", discord.Color.dark_grey()),
    "withdrawn": ("⚫ 出品取り下げ", discord.Color.darker_grey()),
}

# フォーラムに出品するときに付けるステータスタグ（/フリマ設置 で自動作成）
STATUS_TAGS = {
    "open": ("出品中", "🟢"),
    "trading": ("取引中", "🟡"),
    "sold": ("SOLD", "🔴"),
    "withdrawn": ("取り下げ", "⚫"),
}
STATUS_TAG_NAMES = {name for name, _ in STATUS_TAGS.values()}

TRADE_STATUS = {
    "requested": "📨 取引ページ待ち",
    "page_sent": "📦 発送・受け取り待ち",
    "completed": "✅ 取引完了",
    "cancelled": "❌ キャンセル",
}

MARKET_COLOR = discord.Color.from_rgb(255, 112, 67)


# 招待リンクで要求する権限（README の権限一覧と揃える）
BOT_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    send_messages_in_threads=True,
    create_public_threads=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
    manage_threads=True,
    manage_channels=True,
)


def invite_url(client_id: int) -> str:
    return discord.utils.oauth_url(client_id, permissions=BOT_PERMISSIONS, scopes=("bot", "applications.commands"))


def welcome_embed() -> discord.Embed:
    return discord.Embed(
        title="🛍️ フリマBotを導入いただきありがとうございます！",
        description=(
            "**はじめかた（管理者の方）**\n"
            "１. フリマ用のフォーラムチャンネルを作る（テキストチャンネルでもOK）\n"
            "２. `/フリマ設置 チャンネル:#そのフォーラム` を実行\n\n"
            "これだけで、メンバーは「出品する」ボタンや `/出品` から出品できるようになります。\n\n"
            "⚠️ このBotは代金のやり取りを行いません。支払い・発送はアズカリなどの外部サービスで行われ、"
            "取引上のトラブルは当事者間で解決していただく形になります。サーバーのルールに禁止品などを明記しておくのがおすすめです。"
        ),
        color=MARKET_COLOR,
    )


def yen(price: int) -> str:
    return f"¥{price:,}"


def stars(avg: Optional[float]) -> str:
    if avg is None:
        return "☆☆☆☆☆"
    full = int(round(avg))
    return "★" * full + "☆" * (5 - full)


def rating_line(avg: Optional[float], count: int) -> str:
    if not count:
        return "まだ評価がありません"
    return f"{stars(avg)} **{avg:.1f}** （{count}件）"


def relative_time(sqlite_ts: Optional[str]) -> str:
    if not sqlite_ts:
        return ""
    ts = dt.datetime.strptime(sqlite_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    return discord.utils.format_dt(ts, "R")


def action_button(
    action: str,
    target_id: int,
    label: str,
    *,
    emoji: Optional[str] = None,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    disabled: bool = False,
    extra: Optional[int] = None,
) -> discord.ui.Button:
    """`fm:<action>:<id>[:<extra>]` 形式の custom_id を持つ永続ボタンを作る（actions.ActionButton が処理する）。"""
    custom_id = f"fm:{action}:{target_id}" + (f":{extra}" if extra is not None else "")
    return discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled, custom_id=custom_id)


def make_view(*items: discord.ui.Item) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for item in items:
        view.add_item(item)
    return view


# ------------------------------------------------------------------ 出品ページ
def listing_embeds(listing: Mapping[str, Any], seller_summary: Mapping[str, Any]) -> list[discord.Embed]:
    status_label, color = LISTING_STATUS[listing["status"]]
    title = listing["title"]
    if listing["status"] == "sold":
        title = f"【SOLD】{title}"
    elif listing["status"] == "withdrawn":
        title = f"【取り下げ】{title}"

    embed = discord.Embed(title=title, description=listing["description"] or "（説明なし）", color=color)
    embed.add_field(name="💴 価格", value=f"**{yen(listing['price'])}**", inline=True)
    embed.add_field(name="📦 商品の状態", value=listing["condition"], inline=True)
    embed.add_field(name="📌 ステータス", value=status_label, inline=True)
    embed.add_field(
        name="👤 出品者",
        value=f"<@{listing['seller_id']}>\n{rating_line(seller_summary['seller_avg'], seller_summary['seller_count'])}",
        inline=False,
    )
    embed.set_footer(text=f"出品ID #{listing['id']}")

    images: list[str] = listing["images"]
    if not images:
        return [embed]

    # 同じ url を持つ embed は Discord 上で1枚のギャラリーとしてまとめて表示される
    gallery_url = f"https://discord.com/channels/{listing['guild_id']}#listing-{listing['id']}"
    embed.url = gallery_url
    embed.set_image(url=f"attachment://{images[0]}")
    embeds = [embed]
    for name in images[1:4]:
        extra = discord.Embed(url=gallery_url, color=color)
        extra.set_image(url=f"attachment://{name}")
        embeds.append(extra)
    return embeds


def thread_name(listing: Mapping[str, Any]) -> str:
    """フォーラム投稿のタイトル（一覧で価格まで見えるようにする）。"""
    prefix = {"sold": "【SOLD】", "withdrawn": "【取り下げ】"}.get(listing["status"], "")
    return f"{prefix}{listing['title']}　{yen(listing['price'])}"[:100]


def forum_summary(listing: Mapping[str, Any]) -> str:
    """フォーラム一覧のプレビューに出る1行目。"""
    return f"💴 **{yen(listing['price'])}** ｜ 📦 {listing['condition']} ｜ 👤 出品者 <@{listing['seller_id']}>"


def status_tag(forum: discord.ForumChannel, status: str) -> Optional[discord.ForumTag]:
    name = STATUS_TAGS[status][0]
    return discord.utils.get(forum.available_tags, name=name)


def listing_view(listing: Mapping[str, Any]) -> discord.ui.View:
    lid = listing["id"]
    status = listing["status"]
    profile = action_button("profile", listing["seller_id"], "出品者の評価", emoji="⭐", extra=listing["guild_id"])
    if status in ("sold", "withdrawn"):
        return make_view(profile)

    trading = status == "trading"
    return make_view(
        action_button(
            "buy",
            lid,
            "取引中" if trading else "購入する",
            emoji="🛒",
            style=discord.ButtonStyle.success,
            disabled=trading,
        ),
        action_button("nego", lid, "値下げ交渉", emoji="💬", style=discord.ButtonStyle.primary, disabled=trading),
        profile,
        action_button("manage", lid, "出品者メニュー", emoji="⚙️"),
    )


def panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛍️ サーバーフリマ",
        description=(
            "メンバー同士で気軽に売り買いできるフリマです。\n\n"
            "**出品する** … 下のボタン or `/出品` でフォームを埋めるだけ！\n"
            "**買う** … 気になる商品の「🛒 購入する」を押すと出品者にDMで通知されます\n"
            "**値下げ交渉** … 「💬 値下げ交渉」から希望価格を送れます\n"
            "**評価** … 「⭐ 出品者の評価」でいつでも確認できます\n\n"
            "📌 取引の流れ\n"
            "１. 購入ボタン → 出品者にDM通知\n"
            "２. 出品者がアズカリで取引ページを作成し、URLをBotに送信 → 購入者にDM通知\n"
            "３. 商品が届いたら購入者が「受け取りました」→ 掲載終了\n"
            "４. お互いに評価（任意）"
        ),
        color=MARKET_COLOR,
    )
    embed.set_footer(text="画像付きで出品したい場合はボタンのフォームから画像を添付できます")
    return embed


# -------------------------------------------------------------------- 取引DM
def trade_embed(
    title: str,
    description: str,
    listing: Mapping[str, Any],
    trade: Mapping[str, Any],
    *,
    color: discord.Color = MARKET_COLOR,
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.add_field(name="商品", value=f"{listing['title']}（#{listing['id']}）", inline=False)
    embed.add_field(name="取引価格", value=yen(trade["price"]), inline=True)
    embed.add_field(name="出品者", value=f"<@{trade['seller_id']}>", inline=True)
    embed.add_field(name="購入者", value=f"<@{trade['buyer_id']}>", inline=True)
    if listing.get("channel_id") and listing.get("message_id"):
        embed.add_field(
            name="出品ページ",
            value=f"https://discord.com/channels/{listing['guild_id']}/{listing['channel_id']}/{listing['message_id']}",
            inline=False,
        )
    embed.set_footer(text=f"取引ID #{trade['id']}")
    return embed


def offer_embed(
    title: str, listing: Mapping[str, Any], offer: Mapping[str, Any], *, from_user_id: int
) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    embed.add_field(name="商品", value=f"{listing['title']}（#{listing['id']}）", inline=False)
    embed.add_field(name="現在の価格", value=yen(listing["price"]), inline=True)
    embed.add_field(name="提案価格", value=f"**{yen(offer['price'])}**", inline=True)
    embed.add_field(name="提案者", value=f"<@{from_user_id}>", inline=True)
    if offer["message"]:
        embed.add_field(name="メッセージ", value=offer["message"], inline=False)
    embed.set_footer(text=f"交渉ID #{offer['id']}")
    return embed


# ------------------------------------------------------------------- 評価
def profile_embed(
    user: discord.abc.User, summary: Mapping[str, Any], recent: list, *, scope: str = ""
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{user.display_name} さんの評価",
        description=f"## {rating_line(summary['avg'], summary['count'])}",
        color=MARKET_COLOR,
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(
        name="🏷️ 出品者として", value=rating_line(summary["seller_avg"], summary["seller_count"]), inline=True
    )
    embed.add_field(
        name="🛒 購入者として", value=rating_line(summary["buyer_avg"], summary["buyer_count"]), inline=True
    )
    embed.add_field(
        name="📊 取引実績",
        value=f"販売 {summary['sold']}件 ／ 購入 {summary['bought']}件 ／ 出品中 {summary['open_listings']}件",
        inline=False,
    )
    if recent:
        lines = []
        for r in recent:
            role = "出品者" if r["ratee_role"] == "seller" else "購入者"
            comment = f"「{r['comment']}」" if r["comment"] else "（コメントなし）"
            lines.append(
                f"{stars(r['score'])} {role}として・{r['title']}\n"
                f"└ {comment} — <@{r['rater_id']}> {relative_time(r['created_at'])}"
            )
        embed.add_field(name="💬 最近の評価", value="\n".join(lines)[:1024], inline=False)
    if scope:
        embed.set_footer(text=scope)
    return embed
