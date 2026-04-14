"""Canonical sabor (gelado flavour) name normalisation.

All OCR output and user-entered flavour names should be passed through
`normalise_sabor()` before being written to the database.  This collapses
ALL-CAPS variants (from the handwritten production sheet), abbreviated names,
and alternative spellings into the single canonical form used throughout the
application.
"""

import logging

logger = logging.getLogger(__name__)

_SABOR_ALIASES: dict[str, str] = {
    "açai":                   "Açaí",
    "acai":                   "Açaí",
    "amendoim":               "Amendoim",
    "baunilha":               "Baunilha",
    "bonet":                  "Bonet",
    "café":                   "Café",
    "cafe":                   "Café",
    "caramelo":               "Caramelo",
    "cheesecake":             "Cheesecake",
    "choc. branco":           "Chocolate Branco",
    "choc branco":            "Chocolate Branco",
    "chocolate branco":       "Chocolate Branco",
    "coco":                   "Coco",
    "cremino":                "Cremino",
    "doce de leite":          "Doce de leite",
    "extra noir":             "Extra noir",
    "fior de leite":          "Fior di Latte",
    "fior di latte":          "Fior di Latte",
    "flor de leite":          "Fior di Latte",
    "flor di latte":          "Fior di Latte",
    "framboesa":              "Framboesa",
    "ganache":                "Ganache",
    "iogurte":                "Iogurte",
    "manga":                  "Manga",
    "maracujá":               "Maracujá",
    "maracuja":               "Maracujá",
    "noz pecan e maple":      "Noz Pecan e Maple",
    "pistachio":              "Pistacchio",
    "pistacchio":             "Pistacchio",
    "pistachio v.":           "Pistacchio V.",
    "pistacchio v.":          "Pistacchio V.",
    "pistachio vegan":        "Pistacchio V.",
    "pistacchio vegan":       "Pistacchio V.",
    "ricota, noz e mel":      "Ricota, Noz e Mel",
    "ricotta, noz e mel":     "Ricota, Noz e Mel",
    "stracciatella":          "Stracciatella",
}


def normalise_sabor(name: str) -> str:
    """Return the canonical sabor name for *name*.

    Lookup is case-insensitive.  If the name is not recognised it is returned
    unchanged (stripped of leading/trailing whitespace) so that new flavours
    are never silently dropped.
    """
    if not name:
        return name
    stripped = name.strip()
    canonical = _SABOR_ALIASES.get(stripped.lower())
    if canonical and canonical != stripped:
        logger.debug("normalise_sabor: %r → %r", stripped, canonical)
        return canonical
    return stripped
