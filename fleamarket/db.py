"""SQLite による永続化レイヤー。

状態遷移はすべて「条件付き UPDATE」で行い、同時に2人が購入ボタンを
押した場合などでも二重取引にならないようにしている。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id          INTEGER PRIMARY KEY,
    market_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS listings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER,
    message_id  INTEGER,
    seller_id   INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    price       INTEGER NOT NULL,
    condition   TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    images      TEXT    NOT NULL DEFAULT '[]',
    status      TEXT    NOT NULL DEFAULT 'open',   -- open / trading / sold / withdrawn
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id   INTEGER NOT NULL REFERENCES listings(id),
    seller_id    INTEGER NOT NULL,
    buyer_id     INTEGER NOT NULL,
    price        INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'requested', -- requested / page_sent / completed / cancelled
    trade_url    TEXT,
    created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    buyer_id    INTEGER NOT NULL,
    price       INTEGER NOT NULL,
    message     TEXT    NOT NULL DEFAULT '',
    proposed_by TEXT    NOT NULL,                    -- buyer / seller
    status      TEXT    NOT NULL DEFAULT 'pending',  -- pending / accepted / rejected / countered / expired
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ratings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   INTEGER NOT NULL REFERENCES trades(id),
    rater_id   INTEGER NOT NULL,
    ratee_id   INTEGER NOT NULL,
    ratee_role TEXT    NOT NULL,                     -- seller / buyer（評価された側の立場）
    score      INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
    comment    TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (trade_id, rater_id)
);

CREATE INDEX IF NOT EXISTS idx_ratings_ratee ON ratings (ratee_id);
CREATE INDEX IF NOT EXISTS idx_trades_listing ON trades (listing_id);
"""

ACTIVE_TRADE_STATUSES = ("requested", "page_sent")


