import io
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from db import faturas_clientes, schema
from flask_app.services import import_b2b_svc


ATTACHED_EXPORT = Path(
    "attached_assets/listagemDocumentos_2026-09-01_001638_1788218355245.xlsx"
)


class FakeCursor:
    def __init__(self, fetchone_values=None, fetchall_value=None):
        self.fetchone_values = list(fetchone_values or [])
        self.fetchall_value = list(fetchall_value or [])
        self.executions = []
        self.rowcount = 0

    def execute(self, query, params=None):
        self.executions.append((query, params))
        self.rowcount = 2

    def fetchone(self):
        return self.fetchone_values.pop(0)

    def fetchall(self):
        return self.fetchall_value


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self, *args, **kwargs):
        return self._cursor

    def commit(self):
        self.commits += 1


def connection_factory(connection):
    @contextmanager
    def _connection():
        yield connection

    return _connection


def workbook_bytes(rows):
    wb = Workbook()
    ws = wb.active
    ws.append([
        "Documento", "Número", "Data", "Data Vencimento", "Cliente",
        "Nome", "NIF", "Armazém", "Total Bruto", "Total Líquido",
        "Desconto Global", "Total Imposto", "Total", "Observações", "Anulado",
    ])
    for row in rows:
        ws.append(row)
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


class B2BCreditNoteImportTests(unittest.TestCase):
    def test_normalizes_credit_and_debit_note_labels(self):
        self.assertEqual(
            import_b2b_svc.normalize_document_type("Nota de Crédito", "NC A/2"),
            "nota_credito",
        )
        self.assertEqual(
            import_b2b_svc.normalize_document_type("Nota de Débito", "ND A/1"),
            "nota_debito",
        )
        self.assertEqual(
            import_b2b_svc.normalize_document_type("NOTA DE DEBITO", "ND A/2"),
            "nota_debito",
        )
        self.assertEqual(
            import_b2b_svc.normalize_document_type("Fatura", "FA A/63"),
            "fatura",
        )

    @unittest.skipUnless(ATTACHED_EXPORT.exists(), "uploaded B2B export not available")
    def test_uploaded_export_has_expected_net_sales_and_ignores_summary_rows(self):
        invoice_calls = []

        def save_invoice(**kwargs):
            invoice_calls.append(kwargs)
            return len(invoice_calls), True

        with ATTACHED_EXPORT.open("rb") as source, \
             patch.object(import_b2b_svc, "upsert_cliente", return_value=(10, False)), \
             patch.object(import_b2b_svc, "upsert_fatura", side_effect=save_invoice):
            result = import_b2b_svc.import_b2b_from_excel(source)

        self.assertEqual(result["faturas_importadas"], 12)
        self.assertEqual(result["erros"], [])
        credit_notes = [
            row for row in invoice_calls if row["document_type"] == "nota_credito"
        ]
        self.assertEqual(len(credit_notes), 1)
        self.assertEqual(credit_notes[0]["numero"], "NC A/2")
        self.assertAlmostEqual(credit_notes[0]["total"], 449.39)
        net_total = sum(
            -row["total"] if row["document_type"] == "nota_credito" else row["total"]
            for row in invoice_calls
        )
        self.assertAlmostEqual(net_total, 11925.22)

    def test_cancelled_document_is_not_saved(self):
        source = workbook_bytes([[
            "Nota de Crédito", "NC A/3", "2026-09-01", "2026-09-01", 20,
            "Cliente", 255609140, "Sede", 100, 100, 0, 23, 123, "", "S",
        ]])
        with patch.object(import_b2b_svc, "upsert_cliente") as client_upsert, \
             patch.object(import_b2b_svc, "upsert_fatura") as invoice_upsert:
            result = import_b2b_svc.import_b2b_from_excel(source)

        self.assertEqual(result["faturas_anuladas_ignoradas"], 1)
        client_upsert.assert_not_called()
        invoice_upsert.assert_not_called()

    def test_reimport_updates_document_type_by_invoice_number(self):
        source = workbook_bytes([[
            "Nota de Crédito", "NC A/2", "2026-08-12", "2026-08-12", 20,
            "Cliente", 255609140, "Sede", 365.36, 365.36, 0, 84.03,
            449.39, "", "N",
        ]])
        with patch.object(import_b2b_svc, "upsert_cliente", return_value=(10, False)), \
             patch.object(
                 import_b2b_svc, "upsert_fatura", return_value=(61, False)
             ) as invoice_upsert:
            result = import_b2b_svc.import_b2b_from_excel(source)

        self.assertEqual(result["faturas_duplicadas"], 1)
        self.assertEqual(
            invoice_upsert.call_args.kwargs["document_type"], "nota_credito"
        )


