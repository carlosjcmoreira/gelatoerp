"""Store-facing use categories for the administrative purchasing catalogue.

Initial classifications are explicit product-name decisions. Supplier and
operational-origin labels are intentionally not used to infer article use.
"""

ARTICLE_CATEGORIES = (
    'Ingredientes e alimentos',
    'Bebidas e café',
    'Embalagens e serviço',
    'Higiene e limpeza',
    'Equipamento e utensílios',
    'Escritório e identificação',
    'Segurança e proteção',
    'Moedas e caixa',
    'Por classificar',
)

UNCATEGORIZED = 'Por classificar'

_PRODUCTS_BY_CATEGORY = {
    'Ingredientes e alimentos': (
        'Canela em pó 500g',
        'Mel (kg)',
        'Granola (pacote com 800g)',
        'Bolacha Maria (pacote com 800g)',
        'Pistacchio em pedaços',
        'Leite inteiro Gresso (l)',
        'Manteiga com sal Gresso (kg)',
        'Ricotta (kg)',
        'Cannolo (und)',
        'Chocolate 64% (Cannolo)',
        'Brioche (und.)',
        'Cerejas em calda',
        'Cacau em pó',
        'Açúcar de Confeiteiro',
    ),
    'Bebidas e café': (
        'Açúcar para Café sachê',
        'Adoçante para Café sachê',
        'Água sem gás 330ml (Unidade)',
        'Água com gás frize (Unidade)',
        'Frizze limão (Unidade)',
        'Frizze maracujá (Unidade)',
        'Frizze laranja (Unidade)',
        'Pepsi (Unidade)',
        'Pepsi zero (Unidade)',
        'Compal maga/laranja (Unidade)',
        'Compal pêssego (Unidade)',
        'Café Classico',
        'Descafeinado',
    ),
    'Embalagens e serviço': (
        'Papel vegetal',
        'Saco congelação 1 l',
        'Saco congelação 3l',
        'Saco de lixo 5l (pct - 10und)',
        'Saco de lixo 50l (pct -10 und)',
        'Saco de lixo 100l (pct - 10Und.)',
        'Película Aderente',
        'Guardanapos tipo L caixa',
        'Papel Autocorte (6 und)',
        'Papel Zigzag',
        'Papel Higiênico (12 und)',
        'Tampas capuccino',
        'Tampa Milkshake',
        'Saco de papel kraft para cones',
        'Sacos de cookies',
        'Saco plástico para pastelaria',
        'Pratinho para Nivottos',
        'Copo para Água',
        'Copos takeway kraft com tampa',
        'Agitador Café embalada indivialmente 9cm',
        'Caixa Kraft 12x10x4,5 500ml',
        'Caixa Kraft 12x10x4,5 1050ml',
        'Caixa Take Away 500g',
        'Caixa Take Away 1000g',
        'Caixa Take Away 1500g',
        'Colheres para gelado',
        'Cone pequeno 40" (caixa 480 und)',
        'Cone Médio 45" (caixa 420 und.)',
        'Copo piccolo (manga- 50und)',
        'Copo classico (manga- 50und)',
        'Copo grande (manga- 50und)',
        'Copo max (manga- 50und)',
        'Copo peso (manga- 50und)',
        'Copo Café (manga- 50und)',
        'Copo Cappucino/Branco Nivà (manga- 50und)',
        'Copo Milkshake (manga- 50und)',
        'Colheres brancas cartão Açaí',
        'Guardanapos Nivà',
        'Saco Nivà (und)',
        'Fita Niva caixas Take away (rolo)',
        'Capa para cone/Porta cones roxo',
        'Palhinha milkshake',
        'Pote takeway p/ amarena',
        'Mini Cone',
    ),
    'Higiene e limpeza': (
        'Álcool 96% 100ml',
        'Esponja und.',
        'Cheirinho casa de banho Spray',
        'Lixívia Spray',
        'Detergente louça 4l',
        'Neoblanc - Tira manchas (2l)',
        'Sabonete WC (und - 5l)',
        'Abrilhantador máquina de louça (und - 5l)',
        'Desinfetante para superfícies Ecomix',
        'Detergente máquina de lavar louça (und - 5l)',
        'Desinfetante VT 10 - Álcool 70% (und - 5l)',
        'Desinfetante WC - Solim  (und - 5l)',
        'Desinfetante chão - Bioalcool (und - 5l)',
        'Descalcificador (und - 5l)',
        'Esfregona',
        'Higienização Armazém',
        'Limpeza WC',
        'Limpeza área clientes',
        'Limpeza zona de produção',
    ),
    'Equipamento e utensílios': (
        'Espatula para fazer bola',
        'Recipiente de inox para aquecer leite',
        'Recipiente de inox para crumble',
    ),
    'Escritório e identificação': (
        'Rolo de papel térmico 80x60x11 (caixa - 10 und.)',
        'Rolo de papel térmico 57x40x11 (cartão - 10 und. )',
        'Caneta permanente preta (und)',
        'Envelope branco 110x220mm',
        'Adesivo Nivá redondo Pequeno',
        'Post it',
        'Rastreabilidade',
        'Entrada de mercadorias',
    ),
    'Segurança e proteção': (
        'Luva S (caixa)',
        'Luva M (caixa)',
        'Luva L (caixa)',
        'Pezinhos/Protetor de calçados',
        'Touca preta redinha - (100 und)',
        'Band-aid/Penso',
    ),
    'Moedas e caixa': (
        'Fecho de caixa',
        '0,10€',
        '0,20€',
        '0,50€',
        '1,0€',
        '2,0€',
    ),
}

CATEGORY_BY_PRODUCT = {
    product.strip().lower(): category
    for category, products in _PRODUCTS_BY_CATEGORY.items()
    for product in products
}


def category_for_product(product: str | None) -> str:
    """Return the reviewed initial category, or keep the article accessible."""
    return CATEGORY_BY_PRODUCT.get(
        str(product or '').strip().lower(),
        UNCATEGORIZED,
    )


def validate_article_category(category: str | None) -> str:
    category = str(category or '').strip()
    if category not in ARTICLE_CATEGORIES:
        raise ValueError('Selecione uma categoria de artigo válida.')
    return category