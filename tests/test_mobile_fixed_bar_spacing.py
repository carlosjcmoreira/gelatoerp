import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MobileFixedBarSpacingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = (ROOT / "flask_app/static/style.css").read_text()
        cls.base = (ROOT / "flask_app/templates/base.html").read_text()
        cls.pastelaria_count = (
            ROOT / "flask_app/templates/vendas/contagem_pastelaria.html"
        ).read_text()
        cls.pesagem = (
            ROOT / "flask_app/templates/vendas/pesagem.html"
        ).read_text()

    def test_mobile_nav_and_back_bar_reserve_their_combined_height(self):
        rule = re.search(
            r"main\.with-nav\.has-back-bar-content\s*\{(?P<body>[^}]+)\}",
            self.css,
        )
        self.assertIsNotNone(rule)
        body = rule.group("body")
        self.assertIn("var(--bottom-nav-height)", body)
        self.assertIn("env(safe-area-inset-bottom)", body)
        self.assertIn("var(--back-bar-height)", body)
        self.assertIn("var(--fixed-bar-content-gap)", body)

    def test_back_bar_and_content_share_one_height_variable(self):
        self.assertRegex(
            self.css,
            r"\.back-bar\s*\{[^}]*height:\s*var\(--back-bar-height\)",
        )
        self.assertRegex(
            self.css,
            r"\.has-back-bar-content\s*\{[^}]*"
            r"var\(--back-bar-height\)[^}]*var\(--fixed-bar-content-gap\)",
        )

    def test_keyboard_recovery_uses_rendered_bar_position(self):
        self.assertIn(
            "bar.getBoundingClientRect().top - 8",
            self.base,
        )
        self.assertNotIn("var fixedBottom = isMobile ? 154 : 56", self.base)
        self.assertIn(
            "backBar.getBoundingClientRect().top - 8",
            self.pesagem,
        )
        self.assertNotIn("const fixedBottom = isMobile ? 160 : 56", self.pesagem)

    def test_pastelaria_save_action_uses_global_back_bar_spacing(self):
        self.assertIn('{% set back_url = ', self.pastelaria_count)
        self.assertIn('id="save-count"', self.pastelaria_count)
        self.assertIn('type="submit"', self.pastelaria_count)


if __name__ == "__main__":
    unittest.main()