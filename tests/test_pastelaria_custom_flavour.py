import unittest
from unittest.mock import patch

from flask import Flask

from flask_app.routes.pastelaria import (
    PASTELARIA_CUSTOM_FLAVOUR_OPTION,
    pastelaria_bp,
)


class PastelariaCustomFlavourTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../flask_app/templates')
        self.app.secret_key = 'test'
        self.app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
        self.app.add_url_rule(
            '/test-home', endpoint='home.index', view_func=lambda: ''
        )
        self.app.add_url_rule(
            '/test-logout', endpoint='auth.logout', view_func=lambda: ''
        )
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user'] = {
                'username': 'loja',
                'role': 'vendas',
                'acesso_pastelaria': True,
            }

    def _submit(self, data, flavours=('Chocolate',), add_result=True):
        with (
            patch(
                'flask_app.routes.pastelaria.db.get_sabores_list',
                return_value=list(flavours),
            ) as get_flavours,
            patch(
                'flask_app.routes.pastelaria.db.add_produto_pastelaria',
                return_value=add_result,
            ) as add_product,
        ):
            response = self.client.post('/pastelaria/produtos', data={
                'action': 'add_produto_past',
                'novo_tipologia': 'Palito',
                **data,
            })
            with self.client.session_transaction() as session:
                messages = [
                    message for _category, message
                    in session.get('_flashes', [])
                ]
                session.pop('_flashes', None)
        return response, get_flavours, add_product, messages

    def test_accepts_a_flavour_from_the_active_list(self):
        response, get_flavours, add_product, messages = self._submit({
            'novo_sabor_past': 'Chocolate',
        })

        self.assertEqual(response.status_code, 302)
        get_flavours.assert_called_once_with()
        add_product.assert_called_once_with('Palito', 'Chocolate', '')
        self.assertIn('Produto adicionado!', messages)

    def test_keeps_no_flavour_as_a_valid_choice(self):
        response, get_flavours, add_product, messages = self._submit({
            'novo_sabor_past': '',
        })

        self.assertEqual(response.status_code, 302)
        get_flavours.assert_not_called()
        add_product.assert_called_once_with('Palito', '', '')
        self.assertIn('Produto adicionado!', messages)

    def test_saves_trimmed_custom_flavour_only_after_explicit_selection(self):
        response, get_flavours, add_product, messages = self._submit({
            'novo_sabor_past': PASTELARIA_CUSTOM_FLAVOUR_OPTION,
            'novo_sabor_past_custom': '  Pistáchio  ',
        })

        self.assertEqual(response.status_code, 302)
        get_flavours.assert_not_called()
        add_product.assert_called_once_with('Palito', 'Pistáchio', '')
        self.assertIn('Produto adicionado!', messages)

    def test_rejects_blank_custom_flavour(self):
        response, _get_flavours, add_product, messages = self._submit({
            'novo_sabor_past': PASTELARIA_CUSTOM_FLAVOUR_OPTION,
            'novo_sabor_past_custom': '   ',
        })

        self.assertEqual(response.status_code, 302)
        add_product.assert_not_called()
        self.assertIn('Indique o nome do sabor excecional.', messages)

    def test_rejects_custom_flavour_longer_than_database_column(self):
        response, _get_flavours, add_product, messages = self._submit({
            'novo_sabor_past': PASTELARIA_CUSTOM_FLAVOUR_OPTION,
            'novo_sabor_past_custom': 'a' * 256,
        })

        self.assertEqual(response.status_code, 302)
        add_product.assert_not_called()
        self.assertIn('O sabor não pode exceder 255 caracteres.', messages)

    def test_rejects_a_flavour_outside_the_list_without_exception_choice(self):
        response, get_flavours, add_product, messages = self._submit({
            'novo_sabor_past': 'Pistáchio',
        })

        self.assertEqual(response.status_code, 302)
        get_flavours.assert_called_once_with()
        add_product.assert_not_called()
        self.assertIn(
            'Escolha um sabor ativo ou a opção «Outro sabor (exceção)».',
            messages,
        )

    def test_preserves_duplicate_product_warning(self):
        response, _get_flavours, add_product, messages = self._submit(
            {'novo_sabor_past': 'Chocolate'},
            add_result=False,
        )

        self.assertEqual(response.status_code, 302)
        add_product.assert_called_once_with('Palito', 'Chocolate', '')
        self.assertIn('Produto já existe.', messages)

    def test_product_form_exposes_the_explicit_exception_option(self):
        with (
            patch(
                'flask_app.routes.pastelaria.get_pastelaria_stock_minimums',
                return_value={'stores': [], 'products': []},
            ),
            patch(
                'flask_app.routes.pastelaria.db.get_all_produtos_pastelaria',
                return_value=[],
            ),
            patch(
                'flask_app.routes.pastelaria.db.get_all_tipologias_pastelaria',
                return_value=[{'nome': 'Palito'}],
            ),
            patch(
                'flask_app.routes.pastelaria.db.get_sabores_list',
                return_value=['Chocolate'],
            ),
            patch(
                'flask_app.routes.pastelaria.db.get_all_coberturas',
                return_value=[],
            ),
            patch(
                'flask_app.routes.pastelaria.db.get_pastelaria_product_state_history',
                return_value=[],
            ),
            patch(
                'flask_app.routes.pastelaria.db.pastelaria_product_state_token',
                return_value='test-token',
            ),
        ):
            response = self.client.get('/pastelaria/produtos')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f'value="{PASTELARIA_CUSTOM_FLAVOUR_OPTION}">Outro sabor (exceção)',
            page,
        )
        self.assertIn('Nome do sabor', page)
        self.assertIn(
            'Este sabor fica apenas no produto de Pastelaria',
            page,
        )
        self.assertIn('maxlength="255"', page)


if __name__ == '__main__':
    unittest.main()