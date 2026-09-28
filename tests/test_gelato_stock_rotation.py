import unittest
from datetime import date

from db.gelato_rotation import calculate_gelato_stock_rotation


STORES = [
    {'id': 1, 'name': 'Matosinhos', 'store_type': 'producao',
     'is_active': True},
    {'id': 2, 'name': 'Bolhão', 'store_type': 'loja', 'is_active': True},
]
RECIPES = [
    {'nome': 'EXTRA NOIR', 'nome_corrente': 'Extra Noir'},
    {'nome': 'Baunilha', 'nome_corrente': 'Baunilha'},
]


def calculate(**overrides):
    inputs = {
        'data_inicio': date(2026, 9, 1),
        'data_fim': date(2026, 9, 4),
        'stores': STORES,
        'aliases': [],
        'recipes': RECIPES,
        'stock_rows': [],
        'production_rows': [],
        'transfer_order_rows': [],
        'legacy_transfer_rows': [],
        'receipt_rows': [],
        'breakage_rows': [],
    }
    inputs.update(overrides)
    return calculate_gelato_stock_rotation(**inputs)


def cell(result, store, flavor='Extra Noir'):
    row = next(row for row in result['rows'] if row['sabor'] == flavor)
    return row['stores'][store]


class GelatoStockRotationTests(unittest.TestCase):
    def test_unassigned_manual_b2b_production_does_not_contaminate_stores(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            production_rows=[{
                'data': date(2026, 9, 1), 'store_id': None,
                'loja': ' B2B ', 'sabor': 'Baunilha',
                'quantidade_kg': 10, 'tipo': 'manual',
            }],
        )

        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertEqual(rotation['average_daily_kg'], 1)
        self.assertEqual(rotation['valid_intervals'], 1)
        self.assertNotIn('unresolved_production_identity', rotation['issues'])
        self.assertNotIn('production_store', result['unresolved'])

    def test_unknown_physical_production_identity_still_contaminates_interval(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            production_rows=[{
                'data': date(2026, 9, 2), 'store_id': None,
                'loja': 'Loja desconhecida', 'sabor': 'Baunilha',
                'quantidade_kg': 1, 'tipo': 'balança',
            }],
        )

        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('unresolved_production_identity', rotation['issues'])
        self.assertGreater(result['unresolved']['production_store'], 0)

    def test_completed_b2b_transfer_is_outbound_only(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 3), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 3, 'tipo': 'fim'},
            ],
            transfer_order_rows=[{
                'data': date(2026, 9, 2), 'sabor': 'Baunilha',
                'quantidade': 2, 'loja_destino': 'B2B',
                'loja_origem': 'Bolhão', 'status': 'confirmada',
                'confirmado_em': date(2026, 9, 2),
                'destino_tipo': 'b2b', 'store_id': None,
                'origin_store_id': None,
            }],
        )

        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertEqual(rotation['average_daily_kg'], 0)
        self.assertEqual(rotation['valid_intervals'], 1)
        self.assertEqual(rotation['excluded_intervals'], 0)

    def test_sales_store_uses_inbound_transfer_and_end_snapshots(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'loja': 'Bolhão', 'store_id': 2,
                 'sabor': 'EXTRA NOIR', 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 3), 'loja': 'Bolhão', 'store_id': 2,
                 'sabor': 'Extra Noir', 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 2), 'sabor': 'Extra Noir',
                 'quantidade': 3, 'loja_destino': 'Bolhão',
                 'loja_origem': None, 'status': 'confirmada',
                 'confirmado_em': date(2026, 9, 2),
                 'destino_tipo': 'loja', 'store_id': None},
            ],
        )

        rotation = cell(result, 'Bolhão')
        self.assertEqual(rotation['average_daily_kg'], 2.0)
        self.assertEqual(rotation['days_observed'], 2)
        self.assertEqual(rotation['valid_intervals'], 1)
        self.assertNotIn('pending_transfer', rotation['flags'])

    def test_end_snapshot_excludes_transfer_on_opening_date(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2, 'loja': 'Bolhão',
                 'sabor': 'Baunilha', 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2, 'loja': 'Bolhão',
                 'sabor': 'Baunilha', 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 1), 'sabor': 'Baunilha',
                 'quantidade': 10, 'loja_destino': 'Bolhão',
                 'loja_origem': None, 'status': 'pendente',
                 'destino_tipo': 'loja', 'store_id': None},
            ],
        )
        self.assertEqual(cell(result, 'Bolhão', 'Baunilha')['average_daily_kg'], 1)

    def test_production_store_uses_real_production_outbound_and_breakage(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Extra Noir',
                 'quantidade_kg': 10, 'tipo': 'inicio'},
                {'data': date(2026, 9, 3), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Extra Noir',
                 'quantidade_kg': 8, 'tipo': 'inicio'},
            ],
            production_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'EXTRA NOIR',
                 'quantidade_kg': 8, 'tipo': 'producao'},
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Extra Noir',
                 'quantidade_kg': 7, 'tipo': 'balança'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 2), 'sabor': 'Extra Noir',
                 'quantidade': 3, 'loja_destino': 'Bolhão',
                 'loja_origem': None, 'status': 'confirmada',
                 'confirmado_em': date(2026, 9, 2),
                 'destino_tipo': 'loja', 'store_id': None},
            ],
            breakage_rows=[
                {'data': date(2026, 9, 2), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Extra Noir',
                 'quantidade_kg': 1},
            ],
        )

        rotation = cell(result, 'Matosinhos')
        # 10 + 7 production - 3 sent - 1 breakage - 8 closing = 5 kg / 2 days
        self.assertEqual(rotation['average_daily_kg'], 2.5)
        self.assertIn('inferred_transfer_origin', rotation['flags'])

    def test_manual_only_production_invalidates_interval(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'inicio'},
                {'data': date(2026, 9, 2), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'inicio'},
            ],
            production_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 2, 'tipo': 'manual'},
            ],
        )
        rotation = cell(result, 'Matosinhos', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertEqual(rotation['excluded_intervals'], 1)
        self.assertIn('manual_only_production', rotation['issues'])

    def test_new_orders_take_precedence_over_matching_legacy_transfers(self):
        common = {
            'data': date(2026, 9, 2), 'sabor': 'Baunilha',
            'loja_destino': 'Bolhão', 'store_id': 2,
        }
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
            ],
            transfer_order_rows=[{
                **common, 'quantidade': 2, 'loja_origem': None,
                'status': 'confirmada', 'confirmado_em': date(2026, 9, 2),
                'destino_tipo': 'loja',
            }],
            legacy_transfer_rows=[{**common, 'quantidade_kg': 2}],
        )
        self.assertEqual(cell(result, 'Bolhão', 'Baunilha')['average_daily_kg'], 2)

    def test_negative_residual_is_excluded_instead_of_becoming_zero(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 2, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
            ],
        )
        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('negative_stock_residual', rotation['issues'])

    def test_average_is_weighted_by_observed_days(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 10, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 8, 'tipo': 'fim'},
                {'data': date(2026, 9, 4), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 2, 'tipo': 'fim'},
            ],
        )
        # 2 kg over one day + 6 kg over two days = 8 / 3, not mean(2, 3).
        self.assertEqual(cell(result, 'Bolhão', 'Baunilha')['average_daily_kg'], 2.667)

    def test_ambiguous_flavor_alias_is_not_merged(self):
        result = calculate(
            recipes=[
                {'nome': 'Chocolate', 'nome_corrente': 'Chocolate A'},
                {'nome': ' chocolate ', 'nome_corrente': 'Chocolate B'},
            ],
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Chocolate',
                 'quantidade_kg': 2, 'tipo': 'fim'},
            ],
        )
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['unresolved']['stock_flavor'], 1)

    def test_store_alias_resolves_only_when_unambiguous(self):
        result = calculate(
            aliases=[
                {'store_id': 2, 'alias_name': 'Baixa',
                 'alias_code': None, 'store_pos_code': None},
            ],
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': None,
                 'loja': ' baixa ', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': None,
                 'loja': 'Baixa', 'sabor': 'Baunilha',
                 'quantidade_kg': 3, 'tipo': 'fim'},
            ],
        )
        self.assertEqual(cell(result, 'Bolhão', 'Baunilha')['average_daily_kg'], 2)

    def test_missing_snapshots_explain_unknown_cell_and_coverage(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
            ],
        )
        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('insufficient_snapshots', rotation['issues'])
        self.assertEqual(result['coverage']['cells_with_value'], 0)
        self.assertEqual(result['coverage']['by_store']['Bolhão']['none'], 1)

    def test_interval_crossing_requested_start_is_not_partially_counted(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 8, 29), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 8, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
        )
        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('no_intervals_in_period', rotation['issues'])

    def test_inicio_snapshot_after_last_requested_day_closes_last_day(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 4), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'inicio'},
                {'data': date(2026, 9, 5), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 3, 'tipo': 'inicio'},
            ],
        )
        self.assertEqual(cell(
            result, 'Matosinhos', 'Baunilha'
        )['average_daily_kg'], 2)

    def test_zero_balance_record_still_overrides_csv_production(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 10, 'tipo': 'inicio'},
                {'data': date(2026, 9, 2), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 9, 'tipo': 'inicio'},
            ],
            production_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'producao'},
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 0, 'tipo': 'balança'},
            ],
        )
        self.assertEqual(cell(
            result, 'Matosinhos', 'Baunilha'
        )['average_daily_kg'], 1)

    def test_invalid_transfer_quantity_excludes_affected_intervals(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 2), 'sabor': 'Baunilha',
                 'quantidade': None, 'loja_destino': 'Bolhão',
                 'loja_origem': None, 'status': 'confirmada',
                 'confirmado_em': date(2026, 9, 2),
                 'destino_tipo': 'loja', 'store_id': None},
            ],
        )
        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('invalid_transfer_quantity', rotation['issues'])

    def test_unknown_transfer_status_is_not_used_as_physical_movement(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 2), 'sabor': 'Baunilha',
                 'quantidade': 2, 'loja_destino': 'Bolhão',
                 'loja_origem': None, 'status': 'em_transito_desconhecido',
                 'destino_tipo': 'loja', 'store_id': None},
            ],
        )
        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('unknown_transfer_status', rotation['issues'])

    def test_unknown_destination_invalidates_known_origin_interval(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'inicio'},
                {'data': date(2026, 9, 2), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'inicio'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 1), 'sabor': 'Baunilha',
                 'quantidade': 2, 'loja_destino': 'Loja antiga',
                 'loja_origem': 'Matosinhos', 'status': 'confirmada',
                 'confirmado_em': date(2026, 9, 2),
                 'destino_tipo': 'loja', 'store_id': None},
            ],
        )
        rotation = cell(result, 'Matosinhos', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('unknown_transfer_destination', rotation['issues'])

    def test_mismatched_old_and_new_transfer_totals_are_ambiguous(self):
        common = {
            'data': date(2026, 9, 2), 'sabor': 'Baunilha',
            'loja_destino': 'Bolhão', 'store_id': 2,
        }
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
            ],
            transfer_order_rows=[{
                **common, 'quantidade': 2, 'loja_origem': None,
                'status': 'confirmada', 'confirmado_em': date(2026, 9, 2),
                'destino_tipo': 'loja',
            }],
            legacy_transfer_rows=[{**common, 'quantidade_kg': 3}],
        )
        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('ambiguous_transfer_overlap', rotation['issues'])

    def test_rounding_noise_below_tolerance_becomes_zero(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': '4.99', 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': '5.00', 'tipo': 'fim'},
            ],
        )
        self.assertEqual(cell(
            result, 'Bolhão', 'Baunilha'
        )['average_daily_kg'], 0)

    def test_pending_order_is_not_a_physical_inbound_movement(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 1), 'sabor': 'Baunilha',
                 'quantidade': 3, 'loja_destino': 'Bolhão',
                 'loja_origem': 'Matosinhos', 'status': 'pendente',
                 'confirmado_em': None, 'destino_tipo': 'loja',
                 'store_id': None},
            ],
        )
        self.assertEqual(cell(result, 'Bolhão', 'Baunilha')[
            'average_daily_kg'
        ], 1)
        self.assertEqual(result['unresolved']['pending_transfer_ignored'], 1)

    def test_confirmed_inbound_uses_confirmation_date(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
                {'data': date(2026, 9, 3), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 1), 'sabor': 'Baunilha',
                 'quantidade': 2, 'loja_destino': 'Bolhão',
                 'loja_origem': 'Matosinhos', 'status': 'confirmada',
                 'confirmado_em': date(2026, 9, 3),
                 'destino_tipo': 'loja', 'store_id': None},
            ],
        )
        intervals = [
            row for row in result['intervals']
            if row['store'] == 'Bolhão' and row['sabor'] == 'Baunilha'
        ]
        self.assertEqual(intervals[0]['inbound_kg'], 0)
        self.assertEqual(intervals[1]['inbound_kg'], 2)

    def test_direct_receipt_is_inbound_without_counting_generated_receipt(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 6, 'tipo': 'fim'},
            ],
            receipt_rows=[
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade': 3, 'lote': 'fornecedor-42'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade': 9, 'lote': ''},
            ],
        )
        self.assertEqual(cell(result, 'Bolhão', 'Baunilha')[
            'average_daily_kg'
        ], 2)

    def test_transfer_exit_receipt_does_not_duplicate_confirmed_order(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 10, 'tipo': 'inicio'},
                {'data': date(2026, 9, 2), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 7, 'tipo': 'inicio'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 1), 'sabor': 'Baunilha',
                 'quantidade': 2, 'loja_destino': 'Bolhão',
                 'loja_origem': 'Matosinhos', 'status': 'confirmada',
                 'confirmado_em': date(2026, 9, 2),
                 'destino_tipo': 'loja', 'store_id': None},
            ],
            receipt_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade': -2, 'lote': 'transferencia_saida'},
            ],
        )
        # 10 - one 2 kg outbound - 7 = 1 kg consumption.
        self.assertEqual(cell(result, 'Matosinhos', 'Baunilha')[
            'average_daily_kg'
        ], 1)

    def test_pending_order_also_suppresses_its_dual_written_legacy_row(self):
        common = {
            'data': date(2026, 9, 2), 'sabor': 'Baunilha',
            'loja_destino': 'Bolhão', 'store_id': 2,
        }
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            transfer_order_rows=[{
                **common, 'quantidade': 3, 'loja_origem': None,
                'status': 'pendente', 'confirmado_em': None,
                'destino_tipo': 'loja',
            }],
            legacy_transfer_rows=[{**common, 'quantidade_kg': 3}],
        )
        self.assertEqual(cell(result, 'Bolhão', 'Baunilha')[
            'average_daily_kg'
        ], 1)

    def test_rejected_order_suppresses_its_dual_written_legacy_row(self):
        common = {
            'data': date(2026, 9, 2), 'sabor': 'Baunilha',
            'loja_destino': 'Bolhão', 'store_id': 2,
        }
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            transfer_order_rows=[{
                **common, 'quantidade': 3, 'loja_origem': None,
                'status': 'rejeitada', 'confirmado_em': date(2026, 9, 2),
                'destino_tipo': 'loja',
            }],
            legacy_transfer_rows=[{**common, 'quantidade_kg': 3}],
        )
        self.assertEqual(cell(result, 'Bolhão', 'Baunilha')[
            'average_daily_kg'
        ], 1)

    def test_receipt_with_unknown_store_contaminates_candidate_store_cells(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            receipt_rows=[
                {'data': date(2026, 9, 2), 'store_id': None,
                 'loja': 'Loja desconhecida', 'sabor': 'Baunilha',
                 'quantidade': 2, 'lote': 'direto'},
            ],
        )
        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('unresolved_receipt_identity', rotation['issues'])

    def test_production_with_unknown_flavor_contaminates_store_flavors(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'inicio'},
                {'data': date(2026, 9, 2), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'inicio'},
            ],
            production_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Nome desconhecido',
                 'quantidade_kg': 2, 'tipo': 'balança'},
            ],
        )
        rotation = cell(result, 'Matosinhos', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('unresolved_production_identity', rotation['issues'])

    def test_breakage_with_unknown_flavor_contaminates_store_flavors(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            breakage_rows=[
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Nome desconhecido',
                 'quantidade_kg': 1},
            ],
        )
        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('unresolved_breakage_identity', rotation['issues'])

    def test_snapshot_with_unknown_store_contaminates_candidate_store_cells(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': None,
                 'loja': 'Loja desconhecida', 'sabor': 'Baunilha',
                 'quantidade_kg': 3, 'tipo': 'fim'},
            ],
        )
        rotation = cell(result, 'Bolhão', 'Baunilha')
        self.assertIsNone(rotation['average_daily_kg'])
        self.assertIn('unresolved_snapshot_identity', rotation['issues'])

    def test_pending_transfer_with_unknown_flavor_does_not_contaminate_cells(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 2), 'sabor': 'Nome desconhecido',
                 'quantidade': 2, 'loja_destino': 'Bolhão',
                 'loja_origem': 'Matosinhos', 'status': 'pendente',
                 'confirmado_em': None, 'destino_tipo': 'loja',
                 'store_id': None},
            ],
        )
        self.assertEqual(cell(result, 'Bolhão', 'Baunilha')[
            'average_daily_kg'
        ], 1)

    def test_confirmed_unknown_flavor_contaminates_origin_and_destination_dates(self):
        result = calculate(
            stock_rows=[
                {'data': date(2026, 9, 1), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'inicio'},
                {'data': date(2026, 9, 2), 'store_id': 1,
                 'loja': 'Matosinhos', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'inicio'},
                {'data': date(2026, 9, 2), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 5, 'tipo': 'fim'},
                {'data': date(2026, 9, 3), 'store_id': 2,
                 'loja': 'Bolhão', 'sabor': 'Baunilha',
                 'quantidade_kg': 4, 'tipo': 'fim'},
            ],
            transfer_order_rows=[
                {'data': date(2026, 9, 1), 'sabor': 'Nome desconhecido',
                 'quantidade': 2, 'loja_destino': 'Bolhão',
                 'loja_origem': 'Matosinhos', 'status': 'confirmada',
                 'confirmado_em': date(2026, 9, 3),
                 'destino_tipo': 'loja', 'store_id': None},
            ],
        )
        for store in ('Matosinhos', 'Bolhão'):
            rotation = cell(result, store, 'Baunilha')
            self.assertIsNone(rotation['average_daily_kg'])
            self.assertIn('unresolved_transfer_identity', rotation['issues'])


if __name__ == '__main__':
    unittest.main()