class B2BCreditNoteTotalsTests(unittest.TestCase):
    def test_migration_repairs_swapped_rows_before_classifying_nc_a1_and_a2(self):
        cursor = FakeCursor(fetchone_values=[(True,), None])
        connection = FakeConnection(cursor)
        with patch.object(schema, "db_connection", connection_factory(connection)):
            schema.run_migrations_faturas_clientes_document_type()

        statements = [query for query, _ in cursor.executions]
        swap_index = next(
            index for index, sql in enumerate(statements)
            if "SET armazem = document_type" in sql
        )
        classify_index = next(
            index for index, sql in enumerate(statements)
            if "SET document_type = CASE" in sql
        )
        self.assertLess(swap_index, classify_index)
        classification_sql = statements[classify_index]
        self.assertIn("LOWER(numero) LIKE 'nc %'", classification_sql)
        self.assertIn("LOWER(numero) LIKE 'nc/%'", classification_sql)
        self.assertIn("THEN 'nota_credito'", classification_sql)
        self.assertEqual(connection.commits, 1)

    def test_upsert_persists_document_type_and_warehouse_in_correct_columns(self):
        cursor = FakeCursor(fetchone_values=[(61, True)])
        connection = FakeConnection(cursor)
        with patch.object(
            faturas_clientes, "db_connection", connection_factory(connection)
        ):
            faturas_clientes.upsert_fatura(
                cliente_id=20,
                numero="NC A/2",
                data_fatura=date(2026, 8, 12),
                documento="Nota de Crédito",
                document_type="nota_credito",
                armazem="Sede",
                total=449.39,
            )

        sql, params = cursor.executions[0]
        self.assertLess(sql.index("document_type"), sql.index("armazem"))
        self.assertEqual(params[5], "nota_credito")
        self.assertEqual(params[6], "Sede")

    def test_summary_totals_use_signed_credit_note_value(self):
        cursor = FakeCursor(fetchone_values=[(1000, 500, 200, 2, 1, 1)])
        connection = FakeConnection(cursor)
        with patch.object(
            faturas_clientes, "db_connection", connection_factory(connection)
        ):
            faturas_clientes.get_summary_totals()

        sql = cursor.executions[0][0]
        self.assertIn("fc.document_type = 'nota_credito'", sql)
        self.assertIn("THEN -(fc.total)", sql)

    def test_invoice_list_exposes_signed_total_and_document_label(self):
        row = (
            61, 20, "Cliente", "255609140", "b2b", "NC A/2",
            date(2026, 8, 12), date(2026, 8, 12), "Nota de Crédito", "Sede",
            365.36, 365.36, 0, 84.03, 449.39, None, False, "nota_credito",
            "pendente", None, None, None,
        )
        cursor = FakeCursor(fetchall_value=[row])
        connection = FakeConnection(cursor)
        with patch.object(
            faturas_clientes, "db_connection", connection_factory(connection)
        ):
            invoices = faturas_clientes.list_faturas()

        self.assertEqual(invoices[0]["document_type_label"], "Nota de Crédito")
        self.assertAlmostEqual(invoices[0]["total_assinado"], -449.39)

    def test_invoice_list_can_filter_by_b2b_or_events_channel(self):
        cursor = FakeCursor(fetchall_value=[], fetchone_values=[(0,)])
        connection = FakeConnection(cursor)
        with patch.object(
            faturas_clientes, "db_connection", connection_factory(connection)
        ):
            faturas_clientes.list_faturas(cliente_tipo="eventos")
            faturas_clientes.count_faturas(cliente_tipo="b2b")

        self.assertIn("c.tipo = %s", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][-3], "eventos")
        self.assertIn("c.tipo = %s", cursor.executions[1][0])
        self.assertEqual(cursor.executions[1][1][-1], "b2b")

    def test_finance_template_displays_credit_note_and_net_page_total(self):
        template = Path(
            "flask_app/templates/financeiro/faturas_clientes.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Nota de Crédito", template)
        self.assertIn("total_assinado", template)
        self.assertIn("Total página líquido", template)


if __name__ == "__main__":
    unittest.main()