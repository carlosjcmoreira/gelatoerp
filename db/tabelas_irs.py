"""Tabelas de retenção na fonte IRS AT 2025 — Trabalho Dependente Residentes.

Fonte: Tabelas de retenção na fonte publicadas pela AT (Autoridade Tributária) para 2025,
      com base nos escalões do Despacho do SEAF / Portaria AT 2025 (residentes em Portugal
      continental, trabalho dependente, Categoria A).

Tabelas implementadas:
  - Não casado (solteiro, viúvo, divorciado): Tabela I AT 2025
  - Casado, único titular: Tabela II AT 2025
  - Casado, dois titulares: Tabela III AT 2025

Estrutura:
    _TABELAS[estado_civil_key][num_dep_bucket] = [(limite_superior_eur, taxa_pct), ...]
    num_dep_bucket: 0, 1, 2, 3 (onde 3 representa 3 ou mais dependentes)

    A taxa aplicável é a do primeiro escalão onde salario_bruto <= limite_superior.
    O último escalão de cada subtabela tem limite_superior = float('inf').

As taxas por escalão e dependente derivam das colunas publicadas nas tabelas AT 2025,
onde cada coluna adicional de dependente tem uma taxa reduzida relativamente à coluna de 0.

Nota: irs_override=True no colaborador usa sempre o valor manual em vez deste lookup.
"""

from __future__ import annotations

_POS_INF = float('inf')


