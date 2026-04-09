"""Tabelas de retenção na fonte IRS AT 2025 — Trabalho Dependente Residentes.

Fonte: Portaria publicada pela AT (Autoridade Tributária) para 2025.
Tabelas aproximadas com base nos escalões publicados:
  - Tabela I  — Não casado (solteiro, viúvo, divorciado)
  - Tabela II — Casado, único titular
  - Tabela III — Casado, dois titulares

Estrutura de cada tabela:
    Lista de tuplos (limite_superior_eur, taxa_base_pct, parcela_abater_eur)
    O último escalão tem limite_superior = float('inf').

    Cálculo: taxa = taxa_base  (simplificado para fins de projecção de salários)
    Ajuste por dependentes: dedução fixada pelo art.º de cada portaria anual.

Nota: irs_override=True no colaborador usa sempre o valor manual em vez do lookup.
"""

from __future__ import annotations


_NEG_INF = 0.0
_POS_INF = float('inf')


_TABELAS: dict[str, list[tuple[float, float]]] = {
    'nao_casado': [
        (792.00,     0.0),
        (950.00,    10.5),
        (1100.00,   13.0),
        (1265.00,   15.5),
        (1480.00,   17.5),
        (1800.00,   19.8),
        (2100.00,   22.5),
        (2450.00,   25.5),
        (2900.00,   29.5),
        (3500.00,   34.5),
        (4500.00,   38.0),
        (6000.00,   40.5),
        (8000.00,   43.0),
        (_POS_INF,  45.0),
    ],
    'casado_unico': [
        (792.00,     0.0),
        (950.00,     8.5),
        (1100.00,   10.5),
        (1265.00,   12.5),
        (1480.00,   14.5),
        (1800.00,   17.0),
        (2100.00,   19.5),
        (2450.00,   22.5),
        (2900.00,   26.5),
        (3500.00,   31.5),
        (4500.00,   35.5),
        (6000.00,   38.5),
        (8000.00,   41.5),
        (_POS_INF,  43.5),
    ],
    'casado_dois': [
        (792.00,     0.0),
        (1000.00,    9.0),
        (1200.00,   11.5),
        (1450.00,   14.0),
        (1750.00,   16.5),
        (2100.00,   19.0),
        (2500.00,   22.0),
        (3000.00,   25.5),
        (3800.00,   30.0),
        (5000.00,   34.5),
        (7000.00,   37.5),
        (9000.00,   40.5),
        (_POS_INF,  42.5),
    ],
}

_DEDUCAO_DEPENDENTE: dict[str, float] = {
    'nao_casado':    0.55,
    'casado_unico':  0.45,
    'casado_dois':   0.40,
}

_ESTADO_CIVIL_MAP = {
    'solteiro':      'nao_casado',
    'nao_casado':    'nao_casado',
    'viuvo':         'nao_casado',
    'divorciado':    'nao_casado',
    'casado_unico':  'casado_unico',
    'casado_dois':   'casado_dois',
    'casado_1':      'casado_unico',
    'casado_2':      'casado_dois',
}


def lookup_irs(salario_bruto: float, estado_civil: str = 'solteiro',
               num_dependentes: int = 0) -> float:
    """Return the IRS withholding rate (%) for the given monthly gross salary.

    Args:
        salario_bruto:   Monthly gross salary in EUR (including any bonus).
        estado_civil:    'solteiro' | 'casado_unico' | 'casado_dois'
        num_dependentes: Number of dependents (0–5+).

    Returns:
        IRS retention rate as a percentage (e.g. 15.5 for 15.5%).
        Returns 0.0 if salary is below the threshold.
    """
    tabela_key = _ESTADO_CIVIL_MAP.get((estado_civil or 'solteiro').lower(), 'nao_casado')
    tabela = _TABELAS[tabela_key]
    deducao_por_dep = _DEDUCAO_DEPENDENTE[tabela_key]

    base_taxa = 0.0
    for limite, taxa in tabela:
        if salario_bruto <= limite:
            base_taxa = taxa
            break

    ndep = max(0, int(num_dependentes or 0))
    deducao = round(ndep * deducao_por_dep, 2)
    taxa_final = max(0.0, base_taxa - deducao)
    return round(taxa_final, 2)


ESTADO_CIVIL_LABELS = {
    'solteiro':     'Não casado/a (solteiro, divorciado, viúvo)',
    'casado_unico': 'Casado/a — único titular',
    'casado_dois':  'Casado/a — dois titulares',
}
