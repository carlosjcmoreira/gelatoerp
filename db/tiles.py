"""Tile visibility configuration.

Provides a DB-backed mechanism to hide or show navigation tiles per module.
All tiles default to visible=True when first encountered.
"""
import logging
from db.connection import db_connection, db_retry
from db.cache import ttl_cache, ttl_cache_args, invalidate, invalidate_prefix

logger = logging.getLogger(__name__)

_LOCK_TILE_CONFIG = 202613


def _invalidate_tile_config_cache() -> None:
    """Invalidate every cached view of tile configuration after a write."""
    invalidate(
        'module_overrides',
        'all_tile_config',
    )
    invalidate_prefix('tile_config:')


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

        # One-shot rename: "Débitos Diretos" → "Custos Recorrentes ♻️"
        # Only touches rows with the exact old label so intentional user renames
        # are never overwritten.
        cursor.execute("""
            UPDATE tile_config
               SET label = 'Custos Recorrentes ♻️', updated_at = NOW()
             WHERE module = 'financeiro'
               AND tile_id = 'debitos'
               AND label IN ('Débitos Diretos', 'Débitos diretos', 'Debitos Diretos',
                             'Débitos Diretos ', 'debitos diretos')
        """)

        conn.commit()
        _invalidate_tile_config_cache()
        logger.info("run_migrations_tile_config: tile_config ready")


@db_retry
@ttl_cache_args('tile_config', ttl=300)
def _get_tile_config(module: str) -> dict:
    """Load all configuration fields for one module in a single query."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tile_id, label, visible, icon FROM tile_config WHERE module = %s",
            (module,)
        )
        rows = cursor.fetchall()
    return {
        row[0]: {
            'label': row[1] or '',
            'visible': bool(row[2]),
            'icon': row[3] or '',
        }
        for row in rows
    }


def get_tile_visibility(module: str) -> dict:
    """Return {tile_id: visible} dict for a given module.

    Tiles not in the DB are considered visible (default True).
    """
    return {tile_id: row['visible'] for tile_id, row in _get_tile_config(module).items()}


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
    _invalidate_tile_config_cache()


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
    _invalidate_tile_config_cache()


def get_tile_labels(module: str) -> dict:
    """Return {tile_id: label} for tiles that have a non-empty custom label in the given module."""
    return {
        tile_id: row['label']
        for tile_id, row in _get_tile_config(module).items()
        if row['label']
    }


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
    _invalidate_tile_config_cache()


@ttl_cache('module_overrides', ttl=300)
def _get_module_overrides() -> dict:
    """Load module labels and icons together; both live in tile_config."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT module, tile_id, label, icon
               FROM tile_config
               WHERE tile_id IN ('_module_label', '_module_icon')
                 AND (label != '' OR icon != '')"""
        )
        rows = cursor.fetchall()
    labels = {}
    icons = {}
    for module, tile_id, label, icon in rows:
        if tile_id == '_module_label' and label:
            labels[module] = label
        elif tile_id == '_module_icon' and icon:
            icons[module] = icon
    return {'labels': labels, 'icons': icons}


def get_module_labels() -> dict:
    """Return {module: label} for modules that have a custom label set.

    Module labels are stored with tile_id='_module_label' so they share the
    existing tile_config table without a schema change.  Modules not in the
    result have no custom label and should fall back to their built-in default.
    """
    return dict(_get_module_overrides()['labels'])


@ttl_cache('all_tile_config', ttl=300)
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
    return dict(_get_module_overrides()['icons'])


def get_tile_icons(module: str) -> dict:
    """Return {tile_id: icon} for tiles in a given module that have a custom icon.

    Tiles without a custom icon are omitted — caller falls back to the hardcoded default.
    Excludes the special '_module_icon' pseudo-tile (module-level icon override).
    """
    return {
        tile_id: row['icon']
        for tile_id, row in _get_tile_config(module).items()
        if row['icon'] and tile_id != '_module_icon'
    }


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
    _invalidate_tile_config_cache()
