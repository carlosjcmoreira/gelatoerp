"""Tile visibility configuration.

Provides a DB-backed mechanism to hide or show navigation tiles per module.
All tiles default to visible=True when first encountered.
"""
import logging
from db.connection import db_connection, db_retry

logger = logging.getLogger(__name__)

_LOCK_TILE_CONFIG = 202613


def run_migrations_tile_config():
    """Create tile_config table and seed default visible=False tiles for producao.

    Uses advisory lock 202613 for cross-worker idempotency.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_TILE_CONFIG,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_tile_config: lock held by another worker, skipping")
            return

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tile_config (
                module VARCHAR(100) NOT NULL,
                tile_id VARCHAR(100) NOT NULL,
                label VARCHAR(255) NOT NULL DEFAULT '',
                visible BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (module, tile_id)
            )
        """)

        cursor.execute("""
            ALTER TABLE tile_config
            ADD COLUMN IF NOT EXISTS label VARCHAR(255) NOT NULL DEFAULT ''
        """)

        cursor.execute("""
            ALTER TABLE tile_config
            ADD COLUMN IF NOT EXISTS icon VARCHAR(20) NOT NULL DEFAULT ''
        """)

        _HIDDEN_DEFAULTS = [
            ('producao', 'ordem', 'Ordem de Produção'),
            ('vendas', 'fecho_historico', 'Histórico Caixa'),
        ]
        for module, tile_id, label in _HIDDEN_DEFAULTS:
            cursor.execute("""
                INSERT INTO tile_config (module, tile_id, label, visible)
                VALUES (%s, %s, %s, FALSE)
                ON CONFLICT (module, tile_id) DO NOTHING
            """, (module, tile_id, label))

        conn.commit()
        logger.info("run_migrations_tile_config: tile_config ready")


@db_retry
def get_tile_visibility(module: str) -> dict:
    """Return {tile_id: visible} dict for a given module.

    Tiles not in the DB are considered visible (default True).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tile_id, visible FROM tile_config WHERE module = %s",
            (module,)
        )
        rows = cursor.fetchall()
    return {row[0]: bool(row[1]) for row in rows}


def set_tile_visibility(module: str, tile_id: str, visible: bool, label: str = '') -> None:
    """Upsert visibility for a tile. Creates the row if it doesn't exist."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tile_config (module, tile_id, label, visible, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (module, tile_id) DO UPDATE
                SET visible = EXCLUDED.visible,
                    updated_at = NOW(),
                    label = CASE WHEN EXCLUDED.label != '' THEN EXCLUDED.label
                                 ELSE tile_config.label END
        """, (module, tile_id, label, visible))
        conn.commit()


def seed_tile_config(module: str, tiles: list) -> None:
    """Ensure all tiles for a module exist in tile_config (default visible=True).

    tiles: list of {'id': str, 'label': str} dicts.
    Existing rows are not overwritten.
    """
    if not tiles:
        return
    with db_connection() as conn:
        cursor = conn.cursor()
        for t in tiles:
            cursor.execute("""
                INSERT INTO tile_config (module, tile_id, label, visible)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (module, tile_id) DO UPDATE
                    SET label = CASE WHEN tile_config.label = '' THEN EXCLUDED.label
                                     ELSE tile_config.label END
            """, (module, t['id'], t.get('label', '')))
        conn.commit()


def get_tile_labels(module: str) -> dict:
    """Return {tile_id: label} for tiles that have a non-empty custom label in the given module."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tile_id, label FROM tile_config WHERE module = %s AND label != ''",
            (module,)
        )
        rows = cursor.fetchall()
    return {row[0]: row[1] for row in rows}


def set_tile_label(module: str, tile_id: str, label: str) -> None:
    """Update only the label for a tile, preserving its current visibility."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tile_config (module, tile_id, label, visible, updated_at)
            VALUES (%s, %s, %s, TRUE, NOW())
            ON CONFLICT (module, tile_id) DO UPDATE
                SET label = EXCLUDED.label,
                    updated_at = NOW()
        """, (module, tile_id, label.strip()))
        conn.commit()


def get_module_labels() -> dict:
    """Return {module: label} for modules that have a custom label set.

    Module labels are stored with tile_id='_module_label' so they share the
    existing tile_config table without a schema change.  Modules not in the
    result have no custom label and should fall back to their built-in default.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT module, label FROM tile_config"
            " WHERE tile_id = '_module_label' AND label != ''",
        )
        rows = cursor.fetchall()
    return {row[0]: row[1] for row in rows}


def get_all_tile_config() -> list:
    """Return all tile_config rows ordered by module, tile_id.

    Returns list of {'module', 'tile_id', 'label', 'visible', 'icon'} dicts.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT module, tile_id, label, visible, icon
            FROM tile_config
            ORDER BY module, tile_id
        """)
        rows = cursor.fetchall()
    return [
        {'module': r[0], 'tile_id': r[1], 'label': r[2], 'visible': bool(r[3]), 'icon': r[4] or ''}
        for r in rows
    ]


def get_module_icons() -> dict:
    """Return {module: icon} for modules that have a custom icon set.

    Module icons are stored with tile_id='_module_icon' in the icon column.
    Modules not in the result should fall back to their built-in default.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT module, icon FROM tile_config"
            " WHERE tile_id = '_module_icon' AND icon != ''",
        )
        rows = cursor.fetchall()
    return {row[0]: row[1] for row in rows}


def get_tile_icons(module: str) -> dict:
    """Return {tile_id: icon} for tiles in a given module that have a custom icon.

    Tiles without a custom icon are omitted — caller falls back to the hardcoded default.
    Excludes the special '_module_icon' pseudo-tile (module-level icon override).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tile_id, icon FROM tile_config"
            " WHERE module = %s AND icon != '' AND tile_id != '_module_icon'",
            (module,)
        )
        rows = cursor.fetchall()
    return {row[0]: row[1] for row in rows}


def get_all_tile_icons() -> dict:
    """Return {(module, tile_id): icon} for all tiles that have a custom icon.

    Includes both module-level icons (tile_id='_module_icon') and individual
    tile icons. Tiles without a custom icon are omitted — caller falls back to
    the hardcoded default.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT module, tile_id, icon FROM tile_config WHERE icon != ''",
        )
        rows = cursor.fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


def set_tile_icon(module: str, tile_id: str, icon: str) -> None:
    """Update only the icon for a tile or module (tile_id='_module_icon').

    Preserves existing label and visible values.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tile_config (module, tile_id, label, visible, icon, updated_at)
            VALUES (%s, %s, '', TRUE, %s, NOW())
            ON CONFLICT (module, tile_id) DO UPDATE
                SET icon = EXCLUDED.icon,
                    updated_at = NOW()
        """, (module, tile_id, icon.strip()))
        conn.commit()
