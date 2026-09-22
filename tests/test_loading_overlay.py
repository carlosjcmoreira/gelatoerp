import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_TEMPLATE = (
    ROOT / "flask_app" / "templates" / "base.html"
).read_text(encoding="utf-8")
STYLE_SHEET = (
    ROOT / "flask_app" / "static" / "style.css"
).read_text(encoding="utf-8")


class LoadingOverlayTemplateTests(unittest.TestCase):
    def test_overlay_exposes_accessible_progress_and_global_api(self):
        self.assertIn('id="loading-progress-track"', BASE_TEMPLATE)
        self.assertIn('role="progressbar"', BASE_TEMPLATE)
        self.assertIn('aria-valuenow="0"', BASE_TEMPLATE)
        self.assertIn('aria-live="polite"', BASE_TEMPLATE)
        self.assertIn("window.ScoopyLoading", BASE_TEMPLATE)
        self.assertIn("showEstimated", BASE_TEMPLATE)
        self.assertIn("showReal", BASE_TEMPLATE)
        self.assertIn("setProgress", BASE_TEMPLATE)

    def test_overlay_preserves_loading_exceptions_and_timeout_messages(self):
        self.assertIn("data-no-loading", BASE_TEMPLATE)
        self.assertIn("url.origin === window.location.origin", BASE_TEMPLATE)
        self.assertIn("loadingBtn.style.display = ''", BASE_TEMPLATE)
        self.assertIn("Ainda a preparar", BASE_TEMPLATE)
        self.assertIn("}, 10000);", BASE_TEMPLATE)
        self.assertIn("}, 30000);", BASE_TEMPLATE)

    def test_chunk_upload_updates_global_real_progress(self):
        self.assertIn(
            "window.ScoopyLoading.showReal('A enviar ficheiro", BASE_TEMPLATE
        )
        self.assertIn(
            "window.ScoopyLoading.setProgress(", BASE_TEMPLATE
        )
        self.assertIn("A finalizar", BASE_TEMPLATE)

    def test_overlay_supports_small_screens_and_reduced_motion(self):
        self.assertIn(".loading-progress-shell", STYLE_SHEET)
        self.assertIn("calc(100vw - 3rem)", STYLE_SHEET)
        self.assertIn("prefers-reduced-motion: reduce", STYLE_SHEET)


if __name__ == "__main__":
    unittest.main()