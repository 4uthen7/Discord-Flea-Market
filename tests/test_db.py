import unittest

from fleamarket.db import Database


class FleaMarketDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.lid = self.db.create_listing(
            guild_id=1, seller_id=10, title="テスト商品", price=3000,
            condition="新品・未使用", description="", images=[],
        )

    def test_purchase_flow(self) -> None:
        self.assertIsNone(self.db.start_trade(self.lid, 10, 3000), "出品者は自分の商品を買えない")
        tid = self.db.start_trade(self.lid, 20, 3000)
        self.assertIsNotNone(tid)
        self.assertIsNone(self.db.start_trade(self.lid, 30, 3000), "取引中は二重購入できない")
        self.assertEqual(self.db.get_listing(self.lid)["status"], "trading")

        self.assertFalse(self.db.complete_trade(tid), "取引ページ送付前は完了できない")
        self.assertTrue(self.db.set_trade_url(tid, "https://example.com/t/1"))
        self.assertTrue(self.db.complete_trade(tid))
        self.assertEqual(self.db.get_listing(self.lid)["status"], "sold")
        self.assertFalse(self.db.cancel_trade(tid))

    def test_cancel_reopens_listing(self) -> None:
        tid = self.db.start_trade(self.lid, 20, 3000)
        self.assertTrue(self.db.cancel_trade(tid))
        self.assertEqual(self.db.get_listing(self.lid)["status"], "open")

    def test_offers_expire_when_trade_starts(self) -> None:
        o1 = self.db.create_offer(self.lid, 20, 2500, "", "buyer")
        o2 = self.db.create_offer(self.lid, 30, 2000, "", "buyer")
        self.assertTrue(self.db.resolve_offer(o1, "accepted"))
        self.db.start_trade(self.lid, 20, 2500, o1)
        self.assertEqual(self.db.get_offer(o2)["status"], "expired")
        self.assertFalse(self.db.resolve_offer(o2, "accepted"))

    def test_new_offer_replaces_old_one_from_same_buyer(self) -> None:
        o1 = self.db.create_offer(self.lid, 20, 2500, "", "buyer")
        self.db.create_offer(self.lid, 20, 2700, "", "buyer")
        self.assertEqual(self.db.get_offer(o1)["status"], "expired")

    def test_ratings(self) -> None:
        tid = self.db.start_trade(self.lid, 20, 3000)
        self.db.set_trade_url(tid, "https://example.com")
        self.db.complete_trade(tid)
        self.assertTrue(self.db.add_rating(trade_id=tid, rater_id=20, ratee_id=10, ratee_role="seller", score=5, comment="良い"))
        self.assertFalse(self.db.add_rating(trade_id=tid, rater_id=20, ratee_id=10, ratee_role="seller", score=1, comment=""))
        self.assertTrue(self.db.add_rating(trade_id=tid, rater_id=10, ratee_id=20, ratee_role="buyer", score=4, comment=""))

        seller = self.db.rating_summary(10)
        self.assertEqual((seller["seller_avg"], seller["seller_count"], seller["sold"]), (5, 1, 1))
        buyer = self.db.rating_summary(20)
        self.assertEqual((buyer["buyer_avg"], buyer["bought"]), (4, 1))
        self.assertEqual(self.db.recent_ratings(10)[0]["title"], "テスト商品")


if __name__ == "__main__":
    unittest.main()