class Database:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def _one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- settings
    def get_market_channel(self, guild_id: int) -> Optional[int]:
        row = self._one("SELECT market_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return row["market_channel_id"] if row else None

    def set_market_channel(self, guild_id: int, channel_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO guild_settings (guild_id, market_channel_id) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET market_channel_id = excluded.market_channel_id",
                (guild_id, channel_id),
            )

    # ---------------------------------------------------------------- listings
    def create_listing(
        self,
        *,
        guild_id: int,
        seller_id: int,
        title: str,
        price: int,
        condition: str,
        description: str,
        images: list[str],
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO listings (guild_id, seller_id, title, price, condition, description, images) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, seller_id, title, price, condition, description, json.dumps(images)),
            )
        return int(cur.lastrowid)

    def delete_listing(self, listing_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))

    def set_listing_message(self, listing_id: int, channel_id: int, message_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE listings SET channel_id = ?, message_id = ? WHERE id = ?",
                (channel_id, message_id, listing_id),
            )

    def get_listing(self, listing_id: int) -> Optional[dict[str, Any]]:
        row = self._one("SELECT * FROM listings WHERE id = ?", (listing_id,))
        if row is None:
            return None
        listing = dict(row)
        listing["images"] = json.loads(listing["images"])
        return listing

    def update_listing(self, listing_id: int, *, title: str, price: int, condition: str, description: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE listings SET title = ?, price = ?, condition = ?, description = ? "
                "WHERE id = ? AND status = 'open'",
                (title, price, condition, description, listing_id),
            )
        return cur.rowcount == 1

    def withdraw_listing(self, listing_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE listings SET status = 'withdrawn' WHERE id = ? AND status = 'open'", (listing_id,)
            )
            if cur.rowcount == 1:
                self.conn.execute(
                    "UPDATE offers SET status = 'expired' WHERE listing_id = ? AND status = 'pending'",
                    (listing_id,),
                )
        return cur.rowcount == 1

    def listings_by_seller(self, seller_id: int, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self._all(
            f"SELECT * FROM listings WHERE seller_id = ? AND status IN ({marks}) ORDER BY id DESC",
            (seller_id, *statuses),
        )

    # ------------------------------------------------------------------ trades
    def start_trade(self, listing_id: int, buyer_id: int, price: int, accepted_offer_id: Optional[int] = None) -> Optional[int]:
        """出品を「取引中」にして取引を作成する。既に売れていたら None。"""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE listings SET status = 'trading' WHERE id = ? AND status = 'open' AND seller_id != ?",
                (listing_id, buyer_id),
            )
            if cur.rowcount != 1:
                return None
            seller_id = self._one("SELECT seller_id FROM listings WHERE id = ?", (listing_id,))["seller_id"]
            cur = self.conn.execute(
                "INSERT INTO trades (listing_id, seller_id, buyer_id, price) VALUES (?, ?, ?, ?)",
                (listing_id, seller_id, buyer_id, price),
            )
            # 取引が始まったら、ほかの交渉はすべて無効にする
            self.conn.execute(
                "UPDATE offers SET status = 'expired' WHERE listing_id = ? AND status = 'pending' AND id != ?",
                (listing_id, accepted_offer_id or 0),
            )
        return int(cur.lastrowid)

    def get_trade(self, trade_id: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM trades WHERE id = ?", (trade_id,))

    def set_trade_url(self, trade_id: int, url: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE trades SET trade_url = ?, status = 'page_sent' WHERE id = ? AND status IN ('requested', 'page_sent')",
                (url, trade_id),
            )
        return cur.rowcount == 1

    def complete_trade(self, trade_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE trades SET status = 'completed', completed_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'page_sent'",
                (trade_id,),
            )
            if cur.rowcount == 1:
                self.conn.execute(
                    "UPDATE listings SET status = 'sold' WHERE id = (SELECT listing_id FROM trades WHERE id = ?)",
                    (trade_id,),
                )
        return cur.rowcount == 1

    def cancel_trade(self, trade_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE trades SET status = 'cancelled' WHERE id = ? AND status IN ('requested', 'page_sent')",
                (trade_id,),
            )
            if cur.rowcount == 1:
                self.conn.execute(
                    "UPDATE listings SET status = 'open' "
                    "WHERE id = (SELECT listing_id FROM trades WHERE id = ?) AND status = 'trading'",
                    (trade_id,),
                )
        return cur.rowcount == 1

    def active_trades_for_user(self, user_id: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT t.*, l.title FROM trades t JOIN listings l ON l.id = t.listing_id "
            "WHERE (t.seller_id = ? OR t.buyer_id = ?) AND t.status IN ('requested', 'page_sent') "
            "ORDER BY t.id DESC",
            (user_id, user_id),
        )

    # ------------------------------------------------------------------ offers
    def create_offer(self, listing_id: int, buyer_id: int, price: int, message: str, proposed_by: str) -> int:
        with self.conn:
            # 同じ購入希望者からの古い交渉は、新しい提案で置き換える
            self.conn.execute(
                "UPDATE offers SET status = 'expired' WHERE listing_id = ? AND buyer_id = ? AND status = 'pending'",
                (listing_id, buyer_id),
            )
            cur = self.conn.execute(
                "INSERT INTO offers (listing_id, buyer_id, price, message, proposed_by) VALUES (?, ?, ?, ?, ?)",
                (listing_id, buyer_id, price, message, proposed_by),
            )
        return int(cur.lastrowid)

    def get_offer(self, offer_id: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM offers WHERE id = ?", (offer_id,))

    def resolve_offer(self, offer_id: int, status: str) -> bool:
        """保留中の交渉を accepted / rejected / countered にする。"""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE offers SET status = ? WHERE id = ? AND status = 'pending'", (status, offer_id)
            )
        return cur.rowcount == 1

    # ----------------------------------------------------------------- ratings
    def add_rating(self, *, trade_id: int, rater_id: int, ratee_id: int, ratee_role: str, score: int, comment: str) -> bool:
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO ratings (trade_id, rater_id, ratee_id, ratee_role, score, comment) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (trade_id, rater_id, ratee_id, ratee_role, score, comment),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def has_rated(self, trade_id: int, rater_id: int) -> bool:
        return self._one("SELECT 1 FROM ratings WHERE trade_id = ? AND rater_id = ?", (trade_id, rater_id)) is not None

    def rating_summary(self, user_id: int) -> dict[str, Any]:
        def agg(role: Optional[str]) -> tuple[Optional[float], int]:
            if role is None:
                row = self._one("SELECT AVG(score) a, COUNT(*) c FROM ratings WHERE ratee_id = ?", (user_id,))
            else:
                row = self._one(
                    "SELECT AVG(score) a, COUNT(*) c FROM ratings WHERE ratee_id = ? AND ratee_role = ?",
                    (user_id, role),
                )
            return row["a"], row["c"]

        overall_avg, overall_count = agg(None)
        seller_avg, seller_count = agg("seller")
        buyer_avg, buyer_count = agg("buyer")
        sold = self._one(
            "SELECT COUNT(*) c FROM trades WHERE seller_id = ? AND status = 'completed'", (user_id,)
        )["c"]
        bought = self._one(
            "SELECT COUNT(*) c FROM trades WHERE buyer_id = ? AND status = 'completed'", (user_id,)
        )["c"]
        open_listings = self._one(
            "SELECT COUNT(*) c FROM listings WHERE seller_id = ? AND status = 'open'", (user_id,)
        )["c"]
        return {
            "avg": overall_avg,
            "count": overall_count,
            "seller_avg": seller_avg,
            "seller_count": seller_count,
            "buyer_avg": buyer_avg,
            "buyer_count": buyer_count,
            "sold": sold,
            "bought": bought,
            "open_listings": open_listings,
        }

    def recent_ratings(self, user_id: int, limit: int = 5) -> list[sqlite3.Row]:
        return self._all(
            "SELECT r.*, l.title FROM ratings r "
            "JOIN trades t ON t.id = r.trade_id JOIN listings l ON l.id = t.listing_id "
            "WHERE r.ratee_id = ? ORDER BY r.id DESC LIMIT ?",
            (user_id, limit),
        )
