"""services/utils/telegram -- the one simple alert format (2026-11-18). Pure text, nothing is sent."""
import unittest

from services.utils import telegram as T


class TestFormat(unittest.TestCase):
    def test_bought_and_sold_blocks(self):
        b = T.bought_block("SBIN", "15 shares", 812.4, 12186, "55-day high", stop=790.0, risk=336)
        self.assertEqual(b, "<b>SBIN</b> · 15 shares at 812.40 = ₹12,186\nWhy: 55-day high\nStop: 790.00 (most it can lose ≈ ₹336)")
        s = T.sold_block("ETH", "0.5 ETH", 2000, 2300, 1234, 56, "hit the 15% profit target", "3h 20m")
        self.assertIn("Profit: <b>+₹1,234</b> after ₹56 fees", s); self.assertIn("Why: hit the 15% profit target · held 3h 20m", s)
        self.assertIn("Loss: <b>−₹500</b>", T.sold_block("X", "1 share", 10, 5, -500))

    def test_message_and_account_line(self):
        m = T.message("🟢", "BOUGHT · Breakout", ["a", "", "b"], value=101234, start=100000)
        self.assertEqual(m, "🟢 <b>BOUGHT · Breakout</b>\n\na\n\nb\n\nAccount: <b>₹1,01,234</b> (+1.2% since start)")
        self.assertEqual(T.account_line(5000), "Account: <b>₹5,000</b>")
        self.assertEqual(T.account_line(99000, 100000), "Account: <b>₹99,000</b> (−1.0% since start)")

    def test_prices_keep_their_digits(self):
        self.assertEqual((T.price(0.06819), T.price(7.942), T.price(7.9), T.price(812.4), T.price(49215)), ("0.06819", "7.942", "7.9", "812.40", "49,215.00"))

    def test_plain_reasons(self):
        self.assertEqual(T.why_text("TAKE PROFIT (15%)"), "hit the 15% profit target")
        self.assertEqual(T.why_text("STOP LOSS (7%)"), "hit the stop-loss")
        self.assertEqual(T.why_text("TIME STOP (24h)"), "24 hours passed")
        self.assertEqual(T.why_text("held_40_days"), "held 40 trading days, as planned")
        self.assertEqual(T.why_text("something_new"), "something new")

    def test_icons_and_problem(self):
        self.assertEqual((T.trade_icon([], 0), T.trade_icon([], 2), T.trade_icon([100, -50], 0), T.trade_icon([-10], 1)), ("📊", "🟢", "✅", "❌"))
        p = T.problem("Crypto engine", "Has not run for 40 minutes.", "journalctl --user -u x -n 50")
        self.assertEqual(p, "⚠️ <b>PROBLEM · Crypto engine</b>\n\nHas not run for 40 minutes.\n\nCheck: <code>journalctl --user -u x -n 50</code>")


if __name__ == "__main__":
    unittest.main()
