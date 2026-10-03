"""
Scoopy — Endpoint temporário de export da base de dados de produção.

IMPORTANTE: Este ficheiro é TEMPORÁRIO. Remover após concluir a migração de dados.

Uso:
  1. Definir a env var EXPORT_SECRET_TOKEN no Replit Secrets (valor aleatório seguro)
  2. Fazer deploy
  3. Descarregar: curl -o gelato_prod.dump "https://<url>/admin/export-db?token=<TOKEN>"
  4. Remover este ficheiro e fazer novo deploy

O token é validado em cada pedido. Usar uma só vez e eliminar de seguida.
"""
import os
import subprocess
import tempfile
import logging

from flask import Blueprint, abort, request, send_file

logger = logging.getLogger(__name__)

admin_export_bp = Blueprint("admin_export", __name__)

_EXPORT_TOKEN = os.environ.get("EXPORT_SECRET_TOKEN")
_DATABASE_URL = os.environ.get("DATABASE_URL")


@admin_export_bp.route("/admin/export-db")
def export_db():
    """Gera e serve um pg_dump da base de dados de produção.

    Requer query param ?token=<EXPORT_SECRET_TOKEN>.
    Retorna um ficheiro .dump em formato pg_restore custom.
    """
    if not _EXPORT_TOKEN:
        logger.error("EXPORT_SECRET_TOKEN não definido — endpoint desactivado")
        abort(404)

    provided = request.args.get("token", "")
    import hmac
    if not hmac.compare_digest(provided.encode(), _EXPORT_TOKEN.encode()):
        logger.warning("Tentativa de export com token inválido (IP: %s)", request.remote_addr)
        abort(403)

    if not _DATABASE_URL:
        abort(500, "DATABASE_URL não configurado")

    logger.info("Export de BD iniciado (IP: %s)", request.remote_addr)

    try:
        with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as tmp:
            dump_path = tmp.name

        result = subprocess.run(
            [
                "pg_dump",
                _DATABASE_URL,
                "--no-owner",
                "--no-privileges",
                "--format=custom",
                "--compress=9",
                "--file", dump_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            logger.error("pg_dump falhou: %s", result.stderr)
            abort(500, f"pg_dump falhou: {result.stderr[:200]}")

        logger.info("Export concluído: %s", dump_path)

        from datetime import date
        filename = f"scoopy_prod_{date.today().isoformat()}.dump"

        return send_file(
            dump_path,
            as_attachment=True,
            download_name=filename,
            mimetype="application/octet-stream",
        )

    except subprocess.TimeoutExpired:
        abort(500, "pg_dump excedeu o tempo limite (5 min)")
    except Exception as e:
        logger.exception("Erro no export: %s", e)
        abort(500, str(e))
