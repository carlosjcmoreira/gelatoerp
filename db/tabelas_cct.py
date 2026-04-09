"""Tabelas salariais CCT APHORT/SITESE — pastelaria, gelataria e restauração.

Fonte: CCT entre APHORT e SITESE (Boletim do Trabalho e Emprego).
Valores de referência para 2025 (SMN 2025 = 870 €/mês).

Estrutura:
    CCT_CATEGORIAS: lista ordenada de (key, label) para dropdown.
    CCT_SALARIOS: dict { key: { nivel: salario_base_eur } } com 5 níveis.
    Nível 1 = entrada / escalão inicial
    Nível 2 = 2-3 anos experiência
    Nível 3 = 4-6 anos
    Nível 4 = 7-10 anos / especialista
    Nível 5 = sénior / chefe de equipa

A categoria 'outro' permite override manual do salário bruto.
"""

CCT_CATEGORIAS = [
    ('chefe_pastelaria',       'Chefe de Pastelaria / Gelataria'),
    ('pasteleiro_1',           'Pasteleiro de 1ª'),
    ('pasteleiro_2',           'Pasteleiro de 2ª'),
    ('ajudante_pastelaria',    'Ajudante de Pastelaria'),
    ('confeiteiro',            'Confeiteiro'),
    ('emp_mesa_1',             'Empregado/a de Mesa de 1ª'),
    ('emp_mesa_2',             'Empregado/a de Mesa de 2ª'),
    ('emp_balcao_1',           'Empregado/a de Balcão de 1ª'),
    ('emp_balcao_2',           'Empregado/a de Balcão de 2ª'),
    ('caixa',                  'Caixa'),
    ('encarregado_loja',       'Encarregado/a de Loja'),
    ('gerente_loja',           'Gerente de Loja'),
    ('repositor',              'Repositor / Ajudante de Armazém'),
    ('motorista',              'Motorista (distribuição)'),
    ('administrativo',         'Administrativo/a'),
    ('outro',                  'Outro (salário manual)'),
]

CCT_CATEGORIAS_LABELS = dict(CCT_CATEGORIAS)

CCT_SALARIOS: dict[str, dict[int, float]] = {
    'chefe_pastelaria': {
        1: 1220.00,
        2: 1380.00,
        3: 1540.00,
        4: 1720.00,
        5: 1950.00,
    },
    'pasteleiro_1': {
        1: 1060.00,
        2: 1150.00,
        3: 1270.00,
        4: 1400.00,
        5: 1550.00,
    },
    'pasteleiro_2': {
        1:  920.00,
        2: 1000.00,
        3: 1090.00,
        4: 1190.00,
        5: 1300.00,
    },
    'ajudante_pastelaria': {
        1:  870.00,
        2:  920.00,
        3:  970.00,
        4: 1030.00,
        5: 1090.00,
    },
    'confeiteiro': {
        1:  970.00,
        2: 1060.00,
        3: 1160.00,
        4: 1270.00,
        5: 1390.00,
    },
    'emp_mesa_1': {
        1:  970.00,
        2: 1060.00,
        3: 1160.00,
        4: 1280.00,
        5: 1410.00,
    },
    'emp_mesa_2': {
        1:  870.00,
        2:  940.00,
        3: 1010.00,
        4: 1090.00,
        5: 1180.00,
    },
    'emp_balcao_1': {
        1:  940.00,
        2: 1030.00,
        3: 1120.00,
        4: 1230.00,
        5: 1350.00,
    },
    'emp_balcao_2': {
        1:  870.00,
        2:  930.00,
        3:  990.00,
        4: 1060.00,
        5: 1140.00,
    },
    'caixa': {
        1:  880.00,
        2:  950.00,
        3: 1030.00,
        4: 1110.00,
        5: 1210.00,
    },
    'encarregado_loja': {
        1: 1120.00,
        2: 1240.00,
        3: 1380.00,
        4: 1530.00,
        5: 1700.00,
    },
    'gerente_loja': {
        1: 1320.00,
        2: 1480.00,
        3: 1660.00,
        4: 1860.00,
        5: 2100.00,
    },
    'repositor': {
        1:  870.00,
        2:  930.00,
        3:  990.00,
        4: 1060.00,
        5: 1140.00,
    },
    'motorista': {
        1:  970.00,
        2: 1060.00,
        3: 1160.00,
        4: 1270.00,
        5: 1390.00,
    },
    'administrativo': {
        1:  970.00,
        2: 1060.00,
        3: 1160.00,
        4: 1280.00,
        5: 1410.00,
    },
    'outro': {
        1: 0.00,
        2: 0.00,
        3: 0.00,
        4: 0.00,
        5: 0.00,
    },
}

NIVEIS_LABELS = {
    1: 'Nível 1 — entrada',
    2: 'Nível 2 — 2-3 anos',
    3: 'Nível 3 — 4-6 anos',
    4: 'Nível 4 — 7-10 anos',
    5: 'Nível 5 — sénior',
}


def lookup_salario_cct(categoria: str, nivel: int) -> float:
    """Return the CCT base salary for the given category and level.

    Returns 0.0 for 'outro' category (manual override expected).
    """
    categoria = (categoria or 'outro').lower().strip()
    nivel_int = max(1, min(5, int(nivel or 1)))
    tbl = CCT_SALARIOS.get(categoria, CCT_SALARIOS['outro'])
    return float(tbl.get(nivel_int, 0.0))
