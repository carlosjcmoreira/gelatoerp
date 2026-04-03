# Backward-compatibility shim.
# All domain code lives in the db/ package (split by domain).
# Existing imports such as `from database import X` continue to work unchanged.
from db.core import *        # noqa: F401,F403 — connection pool, schema init, cache
from db.auth import *        # noqa: F401,F403 — authentication, sessions, users
from db.config import *      # noqa: F401,F403 — system config, stores, artigos, faturas migrations
from db.producao import *    # noqa: F401,F403 — gelado production, plano, area, stock, KPIs
from db.vendas import *      # noqa: F401,F403 — vendas records, pastelaria/confeitaria, KPIs
from db.financeiro import *  # noqa: F401,F403 — credit contracts, invoices, suppliers, M0b pagamentos
from db.eventos import *     # noqa: F401,F403 — eventos, leads, Google Sheets sync
from db.meteorologia import *  # noqa: F401,F403 — Fase 6: weather data, historical sales
from db.forecast import *   # noqa: F401,F403 — M2b: sales forecast engine
from db.cashflow import *     # noqa: F401,F403 — Fase 8 M3: Cash Flow 13 semanas
from db.fecho_caixa import *  # noqa: F401,F403 — Fecho de Caixa diário + reconciliação
from db.materiais import *    # noqa: F401,F403 — Stock de materiais / consumíveis
from db.centros_custo import *  # noqa: F401,F403 — Cost centers, categories, colaboradores
