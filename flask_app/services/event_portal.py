"""Small, server-side utilities for the public customer event portal."""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests
from werkzeug.utils import secure_filename

from db import eventos as event_db


_MATOSINHOS_LAT = 41.1826
_MATOSINHOS_LON = -8.6890
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_PHOTON_URL = "https://photon.komoot.io/api/"
_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
_ALLOWED_UPLOADS = {
    "pdf": ("application/pdf", b"%PDF-"),
    "jpg": ("image/jpeg", b"\xff\xd8\xff"),
    "png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    "webp": ("image/webp", b"RIFF"),
}


def _address_key(address: str) -> str:
    return " ".join((address or "").casefold().split())[:500]


def resolve_event_address(address: str) -> dict:
    """Resolve an address and round-trip road distance without blocking on failure."""
    key = _address_key(address)
    if len(key) < 8:
        return {"failed": True, "manual_review": True, "reason": "address_too_short"}
    cached = event_db.get_portal_geocode_cache(key)
    if cached:
        return {
            "latitude": cached.get("latitude"),
            "longitude": cached.get("longitude"),
            "round_trip_km": cached.get("round_trip_km"),
            "provider": cached.get("provider"),
            "failed": cached.get("failed", False),
            "manual_review": bool(cached.get("failed")),
        }
    result = {"failed": True, "manual_review": True, "reason": "unavailable"}
    try:
        response = requests.get(
            _NOMINATIM_URL,
            params={"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "pt"},
            headers={"User-Agent": os.environ.get("OSM_USER_AGENT", "ScoopyEventsPortal/1.0")},
            timeout=(2, 5),
        )
        response.raise_for_status()
        matches = response.json()
        if not matches:
            result["reason"] = "not_found"
        else:
            latitude = float(matches[0]["lat"])
            longitude = float(matches[0]["lon"])
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("invalid coordinates")
            route = requests.get(
                f"{_OSRM_URL}/{_MATOSINHOS_LON},{_MATOSINHOS_LAT};{longitude},{latitude}",
                params={"overview": "false"},
                timeout=(2, 5),
            )
            route.raise_for_status()
            routes = route.json().get("routes") or []
            if not routes or not routes[0].get("distance"):
                result["reason"] = "route_not_found"
                result.update({"latitude": latitude, "longitude": longitude, "provider": "nominatim"})
            else:
                result = {
                    "latitude": latitude,
                    "longitude": longitude,
                    "round_trip_km": round(float(routes[0]["distance"]) * 2 / 1000, 2),
                    "provider": "nominatim+osrm",
                    "failed": False,
                    "manual_review": False,
                }
    except (requests.RequestException, ValueError, TypeError, KeyError):
        # A failed lookup is saved too so a form render never becomes a repeated
        # external request. Staff can still correct the address in backoffice.
        pass
    event_db.save_portal_geocode_cache(key, result)
    return result


def resolve_event_coordinates(address: str, latitude, longitude) -> dict:
    """Calculate authoritative road distance for a signed autocomplete result."""
    try:
        latitude = float(latitude)
        longitude = float(longitude)
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError
        route = requests.get(
            f"{_OSRM_URL}/{_MATOSINHOS_LON},{_MATOSINHOS_LAT};{longitude},{latitude}",
            params={"overview": "false"},
            timeout=(2, 5),
        )
        route.raise_for_status()
        routes = route.json().get("routes") or []
        if not routes or not routes[0].get("distance"):
            raise ValueError
        return {
            "latitude": latitude,
            "longitude": longitude,
            "round_trip_km": round(float(routes[0]["distance"]) * 2 / 1000, 2),
            "provider": "photon+osrm",
            "failed": False,
            "manual_review": False,
        }
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return resolve_event_address(address)


def suggest_event_addresses(query: str) -> list[dict]:
    """Return minimal, Portugal-biased autocomplete results.

    Photon is used for interactive suggestions; failures intentionally look
    like an empty result so the form can offer manual entry.
    """
    query = " ".join((query or "").split())
    if len(query) < 3 or len(query) > 160:
        return []
    try:
        response = requests.get(
            _PHOTON_URL,
            params={"q": query, "limit": 5, "lat": _MATOSINHOS_LAT,
                    "lon": _MATOSINHOS_LON, "bbox": "-9.6,36.8,-6.0,42.2"},
            headers={"User-Agent": os.environ.get("OSM_USER_AGENT", "ScoopyEventsPortal/1.0")},
            timeout=(2, 4),
        )
        response.raise_for_status()
        output = []
        for feature in (response.json().get("features") or []):
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) != 2:
                continue
            props = feature.get("properties") or {}
            parts = [props.get(key) for key in ("name", "street", "housenumber",
                                                "postcode", "city", "district")]
            label = ", ".join(dict.fromkeys(str(part).strip() for part in parts if part))
            if label and -180 <= float(coords[0]) <= 180 and -90 <= float(coords[1]) <= 90:
                output.append({"label": label[:255], "latitude": float(coords[1]),
                               "longitude": float(coords[0])})
        return output
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return []


def validate_portal_proof(upload, max_bytes=4 * 1024 * 1024):
    """Read and validate a proof by signature, not user-supplied content type."""
    if not upload or not upload.filename:
        raise ValueError("Selecione um comprovativo em PDF ou imagem.")
    filename = secure_filename(upload.filename)
    ext = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if ext == "jpeg":
        ext = "jpg"
    if ext not in _ALLOWED_UPLOADS:
        raise ValueError("Formato não permitido. Use PDF, JPG, PNG ou WebP.")
    payload = upload.read(max_bytes + 1)
    if not payload or len(payload) > max_bytes:
        raise ValueError("O comprovativo deve ter no máximo 4 MB.")
    expected_type, magic = _ALLOWED_UPLOADS[ext]
    valid_magic = payload.startswith(magic)
    if ext == "webp":
        valid_magic = valid_magic and payload[8:12] == b"WEBP"
    if not valid_magic:
        raise ValueError("O conteúdo do ficheiro não corresponde ao formato indicado.")
    return {
        "payload": payload,
        "original_filename": filename[:255],
        "content_type": expected_type,
        "extension": ext,
    }


def save_private_portal_proof(validated: dict, upload_root: str, retention_days=180):
    """Persist a validated proof outside Flask's static tree."""
    root = Path(upload_root)
    root.mkdir(parents=True, exist_ok=True)
    storage_name = f"{uuid.uuid4().hex}.{validated['extension']}"
    path = root / storage_name
    path.write_bytes(validated["payload"])
    return {
        "storage_name": storage_name,
        "original_filename": validated["original_filename"],
        "content_type": validated["content_type"],
        "byte_size": len(validated["payload"]),
        "expires_at": datetime.now() + timedelta(days=retention_days),
    }


def cleanup_expired_portal_proofs(upload_root: str):
    """Delete files that the database has marked as expired; missing files are safe."""
    root = Path(upload_root)
    removed = 0
    for storage_name in event_db.expire_portal_files():
        path = root / storage_name
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    return removed