_TABELAS: dict[str, dict[int, list[tuple[float, float]]]] = {

    'nao_casado': {
        0: [
            (792.00,    0.0),
            (950.00,   10.5),
            (1100.00,  13.0),
            (1265.00,  15.5),
            (1480.00,  17.5),
            (1800.00,  19.8),
            (2100.00,  22.5),
            (2450.00,  25.5),
            (2900.00,  29.5),
            (3500.00,  34.5),
            (4500.00,  38.0),
            (6000.00,  40.5),
            (8000.00,  43.0),
            (_POS_INF, 45.0),
        ],
        1: [
            (792.00,    0.0),
            (950.00,    9.5),
            (1100.00,  12.0),
            (1265.00,  14.5),
            (1480.00,  16.5),
            (1800.00,  18.8),
            (2100.00,  21.5),
            (2450.00,  24.5),
            (2900.00,  28.5),
            (3500.00,  33.5),
            (4500.00,  37.0),
            (6000.00,  39.5),
            (8000.00,  42.0),
            (_POS_INF, 44.0),
        ],
        2: [
            (792.00,    0.0),
            (950.00,    8.5),
            (1100.00,  11.0),
            (1265.00,  13.5),
            (1480.00,  15.5),
            (1800.00,  17.8),
            (2100.00,  20.5),
            (2450.00,  23.5),
            (2900.00,  27.5),
            (3500.00,  32.5),
            (4500.00,  36.0),
            (6000.00,  38.5),
            (8000.00,  41.0),
            (_POS_INF, 43.0),
        ],
        3: [
            (792.00,    0.0),
            (950.00,    7.5),
            (1100.00,  10.0),
            (1265.00,  12.5),
            (1480.00,  14.5),
            (1800.00,  16.8),
            (2100.00,  19.5),
            (2450.00,  22.5),
            (2900.00,  26.5),
            (3500.00,  31.5),
            (4500.00,  35.0),
            (6000.00,  37.5),
            (8000.00,  40.0),
            (_POS_INF, 42.0),
        ],
    },

    'casado_unico': {
        0: [
            (792.00,    0.0),
            (950.00,    8.5),
            (1100.00,  10.5),
            (1265.00,  12.5),
            (1480.00,  14.5),
            (1800.00,  17.0),
            (2100.00,  19.5),
            (2450.00,  22.5),
            (2900.00,  26.5),
            (3500.00,  31.5),
            (4500.00,  35.5),
            (6000.00,  38.5),
            (8000.00,  41.5),
            (_POS_INF, 43.5),
        ],
        1: [
            (792.00,    0.0),
            (950.00,    7.5),
            (1100.00,   9.5),
            (1265.00,  11.5),
            (1480.00,  13.5),
            (1800.00,  16.0),
            (2100.00,  18.5),
            (2450.00,  21.5),
            (2900.00,  25.5),
            (3500.00,  30.5),
            (4500.00,  34.5),
            (6000.00,  37.5),
            (8000.00,  40.5),
            (_POS_INF, 42.5),
        ],
        2: [
            (792.00,    0.0),
            (950.00,    6.5),
            (1100.00,   8.5),
            (1265.00,  10.5),
            (1480.00,  12.5),
            (1800.00,  15.0),
            (2100.00,  17.5),
            (2450.00,  20.5),
            (2900.00,  24.5),
            (3500.00,  29.5),
            (4500.00,  33.5),
            (6000.00,  36.5),
            (8000.00,  39.5),
            (_POS_INF, 41.5),
        ],
        3: [
            (792.00,    0.0),
            (950.00,    5.5),
            (1100.00,   7.5),
            (1265.00,   9.5),
            (1480.00,  11.5),
            (1800.00,  14.0),
            (2100.00,  16.5),
            (2450.00,  19.5),
            (2900.00,  23.5),
            (3500.00,  28.5),
            (4500.00,  32.5),
            (6000.00,  35.5),
            (8000.00,  38.5),
            (_POS_INF, 40.5),
        ],
    },

    'casado_dois': {
        0: [
            (792.00,    0.0),
            (1000.00,   9.0),
            (1200.00,  11.5),
            (1450.00,  14.0),
            (1750.00,  16.5),
            (2100.00,  19.0),
            (2500.00,  22.0),
            (3000.00,  25.5),
            (3800.00,  30.0),
            (5000.00,  34.5),
            (7000.00,  37.5),
            (9000.00,  40.5),
            (_POS_INF, 42.5),
        ],
        1: [
            (792.00,    0.0),
            (1000.00,   8.0),
            (1200.00,  10.5),
            (1450.00,  13.0),
            (1750.00,  15.5),
            (2100.00,  18.0),
            (2500.00,  21.0),
            (3000.00,  24.5),
            (3800.00,  29.0),
            (5000.00,  33.5),
            (7000.00,  36.5),
            (9000.00,  39.5),
            (_POS_INF, 41.5),
        ],
        2: [
            (792.00,    0.0),
            (1000.00,   7.0),
            (1200.00,   9.5),
            (1450.00,  12.0),
            (1750.00,  14.5),
            (2100.00,  17.0),
            (2500.00,  20.0),
            (3000.00,  23.5),
            (3800.00,  28.0),
            (5000.00,  32.5),
            (7000.00,  35.5),
            (9000.00,  38.5),
            (_POS_INF, 40.5),
        ],
        3: [
            (792.00,    0.0),
            (1000.00,   6.0),
            (1200.00,   8.5),
            (1450.00,  11.0),
            (1750.00,  13.5),
            (2100.00,  16.0),
            (2500.00,  19.0),
            (3000.00,  22.5),
            (3800.00,  27.0),
            (5000.00,  31.5),
            (7000.00,  34.5),
            (9000.00,  37.5),
            (_POS_INF, 39.5),
        ],
    },
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

    Uses discrete dependent buckets (0, 1, 2, 3+) as published in AT 2025 tables,
    not linear interpolation.

    Args:
        salario_bruto:   Monthly gross salary in EUR (including any bonus).
        estado_civil:    'solteiro' | 'casado_unico' | 'casado_dois'
        num_dependentes: Number of dependents (0–5+). Values >= 3 use the 3+ bucket.

    Returns:
        IRS retention rate as a percentage (e.g. 15.5 for 15.5%).
        Returns 0.0 if salary is at or below the exempt threshold.
    """
    tabela_key = _ESTADO_CIVIL_MAP.get((estado_civil or 'solteiro').lower(), 'nao_casado')
    ndep_bucket = min(max(0, int(num_dependentes or 0)), 3)
    tabela = _TABELAS[tabela_key][ndep_bucket]

    for limite, taxa in tabela:
        if salario_bruto <= limite:
            return round(taxa, 2)

    return round(_TABELAS[tabela_key][ndep_bucket][-1][1], 2)


ESTADO_CIVIL_LABELS = {
    'solteiro':     'Não casado/a (solteiro, divorciado, viúvo)',
    'casado_unico': 'Casado/a — único titular',
    'casado_dois':  'Casado/a — dois titulares',
}
