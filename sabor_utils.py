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
    # Açaí
    "açai":                              "Açaí",
    "acai":                              "Açaí",
    # Amendoim
    "amendoim":                          "Amendoim",
    "arachide":                          "Amendoim",
    "arachide torino":                   "Amendoim",
    # Avelã / Nocciolato
    "avelã":                             "Avelã",
    "avela":                             "Avelã",
    "nocciola":                          "Nocciolato",
    "nocciolato":                        "Nocciolato",
    "noc.":                              "Nocciolato",
    "grantorino tuorlo zuccherato":      "Nocciolato",
    # Baunilha
    "baunilha":                          "Baunilha",
    "vaniglia":                          "Baunilha",
    # Bonet
    "bonet":                             "Bonet",
    # Café
    "café":                              "Café",
    "cafe":                              "Café",
    "caffe":                             "Café",
    # Caramelo
    "caramelo":                          "Caramelo",
    # Cheesecake
    "cheesecake":                        "Cheesecake",
    # Chocolate Branco
    "choc. branco":                      "Chocolate Branco",
    "choc branco":                       "Chocolate Branco",
    "chocolate branco":                  "Chocolate Branco",
    # Coco
    "coco":                              "Coco",
    # Cremino
    "cremino":                           "Cremino",
    # Doce de Leite
    "doce de leite":                     "Doce de Leite",
    "caramello dulce de leche francisco":    "Doce de Leite",
    "caramello dulce de leche francisco(1)": "Doce de Leite",
    # Extra noir / Extra Noir Hortelã
    "extra noir":                        "Extra Noir",
    "extra noir hortelã":                "Extra Noir Hortelã",
    "all natural chocolate sorbetto":    "Extra Noir",
    "all natural chocolate hortela sorbetto": "Extra Noir Hortelã",
    # Fior di Latte (várias grafias)
    "fior de leite":                     "Fior di Latte",
    "fior di latte":                     "Fior di Latte",
    "fior di latte ":                    "Fior di Latte",
    "flor de leite":                     "Fior di Latte",
    "flor di latte":                     "Fior di Latte",
    "fiordilatte":                       "Fior di Latte",
    # Framboesa
    "framboesa":                         "Framboesa",
    # Ganache
    "ganache":                           "Ganache",
    # Gianduia
    "gianduia":                          "Gianduia",
    "gianduja":                          "Gianduia",
    # Abacaxi / Ananás
    "abacaxi":                           "Abacaxi",
    "ananás":                            "Abacaxi",
    "ananas":                            "Abacaxi",
    "all natural ananas abacaxi":        "Abacaxi",
    # Frutos Vermelhos
    "frutos vermelhos":                  "Frutos Vermelhos",
    "frutos do bosque":                  "Frutos Vermelhos",
    "all natural frutos do bosque":      "Frutos Vermelhos",
    # Iogurte
    "iogurte":                           "Iogurte",
    "yogurt":                            "Iogurte",
    # Manga
    "manga":                             "Manga",
    # Maracujá
    "maracujá":                          "Maracujá",
    "maracuja":                          "Maracujá",
    # Noz Pecan e Maple (inclui variante com espaço extra e abreviações OCR)
    "noz pecan e maple":                 "Noz Pecan e Maple",
    "noz pecan e maple ":                "Noz Pecan e Maple",
    "noc. pecan e maple":                "Noz Pecan e Maple",
    "noz pecan maple":                   "Noz Pecan e Maple",
    "noc. pecan maple":                  "Noz Pecan e Maple",
    # Pistacchio
    "pistachio":                         "Pistacchio",
    "pistacchio":                        "Pistacchio",
    "pistac.":                           "Pistacchio",
    "pistachio v.":                      "Pistacchio V.",
    "pistacchio v.":                     "Pistacchio V.",
    "pistac. v.":                        "Pistacchio V.",
    "pist. v.":                          "Pistacchio V.",
    "pistachio vegan":                   "Pistacchio V.",
    "pistacchio vegan":                  "Pistacchio V.",
    "pistacchio v":                      "Pistacchio V.",
    "pistachio v":                       "Pistacchio V.",
    # Ricota, Noz e Mel (e abreviações OCR)
    "ricota, noz e mel":                 "Ricota, Noz e Mel",
    "ricotta, noz e mel":                "Ricota, Noz e Mel",
    "ricota noz e mel":                  "Ricota, Noz e Mel",
    "ricot. noz e mel":                  "Ricota, Noz e Mel",
    "ricot. noz mel":                    "Ricota, Noz e Mel",
    # Stracciatella (regular)
    "stracciatella":                     "Stracciatella",
    # Stracciatella Ruby — todas as variantes de escrita manual / OCR
    "stracciatella ruby":                "Stracciatella Ruby",
    "stracciatella rubí":                "Stracciatella Ruby",
    "stracciatella ruby ":               "Stracciatella Ruby",
    "strac. ruby":                       "Stracciatella Ruby",
    "stroc. ruby":                       "Stracciatella Ruby",
    "strac ruby":                        "Stracciatella Ruby",
    "stroc ruby":                        "Stracciatella Ruby",
    "straciatella ruby":                 "Stracciatella Ruby",
    # Tiramisu
    "tiramisu":                          "Tiramisu",
    "tiramisù":                          "Tiramisu",
    "tiramisú":                          "Tiramisu",
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
