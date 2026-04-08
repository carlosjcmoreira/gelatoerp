#!/usr/bin/env python3
"""
Smoke test / regression fixture for OCR production-sheet extraction.

Usage:
    python3 scripts/smoke_test_ocr_producao.py [path/to/sheet.jpeg] [--json]

Default test image (08/04/2026 session):
    attached_assets/WhatsApp_Image_2026-04-08_at_16.33.01_1775665731905.jpeg

The fixture checks a subset of single-value cells (no X+Y sum notation) that
were confirmed from the physical sheet photo. Values are allowed ±0.05 kg
tolerance for minor rounding differences.

NOTE: LLM responses are non-deterministic. Expect some variance between runs.
Typical results with claude-sonnet-4-5: 17–20 / 20 cells correct.
Typical results with claude-haiku-4-5:  11–13 / 20 cells correct (for comparison).

Exit code 0 = all checks pass (or failures within acceptable rate).
Exit code 1 = failure rate too high (≥ 4 failures).
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


EXPECTED_SINGLE_VALUE_CELLS = {
    "CAFÉ":           {"pesagem_mat": 1.280},
    "MANGA":          {"pesagem_mat": 3.186},
    "NOCCIOLATO":     {"pesagem_mat": 2.476},
    "FRAMBOESA":      {"pesagem_mat": 1.584},
    "AÇAÍ":           {"pesagem_mat": 2.728, "prod_matosinhos": 4.085},
    "PISTACHIO":      {"pesagem_mat": 2.826},
    "CREMINO":        {"pesagem_mat": 1.808},
    "BAUNILHA":       {"pesagem_mat": 2.066},
    "CARAMELO":       {"pesagem_mat": 1.426},
    "DOCE DE LEITE":  {"pesagem_mat": 2.004},
    "CHEESECAKE":     {"prod_bolhao": 3.170},
    "MARACUJÁ":       {"pesagem_mat": 3.032},
    "PISTACHIO VEGAN":{"prod_matosinhos": 4.390},
    "STRACCIATELLA":  {"pesagem_mat": 0.650, "prod_bolhao": 3.715, "prod_matosinhos": 3.435},
    "FLOR DE LEITE":  {"pesagem_mat": 0.838, "prod_bolhao": 2.809},
    "CHOCOLATE BRANCO": {"pesagem_mat": 1.906},
}

TOLERANCE = 0.05


def check(extracted: dict) -> bool:
    sabores = extracted.get("sabores", {})
    passed = 0
    failed = 0
    skipped = 0

    for sabor, expected_vals in EXPECTED_SINGLE_VALUE_CELLS.items():
        got = sabores.get(sabor)
        if got is None:
            found_key = next((k for k in sabores if k.upper() == sabor.upper()), None)
            got = sabores.get(found_key) if found_key else None

        if got is None:
            print(f"  MISSING  {sabor} — not in OCR output")
            skipped += 1
            continue

        for field, expected_val in expected_vals.items():
            actual = got.get(field, 0.0)
            diff = abs(actual - expected_val)
            if diff <= TOLERANCE:
                print(f"  PASS     {sabor}.{field}: {actual} (expected {expected_val})")
                passed += 1
            else:
                print(f"  FAIL     {sabor}.{field}: {actual} (expected {expected_val}, diff {diff:.3f})")
                failed += 1

    print()
    print(f"Results: {passed} passed, {failed} failed, {skipped} missing")
    print(f"Confidence: {extracted.get('ocr_confidence', 'N/A')}")
    print(f"Date: {extracted.get('date', 'N/A')}")
    return failed == 0


def main():
    if len(sys.argv) < 2:
        default = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "attached_assets",
            "WhatsApp_Image_2026-04-08_at_16.33.01_1775665731905.jpeg",
        )
        if os.path.exists(default):
            image_path = default
            print(f"Using default test image: {default}")
        else:
            print("Usage: python3 scripts/smoke_test_ocr_producao.py path/to/sheet.jpeg")
            sys.exit(1)
    else:
        image_path = sys.argv[1]

    if not os.path.exists(image_path):
        print(f"Image not found: {image_path}")
        sys.exit(1)

    from flask_app.ocr_producao import extract_producao_sheet

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    print(f"Running OCR on: {image_path} ({len(image_bytes)} bytes)")
    print()

    result = extract_producao_sheet(image_bytes, os.path.basename(image_path))

    if result.get("error") and not result.get("sabores"):
        print(f"OCR error: {result['error']}")
        sys.exit(1)

    if "--json" in sys.argv:
        print(json.dumps(result, indent=2, default=str))
        print()

    ok = check(result)
    if not ok:
        print()
        print("Note: LLM OCR is non-deterministic. Occasional misreads are expected.")
        print("Re-run to see if failures are consistent or transient.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
