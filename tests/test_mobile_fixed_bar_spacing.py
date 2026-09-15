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

    def test_keyboard_recovery_does_not_jump_from_intermediate_fields(self):
        self.assertIn("function isLastEditableField(field, form)", self.base)
        self.assertIn(':not([type="submit"])', self.base)
        self.assertIn(":not([readonly])", self.base)
        self.assertIn(
            "if (mode !== 'always' && !isLastEditableField(e.target, scope)) return;",
            self.base,
        )
        self.assertIn(
            "if (generation !== recoveryGeneration) return;",
            self.base,
        )
        self.assertIn(
            "if (active && active !== document.body && active !== document.documentElement) return;",
            self.base,
        )
        self.assertNotIn("e.target.closest('form') || document", self.base)

    def test_keyboard_recovery_uses_the_visual_viewport_when_available(self):
        self.assertIn("if (window.visualViewport)", self.base)
        self.assertIn("window.visualViewport.offsetTop", self.base)
        self.assertIn("window.visualViewport.height", self.base)

    def test_keyboard_recovery_propagates_scroll_through_nested_containers(self):
        self.assertIn("function scrollableAncestors(element)", self.base)
        self.assertIn("var scrollRoots = scrollableAncestors(targetSubmit)", self.base)
        self.assertIn("root.scrollHeight - root.clientHeight - root.scrollTop", self.base)
        self.assertIn("root.scrollTop += consumed", self.base)
        self.assertIn(
            "window.scrollBy({ top: remaining, behavior: 'smooth' })",
            self.base,
        )

    def test_pastelaria_save_action_uses_global_back_bar_spacing(self):
        self.assertIn('{% set back_url = ', self.pastelaria_count)
        self.assertIn('id="save-count"', self.pastelaria_count)
        self.assertIn('type="submit"', self.pastelaria_count)
        self.assertIn('data-ios-submit-scroll="last-field"', self.pastelaria_count)


if __name__ == "__main__":
    unittest.main()