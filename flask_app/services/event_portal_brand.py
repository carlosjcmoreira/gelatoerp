"""Validation and storage helpers for public event portal identities."""

from __future__ import annotations

import io
import re
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename


MAX_LOGO_BYTES = 2 * 1024 * 1024
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
_LOGO_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

DEFAULT_PORTAL_BRAND = {
    "store_id": None,
    "brand_name": "Scoopy",
    "logo_filename": None,
    "primary_color": "#167C70",
    "accent_color": "#35A394",
    "background_color": "#FFF8F2",
    "text_color": "#173B38",
    "button_color": "#167C70",
    "button_text_color": "#FFFFFF",
    "form_title": "Peça o seu evento",
    "form_intro": (
        "Conte-nos o que imagina. A equipa confirma disponibilidade, "
        "logística e orçamento."
    ),
    "confirmation_message": (
        "Recebemos o seu pedido. A equipa irá confirmar disponibilidade e logística."
    ),
    "contact_text": (
        "Deixe-nos os seus contactos para podermos responder ao pedido."
    ),
    "support_phone": None,
    "min_advance_days": 0,
    "short_notice_warning": "Atenção: esta data está próxima e poderá não ser possível garantir a disponibilidade.",
    "field_labels": {},
    "visible_fields": {
        "event_name": True,
        "duration": True,
        "service_mode": True,
        "resource_preferences": True,
        "referral_source": True,
    },
    "is_default": False,
}


def default_portal_brand():
    """Return a fresh safe fallback, avoiding mutation of the module constant."""
    return {
        **DEFAULT_PORTAL_BRAND,
        "field_labels": dict(DEFAULT_PORTAL_BRAND["field_labels"]),
        "visible_fields": dict(DEFAULT_PORTAL_BRAND["visible_fields"]),
    }


def _text(value, default, limit):
    value = str(value or "").strip()
    return value[:limit] if value else default


def validate_brand_form(form):
    """Normalise editable brand values and reject unsafe colour/content input."""
    result = default_portal_brand()
    result.update({
        "brand_name": _text(form.get("brand_name"), result["brand_name"], 120),
        "primary_color": _color(form.get("primary_color"), result["primary_color"]),
        "accent_color": _color(form.get("accent_color"), result["accent_color"]),
        "background_color": _color(form.get("background_color"), result["background_color"]),
        "text_color": _color(form.get("text_color"), result["text_color"]),
        "button_color": _color(form.get("button_color"), result["button_color"]),
        "button_text_color": _color(
            form.get("button_text_color"), result["button_text_color"]
        ),
        "form_title": _text(form.get("form_title"), result["form_title"], 160),
        "form_intro": _text(form.get("form_intro"), result["form_intro"], 500),
        "confirmation_message": _text(
            form.get("confirmation_message"), result["confirmation_message"], 500
        ),
        "contact_text": _text(form.get("contact_text"), result["contact_text"], 300),
        "support_phone": _support_phone(form.get("support_phone")),
    })
    raw_days = form.get("min_advance_days", result["min_advance_days"])
    try:
        if isinstance(raw_days, str) and not raw_days.strip():
            raise ValueError
        days = int(raw_days)
    except (TypeError, ValueError):
        raise ValueError("Os dias de antecedência devem ser um número inteiro.")
    if str(raw_days).strip() != str(days) or not 0 <= days <= 3650:
        raise ValueError("Os dias de antecedência devem ser um inteiro entre 0 e 3650.")
    raw_warning = form.get("short_notice_warning", result["short_notice_warning"])
    if not str(raw_warning or "").strip():
        raise ValueError("Indique o aviso para datas de curto prazo.")
    if len(str(raw_warning).strip()) > 500:
        raise ValueError("O aviso para datas de curto prazo é demasiado longo (máximo 500 caracteres).")
    warning = _text(raw_warning, result["short_notice_warning"], 500)
    result["min_advance_days"] = days
    result["short_notice_warning"] = warning
    access_code = str(form.get("portal_access_code") or "").strip()
    if access_code and not re.fullmatch(r"[A-Za-z0-9_-]{4,32}", access_code):
        raise ValueError(
            "O código de consulta deve ter entre 4 e 32 letras, números, hífen ou sublinhado."
        )
    # A blank field deliberately means "keep the existing code"; it is never
    # prefilled or returned to a browser.
    result["portal_access_code"] = access_code or None
    if not result["brand_name"] or not result["form_title"]:
        raise ValueError("Indique o nome da marca e o título do formulário.")

    label_defaults = {
        "event_name": "Nome do evento",
        "event_type": "Tipo de evento",
        "estimated_guests": "Participantes",
        "servings_per_guest": "Bolas por pessoa",
        "duration": "Duração prevista",
        "occurrences": "Datas e local",
        "flavours": "Sabores (1 a 6)",
        "service_mode": "Como prefere o serviço?",
        "resource_preferences": "Equipamento preferido",
        "contact": "Contacto",
        "client_name": "Nome",
        "client_email": "Email",
        "client_phone": "Telefone",
        "referral_source": "Como chegou até nós?",
        "privacy": (
            "Li e aceito que os meus dados sejam usados para responder e gerir este pedido."
        ),
        "submit": "Enviar pedido",
    }
    result["field_labels"] = {
        key: _text(form.get(f"label_{key}"), default, 180)
        for key, default in label_defaults.items()
    }
    result["visible_fields"] = {
        key: form.get(f"visible_{key}") == "1"
        for key in DEFAULT_PORTAL_BRAND["visible_fields"]
    }
    # Privacy is a protected mandatory consent and is intentionally not editable
    # through the optional visibility controls.
    return result


def _color(value, default):
    value = str(value or "").strip()
    if not value:
        return default
    if not _HEX_COLOR.fullmatch(value):
        raise ValueError("As cores devem estar no formato hexadecimal, por exemplo #167C70.")
    return value.upper()


def _support_phone(value):
    """Normalize an optional support phone without accepting display markup."""
    value = str(value or "").strip()
    if not value:
        return None
    if not value.startswith("+"):
        raise ValueError("O telefone de apoio deve incluir o indicativo internacional.")
    digits = "".join(char for char in value if char.isdigit())
    if len(digits) < 7 or len(digits) > 15:
        raise ValueError("Indique um telefone de apoio válido.")
    return "+" + digits


def validate_portal_logo(upload, max_bytes=MAX_LOGO_BYTES):
    """Validate an uploaded logo by decoded image content, not only its MIME."""
    if not upload or not upload.filename:
        raise ValueError("Selecione uma imagem para o logótipo.")
    filename = secure_filename(upload.filename)
    extension = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if extension == "jpeg":
        extension = "jpg"
    if extension not in _LOGO_EXTENSIONS:
        raise ValueError("O logótipo deve ser PNG, JPG ou WebP.")
    payload = upload.read(max_bytes + 1)
    if not payload or len(payload) > max_bytes:
        raise ValueError("O logótipo deve ter no máximo 2 MB.")
    try:
        image = Image.open(io.BytesIO(payload))
        image.verify()
    except (UnidentifiedImageError, OSError):
        raise ValueError("O conteúdo do logótipo não é uma imagem válida.")
    if image.format.casefold() not in {"png", "jpeg", "webp"}:
        raise ValueError("O logótipo deve ser PNG, JPG ou WebP.")
    return {
        "payload": payload,
        "extension": extension,
        "content_type": _LOGO_MIME_TYPES[extension],
        "original_filename": filename[:255],
    }


def save_public_portal_logo(validated, static_root):
    """Store a validated, intentionally public logo under a UUID-only filename."""
    root = Path(static_root) / "uploads" / "event_portal_brands"
    root.mkdir(parents=True, exist_ok=True)
    storage_name = f"{uuid.uuid4().hex}.{validated['extension']}"
    (root / storage_name).write_bytes(validated["payload"])
    return storage_name


def save_public_event_resource_image(validated, static_root):
    """Store a validated equipment image on the application's own origin."""
    root = Path(static_root) / "uploads" / "event_resources"
    root.mkdir(parents=True, exist_ok=True)
    storage_name = f"{uuid.uuid4().hex}.{validated['extension']}"
    (root / storage_name).write_bytes(validated["payload"])
    return f"uploads/event_resources/{storage_name}"