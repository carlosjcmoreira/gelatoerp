"""Agente Scoopy — serviço de conversação com IA (Claude Sonnet).

Ferramentas disponíveis:
  - query_database                   : SELECT na BD → análise
  - render_chart                     : spec Plotly → gráfico inline
  - propose_write_query              : propõe DELETE/UPDATE/INSERT para confirmação
  - import_data_from_file            : pré-visualização de CSV/Excel
  - confirm_import                   : executa inserção de dados
  - save_memory                      : guarda facto na agente_memoria
  - create_tarefa                    : cria tarefa no módulo Tarefas
  - get_daily_briefing               : resumo do estado do negócio
  - analyse_weather_sales_correlation: correlação clima × vendas + sugestão amanhã
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

_API_KEY  = os.environ.get("AI_INTEGRATIONS_ANTHROPIC_API_KEY")
_BASE_URL = os.environ.get("AI_INTEGRATIONS_ANTHROPIC_BASE_URL")
_MODEL    = "claude-sonnet-4-5"
_MAX_TOKENS = 8192
_MAX_LOOP   = 6

_PROTECTED_TABLES = {
    "agente_conversas", "agente_mensagens", "agente_memoria",
    "agente_operacoes_pendentes", "users", "stores", "sessions",
    "auth_sessions", "user_store_vendas",
}

_SCHEMA_CONTEXT = """
TABELAS PRINCIPAIS DO SCOOPY (PostgreSQL):

stores(id, name, store_type, is_active, requires_eod_weighing, supports_vendas)
  — lojas: 'Niva Bolhão' (store_type='loja'), 'Niva Matosinhos' (store_type='producao')

vendas(id, data DATE, loja VARCHAR, valor_euros REAL, store_id INT, created_at)
  — resumo diário de vendas por loja

vendas_detalhe(id, data DATE, loja VARCHAR, produto VARCHAR, categoria VARCHAR,
               quantidade INT, valor_euros REAL)
  — detalhe de cada produto vendido

sales_historico(id, store_id INT, data DATE, loja VARCHAR, produto VARCHAR,
                categoria VARCHAR, quantidade INT, valor_euros REAL,
                ano_referencia INT, created_at)
  — histórico de vendas importado (anos anteriores, dados POS)

fecho_caixa(id, data DATE, loja VARCHAR, store_id INT,
            faturacao_sistema REAL, faturacao_real REAL, diferenca REAL,
            vendas_numerario REAL, vendas_multibanco REAL,
            total_caixa REAL, envelope_sobra REAL,
            total_vendas_pos REAL, dinheiro_pos REAL, cartao_pos REAL,
            ubereats_pos REAL, tpa_getnet REAL,
            colaborador VARCHAR, justificacao TEXT)
  — fecho de caixa diário por loja

producao(id, data DATE, loja VARCHAR, sabor VARCHAR, quantidade_kg REAL,
         tipo VARCHAR, store_id INT)
  — produção de gelado (tipo: 'producao', 'transferencia', etc.)

plano_producao(id, data DATE, sabor VARCHAR, stock_bolhao REAL,
               producao_necessaria REAL, producao_real_matosinhos REAL,
               producao_real_total REAL)
  — plano diário de produção de gelado

producao_pastelaria(id, data DATE, loja VARCHAR, produto VARCHAR,
                    quantidade INT, lote VARCHAR)
producao_confeitaria(id, data DATE, loja VARCHAR, produto VARCHAR,
                     quantidade INT, lote VARCHAR)

quebras(id, data DATE, loja VARCHAR, sabor VARCHAR, produto VARCHAR,
        quantidade_kg REAL, motivo VARCHAR, lote VARCHAR)
  — registo de desperdício/quebras

stock_gelado(id, data DATE, loja VARCHAR, sabor VARCHAR,
             quantidade_kg REAL, tipo VARCHAR)
  — stock de gelado (tipo: 'inicio', 'fim')

ordens_transferencia(id, data_prevista DATE, loja_destino VARCHAR,
                     estado VARCHAR, batch_id TEXT, notas TEXT,
                     created_at TIMESTAMP)
  — ordens de transferência de gelado entre produção e loja

receitas_gelado(id, nome VARCHAR, nome_corrente VARCHAR, ativo BOOLEAN,
                conta_eurokg BOOLEAN)
  — catálogo de sabores/receitas de gelado

invoices(id, supplier_name VARCHAR, invoice_number VARCHAR,
         amount_eur NUMERIC, vat_amount_eur NUMERIC,
         issue_date DATE, due_date DATE, status VARCHAR,
         category VARCHAR, centro_custo_id INT, categoria_custo_id INT)
  — faturas de fornecedores
  — status: 'draft', 'pending_review', 'scheduled', 'paid'

suppliers(id, name VARCHAR, nif VARCHAR,
          payment_terms INT, iban VARCHAR, categoria_custo_id INT)

invoice_payments(id, invoice_id INT, amount_paid NUMERIC,
                 payment_date DATE, confirmed_date DATE)

tarefas(id, nome VARCHAR, tipo VARCHAR, frequencia VARCHAR,
        dia_semana INT, dia_mes INT, utilizador_id INT,
        loja_id INT, equipa VARCHAR, ativo BOOLEAN)
  — tipo: 'unica', 'diaria', 'semanal', 'mensal'

tarefas_registos(id, tarefa_id INT, data DATE, estado VARCHAR,
                 motivo TEXT, utilizador_id INT, created_at TIMESTAMP)
  — estado: 'feito', 'nao_feito', 'parcial'

materiais(id, nome VARCHAR, unidade VARCHAR, categoria VARCHAR)
stock_materiais(id, material_id INT, loja VARCHAR, quantidade REAL)

agente_memoria(id, user_id INT, chave VARCHAR, valor TEXT,
               categoria VARCHAR, created_at, updated_at)
  — memória persistente do negócio

weather_data(id, store_id INT, fonte VARCHAR, data DATE,
             temperatura_max REAL, temperatura_min REAL,
             precipitacao_mm REAL, vento_kmh REAL, uv_index REAL,
             condicao VARCHAR, score INTEGER, updated_at)
  — dados meteorológicos históricos e de previsão por loja e fonte (IPMA, Open-Meteo, etc.)
  — score: 0-100 (0=mau tempo, 100=excelente); fontes: 'ipma', 'open_meteo', 'accuweather'

sales_forecasts(id, store_id INT, loja VARCHAR, data DATE,
                previsao_eur NUMERIC, banda_min NUMERIC, banda_max NUMERIC,
                score_meteo INTEGER, condicao_meteo VARCHAR,
                factor_yoy NUMERIC, multiplicador_meteo NUMERIC,
                base_historica NUMERIC, override_manual NUMERIC,
                override_motivo TEXT, venda_real NUMERIC, erro_real_pct NUMERIC,
                gerado_em TIMESTAMP)
  — previsões de vendas diárias (motor M2b), inclui ajuste meteorológico
  — multiplicador_meteo: ex. 1.15 = +15% por bom tempo; 0.70 = -30% por mau tempo
"""


def _get_client():
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=_API_KEY, base_url=_BASE_URL)
    except ImportError:
        return None


def _build_system_prompt(memoria: list) -> str:
    hoje = datetime.now().strftime("%A, %d de %B de %Y, %H:%M")
    mem_txt = ""
    if memoria:
        mem_txt = "\n\nMEMÓRIA DO NEGÓCIO (factos guardados anteriormente):\n"
        for m in memoria:
            mem_txt += f"  [{m['categoria']}] {m['chave']}: {m['valor']}\n"

    return f"""És o Agente Scoopy — assistente de gestão inteligente da Niva, uma empresa de gelado artesanal e pastelaria com duas lojas em Portugal: Niva Bolhão (Porto, store_type='loja') e Niva Matosinhos (unidade de produção, store_type='producao').

Data e hora actual: {hoje}

O teu papel é ser um segundo cérebro para o gestor: analisar dados, identificar tendências, sugerir melhorias operacionais e de crescimento, alertar para anomalias, e executar operações na base de dados quando pedido.

{_SCHEMA_CONTEXT}{mem_txt}

REGRAS DE COMPORTAMENTO:
1. Responde sempre em Português de Portugal.
2. Para análise de dados, usa a ferramenta query_database (SELECT only).
3. Quando os dados beneficiam de visualização, gera um gráfico com render_chart.
4. Para operações de escrita (DELETE, UPDATE, INSERT), usa SEMPRE propose_write_query — nunca executes writes directamente.
5. Quando descobrires um padrão ou insight importante sobre o negócio, guarda-o com save_memory.
6. Sê proactivo: além de responder à pergunta, sugere insights adicionais relevantes.
7. Quando um ficheiro é enviado, usa import_data_from_file para analisar antes de importar.
8. Limita os resultados de queries a 200 linhas por defeito. Usa agregações quando adequado.
9. Para comparações YoY, usa as tabelas vendas + sales_historico em conjunto.
10. Ao propor operações de escrita, descreve claramente o impacto em linguagem natural e apresenta o SQL exacto.
11. Para análise de correlação clima × vendas ou sugestões baseadas em meteorologia, usa SEMPRE a ferramenta analyse_weather_sales_correlation — nunca faças a correlação manualmente via SQL. Após receberes os dados, gera um gráfico de dispersão (temperatura vs vendas) com render_chart para cada loja e apresenta a sugestão de amanhã em destaque.
"""


# ── Definição de ferramentas ──────────────────────────────────────────────────

_TOOLS = [
    {
        "name": "query_database",
        "description": "Executa uma query SELECT na base de dados do Scoopy e devolve os resultados para análise. Só permite SELECT.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Query SQL SELECT a executar"},
                "descricao": {"type": "string", "description": "Breve descrição do que esta query faz"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "render_chart",
        "description": "Gera um gráfico Plotly para apresentar inline na conversa. Usa quando dados beneficiam de visualização.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": ["bar", "line", "scatter", "pie"], "description": "Tipo de gráfico"},
                "titulo": {"type": "string", "description": "Título do gráfico"},
                "x_label": {"type": "string", "description": "Label do eixo X"},
                "y_label": {"type": "string", "description": "Label do eixo Y"},
                "series": {
                    "type": "array",
                    "description": "Séries de dados",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nome": {"type": "string"},
                            "x": {"type": "array", "items": {}},
                            "y": {"type": "array", "items": {}},
                        },
                        "required": ["nome", "x", "y"],
                    },
                },
            },
            "required": ["tipo", "titulo", "series"],
        },
    },
    {
        "name": "propose_write_query",
        "description": "Propõe uma operação de escrita na base de dados (DELETE/UPDATE/INSERT) para confirmação pelo utilizador. O agente NUNCA executa writes directamente — usa sempre esta ferramenta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL de escrita a propor (DELETE/UPDATE/INSERT)"},
                "descricao": {"type": "string", "description": "Explicação clara do que a operação vai fazer, em linguagem natural"},
                "conversa_id": {"type": "integer", "description": "ID da conversa actual"},
            },
            "required": ["sql", "descricao", "conversa_id"],
        },
    },
    {
        "name": "import_data_from_file",
        "description": "Analisa um ficheiro CSV ou Excel enviado, devolve pré-visualização e cria uma operação pendente que o utilizador tem de confirmar manualmente antes de qualquer inserção na base de dados. Nunca insere dados directamente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "conteudo_b64": {"type": "string", "description": "Conteúdo do ficheiro em base64"},
                "nome_ficheiro": {"type": "string", "description": "Nome do ficheiro incluindo extensão"},
                "tipo_dados": {"type": "string", "enum": ["vendas_csv", "vendas_xlsx", "producao_csv", "historico_manual"], "description": "Tipo de dados detectado no ficheiro"},
                "loja": {"type": "string", "description": "Nome da loja (obrigatório para vendas_csv e producao_csv)"},
                "conversa_id": {"type": "integer", "description": "ID da conversa actual"},
            },
            "required": ["conteudo_b64", "nome_ficheiro", "tipo_dados", "conversa_id"],
        },
    },
    {
        "name": "save_memory",
        "description": "Guarda um facto ou insight sobre o negócio na memória persistente. Usa para padrões, eventos importantes, preferências do gestor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chave": {"type": "string", "description": "Identificador único para este facto (ex: 'lancamento_framboesa_2025')"},
                "valor": {"type": "string", "description": "O facto ou insight a guardar"},
                "categoria": {"type": "string", "enum": ["produto", "padrao", "evento", "preferencia", "financeiro", "operacional"], "description": "Categoria do facto"},
            },
            "required": ["chave", "valor", "categoria"],
        },
    },
    {
        "name": "create_tarefa",
        "description": "Cria uma tarefa no módulo de Tarefas do Scoopy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome": {"type": "string", "description": "Nome/descrição da tarefa"},
                "tipo": {"type": "string", "enum": ["abertura", "fecho"], "description": "Momento do dia: abertura (início do turno) ou fecho (fim do turno)"},
                "frequencia": {"type": "string", "enum": ["diaria", "semanal", "mensal"], "description": "Frequência de repetição. Omitir para tarefas únicas (sem recorrência)."},
                "equipa": {"type": "string", "description": "Equipa responsável (opcional)"},
            },
            "required": ["nome", "tipo"],
        },
    },
    {
        "name": "get_daily_briefing",
        "description": "Obtém resumo do estado actual do negócio: vendas ontem vs semana anterior, produção real vs plano de hoje, stock crítico (< 3 kg), tarefas não feitas ontem, faturas próximas a vencer, quebras.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "analyse_weather_sales_correlation",
        "description": (
            "Analisa a correlação entre dados meteorológicos (temperatura, precipitação, score de tempo) "
            "e as vendas diárias por loja. Devolve: (1) dados de dispersão temperatura vs vendas prontos "
            "para gráfico, (2) coeficiente de correlação de Pearson, (3) sugestão automática baseada na "
            "previsão meteorológica de amanhã (ex: 'Amanhã há sol e 28°C — prevejo vendas acima da média "
            "em 15%'). Integra com o motor de forecast existente: lê sales_forecasts e, se a previsão de "
            "amanhã ainda não existir, gera-a automaticamente (efeito colateral: cria rows em sales_forecasts)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "loja": {
                    "type": "string",
                    "description": "Nome da loja a analisar (ex: 'Niva Bolhão'). Se omitido, analisa todas as lojas activas.",
                },
                "dias": {
                    "type": "integer",
                    "description": "Janela histórica em dias para a correlação (padrão: 90). Máximo: 365.",
                },
            },
            "required": [],
        },
    },
]


# ── Handlers das ferramentas ──────────────────────────────────────────────────

_WRITE_KEYWORDS = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|RENAME|REPLACE|GRANT|REVOKE|EXEC|EXECUTE|COPY|CALL)\b',
    re.IGNORECASE,
)


def _handle_query_database(params: dict) -> str:
    sql = params.get("sql", "").strip()
    sql_normalized = re.sub(r'\s+', ' ', sql).upper().lstrip()

    if not sql_normalized.startswith("SELECT"):
        return json.dumps({"erro": "Só são permitidas queries SELECT. Use propose_write_query para operações de escrita."})

    if ';' in sql:
        return json.dumps({"erro": "Queries multi-statement não são permitidas. Remove qualquer ponto-e-vírgula."})

    write_match = _WRITE_KEYWORDS.search(sql)
    if write_match:
        return json.dumps({"erro": f"Palavra-chave de escrita '{write_match.group()}' encontrada. Apenas SELECT é permitido em query_database."})

    try:
        from db.connection import db_connection
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET LOCAL default_transaction_read_only = on")
            cursor.execute(sql)
            rows = cursor.fetchmany(500)
            cols = [d[0] for d in cursor.description] if cursor.description else []
            results = [dict(zip(cols, r)) for r in rows]
            for r in results:
                for k, v in r.items():
                    if isinstance(v, (date, datetime)):
                        r[k] = v.isoformat()
            return json.dumps({"colunas": cols, "linhas": results, "total": len(results)}, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.warning("query_database error: %s", exc)
        return json.dumps({"erro": str(exc)})


def _handle_render_chart(params: dict) -> str:
    return json.dumps({"chart_spec": params, "_is_chart": True}, ensure_ascii=False)


_ALLOWED_WRITE_OPS = re.compile(r'^(INSERT|UPDATE|DELETE)\b', re.IGNORECASE)
_DDL_PATTERN = re.compile(r'\b(DROP|ALTER|TRUNCATE|CREATE|RENAME|REPLACE|GRANT|REVOKE|EXEC|EXECUTE|COPY)\b', re.IGNORECASE)


def _validate_write_sql(sql: str) -> str | None:
    """Return error string if SQL is not a safe INSERT/UPDATE/DELETE, else None."""
    sql_stripped = re.sub(r'\s+', ' ', sql.strip())
    if not _ALLOWED_WRITE_OPS.match(sql_stripped):
        return "Só são permitidas operações INSERT, UPDATE ou DELETE. Usa query_database para SELECT."
    if _DDL_PATTERN.search(sql_stripped):
        return "Operações DDL (DROP, ALTER, TRUNCATE, etc.) não são permitidas."
    words = set(re.findall(r'\b\w+\b', sql.lower()))
    blocked = _PROTECTED_TABLES & words
    if blocked:
        return f"Operação recusada: não é permitido modificar as tabelas {blocked}."
    return None


def _handle_propose_write_query(params: dict, user_id: int) -> tuple[str, dict | None]:
    sql = params.get("sql", "").strip()
    descricao = params.get("descricao", "")
    conversa_id = params.get("conversa_id")

    err = _validate_write_sql(sql)
    if err:
        return json.dumps({"erro": err}), None

    sql_upper = re.sub(r'\s+', ' ', sql).upper().strip()
    impacto = "Impacto desconhecido"
    try:
        count_sql = None
        m_delete = re.match(r'DELETE\s+FROM\s+(\w+)(.*)', sql_upper, re.DOTALL)
        m_update = re.match(r'UPDATE\s+(\w+)\s+SET\s+.*?(WHERE.*)?$', sql_upper, re.DOTALL)
        if m_delete:
            table = m_delete.group(1).lower()
            where = m_delete.group(2).strip()
            count_sql = f"SELECT COUNT(*) FROM {table} {where}".strip()
        elif m_update:
            table = m_update.group(1).lower()
            where_part = ""
            m_where = re.search(r'WHERE\s+(.+)$', sql, re.IGNORECASE | re.DOTALL)
            if m_where:
                where_part = "WHERE " + m_where.group(1)
            count_sql = f"SELECT COUNT(*) FROM {table} {where_part}".strip()

        if count_sql:
            from db.connection import db_connection
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(count_sql)
                n = cursor.fetchone()[0]
                impacto = f"Esta operação afectará aproximadamente {n} registo(s)."
    except Exception as exc:
        logger.warning("propose_write_query impact estimate failed: %s", exc)

    try:
        from db.agente import criar_operacao_pendente
        op_id = criar_operacao_pendente(conversa_id, user_id, sql, descricao, impacto)
        op_info = {
            "operacao_id": op_id,
            "sql": sql,
            "descricao": descricao,
            "impacto_estimado": impacto,
        }
        return json.dumps({"ok": True, "mensagem": "Operação pendente criada. O utilizador precisa de confirmar antes de ser executada.", "operacao_id": op_id}), op_info
    except Exception as exc:
        logger.error("propose_write_query DB error: %s", exc)
        return json.dumps({"erro": str(exc)}), None


def _handle_import_data_from_file(params: dict, user_id: int) -> tuple[str, dict | None]:
    """Parse the file, show a preview, and create a pending import record.

    Returns (result_str, op_info) where op_info is the pending operation that the
    user must confirm via the UI — the model itself cannot trigger the actual insert.
    """
    b64 = params.get("conteudo_b64", "")
    nome = params.get("nome_ficheiro", "")
    tipo_dados = params.get("tipo_dados", "historico_manual")
    loja = params.get("loja", "")
    conversa_id = params.get("conversa_id")

    try:
        data = base64.b64decode(b64)
        ext = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
        df = None
        if ext in ("xlsx", "xls"):
            import pandas as pd
            df = pd.read_excel(io.BytesIO(data), engine="openpyxl", nrows=200)
        elif ext == "csv":
            import pandas as pd
            for enc in ("utf-8", "latin-1", "cp1252"):
                try:
                    df = pd.read_csv(io.BytesIO(data), encoding=enc, nrows=200)
                    break
                except Exception:
                    continue
        else:
            return json.dumps({"erro": f"Formato não suportado: {ext}. Usa CSV ou Excel (.xlsx)."}), None

        if df is None:
            return json.dumps({"erro": "Não foi possível ler o ficheiro."}), None

        preview = df.head(5).to_dict(orient="records")
        for row in preview:
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
                elif str(type(v)) in ("<class 'float'>", "<class 'numpy.float64'>") and str(v) == "nan":
                    row[k] = None

        num_linhas = len(df)
        impacto = f"Importará aproximadamente {num_linhas} linha(s) como '{tipo_dados}'" + (f" para loja '{loja}'" if loja else "")

        op_info = None
        if conversa_id:
            try:
                from db.agente import criar_operacao_pendente
                import_payload = json.dumps({
                    "__import": True,
                    "tipo_dados": tipo_dados,
                    "loja": loja,
                    "nome_ficheiro": nome,
                    "conteudo_b64": b64,
                }, ensure_ascii=False)
                op_id = criar_operacao_pendente(
                    conversa_id, user_id,
                    import_payload,
                    f"Importação de ficheiro '{nome}' ({tipo_dados})",
                    impacto,
                )
                op_info = {
                    "operacao_id": op_id,
                    "sql": import_payload,
                    "descricao": f"Importação de ficheiro '{nome}' ({tipo_dados})" + (f" — loja: {loja}" if loja else ""),
                    "impacto_estimado": impacto,
                    "tipo": "import",
                }
            except Exception as exc:
                logger.warning("import pending record creation failed: %s", exc)

        result = json.dumps({
            "ok": True,
            "colunas": list(df.columns),
            "num_linhas_total": num_linhas,
            "preview_5_linhas": preview,
            "mensagem": "Pré-visualização pronta. O utilizador precisa de confirmar antes de inserir os dados.",
            "pending_import_id": op_info["operacao_id"] if op_info else None,
        }, ensure_ascii=False, default=str)
        return result, op_info

    except Exception as exc:
        logger.warning("import_data_from_file error: %s", exc)
        return json.dumps({"erro": str(exc)}), None


def _inferir_tipo_dados_from_name(nome_ficheiro: str) -> str:
    """Infer import type from the filename."""
    nome_lower = nome_ficheiro.lower()
    if "venda" in nome_lower or "sale" in nome_lower:
        ext = nome_lower.rsplit(".", 1)[-1] if "." in nome_lower else "csv"
        return "vendas_xlsx" if ext in ("xlsx", "xls") else "vendas_csv"
    if "producao" in nome_lower or "produção" in nome_lower or "production" in nome_lower:
        return "producao_csv"
    if "historico" in nome_lower or "histórico" in nome_lower:
        return "historico_manual"
    return "historico_manual"


def _inferir_tipo_dados(cols: list) -> str:
    cols_lower = [c.lower() for c in cols]
    if any("produto" in c or "product" in c for c in cols_lower) and any("valor" in c or "amount" in c for c in cols_lower):
        return "vendas"
    if any("sabor" in c or "flavor" in c for c in cols_lower) and any("kg" in c or "peso" in c or "quant" in c for c in cols_lower):
        return "producao"
    if any("historico" in c or "ano" in c or "year" in c for c in cols_lower):
        return "historico"
    return "outro"


def execute_import_from_payload(payload: dict) -> str:
    """Execute a pending import from the stored payload dict.

    Called by the confirm endpoint — never by the model directly.
    ``payload`` is the JSON-decoded content of agente_operacoes_pendentes.sql_proposto.
    """
    if not payload.get("__import"):
        return json.dumps({"erro": "Payload inválido: não é um registo de importação."})
    return _handle_confirm_import({
        "conteudo_b64": payload.get("conteudo_b64", ""),
        "nome_ficheiro": payload.get("nome_ficheiro", ""),
        "tipo_dados": payload.get("tipo_dados", "historico_manual"),
        "loja": payload.get("loja", ""),
    })


def _handle_confirm_import(params: dict) -> str:
    b64 = params.get("conteudo_b64", "")
    nome = params.get("nome_ficheiro", "")
    tipo = params.get("tipo_dados", "")
    loja = params.get("loja", "")
    try:
        data = base64.b64decode(b64)
        buf = io.BytesIO(data)
        buf.name = nome

        if tipo == "vendas_csv":
            from flask_app.services.gestor import import_vendas_csv
            n = import_vendas_csv(buf, loja or "Bolhão")
            return json.dumps({"ok": True, "importados": n, "mensagem": f"{n} registos de vendas importados com sucesso."})

        elif tipo == "vendas_xlsx":
            from flask_app.services.gestor import import_vendas_xlsx
            import database as db
            stores = db.get_vendas_module_stores()
            loja_map = {str(s["id"]): s["name"] for s in stores}
            imported, skipped = import_vendas_xlsx(buf, loja_map)
            return json.dumps({"ok": True, "importados": imported, "ignorados": skipped, "mensagem": f"{imported} registos importados, {skipped} ignorados."})

        elif tipo == "producao_csv":
            from flask_app.services.gestor import import_producao_csv
            result = import_producao_csv(buf, loja or "Matosinhos", False)
            return json.dumps({"ok": True, "resultado": result, "mensagem": "Produção importada com sucesso."})

        elif tipo == "historico_manual":
            import pandas as pd
            ext = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
            if ext in ("xlsx", "xls"):
                df = pd.read_excel(io.BytesIO(data), engine="openpyxl")
            else:
                df = pd.read_csv(io.BytesIO(data), encoding="utf-8")

            col_map = {c.lower(): c for c in df.columns}
            date_col = col_map.get("data") or col_map.get("date")
            prod_col = col_map.get("produto") or col_map.get("product")
            qty_col  = col_map.get("quantidade") or col_map.get("qty") or col_map.get("quantity")
            val_col  = col_map.get("valor") or col_map.get("valor_euros") or col_map.get("amount")
            loja_col = col_map.get("loja") or col_map.get("store")

            if not all([date_col, prod_col, qty_col]):
                return json.dumps({"erro": "Colunas obrigatórias não encontradas: Data, Produto, Quantidade."})

            from db.connection import db_connection
            imported = 0
            with db_connection() as conn:
                cursor = conn.cursor()
                for _, row in df.iterrows():
                    try:
                        row_loja = str(row[loja_col]) if loja_col else (loja or "Bolhão")
                        cursor.execute("""
                            INSERT INTO sales_historico
                              (data, loja, produto, categoria, quantidade, valor_euros, ano_referencia)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (
                            str(row[date_col]),
                            row_loja,
                            str(row[prod_col]),
                            None,
                            int(row[qty_col]) if qty_col else 0,
                            float(row[val_col]) if val_col and val_col in row else None,
                            int(str(row[date_col])[:4]) if date_col else 0,
                        ))
                        imported += 1
                    except Exception:
                        continue
                conn.commit()
            return json.dumps({"ok": True, "importados": imported, "mensagem": f"{imported} registos importados em sales_historico."})

        else:
            return json.dumps({"erro": f"Tipo de dados desconhecido: {tipo}"})
    except Exception as exc:
        logger.error("confirm_import error: %s", exc)
        return json.dumps({"erro": str(exc)})


def _handle_save_memory(params: dict, user_id: int) -> str:
    try:
        from db.agente import guardar_memoria
        guardar_memoria(user_id, params["chave"], params["valor"], params["categoria"])
        return json.dumps({"ok": True, "mensagem": f"Facto '{params['chave']}' guardado na memória."})
    except Exception as exc:
        return json.dumps({"erro": str(exc)})


def _handle_create_tarefa(params: dict) -> str:
    try:
        from db.tarefas import create_tarefa
        nome = params["nome"]
        tipo = params.get("tipo", "abertura")
        frequencia = params.get("frequencia") or None
        equipa = params.get("equipa") or None
        tid = create_tarefa(
            nome=nome,
            tipo=tipo,
            frequencia=frequencia,
            equipa=equipa,
        )
        return json.dumps({"ok": True, "tarefa_id": tid, "mensagem": f"Tarefa '{nome}' criada com id {tid}."})
    except Exception as exc:
        return json.dumps({"erro": str(exc)})


def _handle_get_daily_briefing() -> str:
    try:
        from db.connection import db_connection
        hoje = date.today()
        ontem = date.fromordinal(hoje.toordinal() - 1)
        semana_passada = date.fromordinal(hoje.toordinal() - 7)

        result = {}
        with db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT loja, valor_euros FROM vendas WHERE data = %s", (ontem,))
            result["vendas_ontem"] = [{"loja": r[0], "valor": r[1]} for r in cursor.fetchall()]

            cursor.execute("SELECT loja, valor_euros FROM vendas WHERE data = %s", (semana_passada,))
            result["vendas_semana_passada_mesmo_dia"] = [{"loja": r[0], "valor": r[1]} for r in cursor.fetchall()]

            cursor.execute("""
                SELECT COUNT(*) FROM tarefas_registos tr
                JOIN tarefas t ON t.id = tr.tarefa_id
                WHERE tr.data = %s AND tr.estado != 'feita' AND t.ativo = TRUE
            """, (ontem,))
            row = cursor.fetchone()
            result["tarefas_nao_feitas_ontem"] = row[0] if row else 0

            cursor.execute("""
                SELECT supplier_name, amount_eur, due_date FROM invoices
                WHERE status NOT IN ('paid') AND due_date <= CURRENT_DATE + INTERVAL '7 days'
                ORDER BY due_date ASC LIMIT 10
            """)
            result["faturas_proximas"] = [
                {"fornecedor": r[0], "valor": float(r[1]), "vencimento": r[2].isoformat() if r[2] else None}
                for r in cursor.fetchall()
            ]

            cursor.execute("""
                SELECT loja, SUM(quantidade_kg) as total_quebras
                FROM quebras WHERE data = %s GROUP BY loja
            """, (ontem,))
            result["quebras_ontem"] = [{"loja": r[0], "kg": r[1]} for r in cursor.fetchall()]

            cursor.execute("""
                SELECT loja, SUM(quantidade_kg) as total_quebras
                FROM quebras
                WHERE data >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY loja
            """)
            result["quebras_30d"] = [{"loja": r[0], "kg": r[1]} for r in cursor.fetchall()]

            cursor.execute("""
                SELECT sabor,
                       producao_necessaria,
                       COALESCE(producao_real_total, 0) AS producao_real,
                       stock_bolhao
                FROM plano_producao
                WHERE data = %s
                ORDER BY sabor
            """, (hoje,))
            rows = cursor.fetchall()
            result["producao_vs_plano"] = [
                {
                    "sabor": r[0],
                    "necessario_kg": float(r[1]) if r[1] is not None else None,
                    "real_kg": float(r[2]) if r[2] is not None else None,
                    "desvio_kg": (float(r[2]) - float(r[1])) if (r[1] is not None and r[2] is not None) else None,
                    "stock_bolhao_kg": float(r[3]) if r[3] is not None else None,
                }
                for r in rows
            ]

            cursor.execute("""
                SELECT loja, sabor, quantidade_kg
                FROM stock_gelado
                WHERE data = %s AND tipo = 'fim' AND quantidade_kg < 3
                  AND is_active = TRUE
                ORDER BY quantidade_kg ASC
                LIMIT 10
            """, (ontem,))
            result["stock_critico"] = [
                {"loja": r[0], "sabor": r[1], "quantidade_kg": float(r[2])}
                for r in cursor.fetchall()
            ]

        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.error("get_daily_briefing error: %s", exc)
        return json.dumps({"erro": str(exc)})


def _pearson_correlation(xs: list, ys: list) -> float | None:
    """Compute Pearson r between two equal-length lists. Returns None if insufficient data."""
    n = len(xs)
    if n < 3:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = sum((x - mean_x) ** 2 for x in xs) ** 0.5
    den_y = sum((y - mean_y) ** 2 for y in ys) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return round(num / (den_x * den_y), 4)


def _handle_analyse_weather_sales_correlation(params: dict) -> str:
    """Handler for the analyse_weather_sales_correlation tool.

    Queries weather_data + vendas for the requested window, computes Pearson
    correlations, loads tomorrow's forecast from sales_forecasts (if available)
    and returns a structured payload the model can use to narrate insights and
    call render_chart with the scatter data.
    """
    loja_filter = params.get("loja") or None

    try:
        try:
            dias = max(1, min(int(params.get("dias") or 90), 365))
        except (TypeError, ValueError):
            dias = 90

        from db.connection import db_connection
        hoje = date.today()
        data_inicio = hoje - timedelta(days=dias)

        with db_connection() as conn:
            cursor = conn.cursor()

            # ── 1. Load stores ──────────────────────────────────────────────
            # Prefer stores that support sales; fall back to all active stores
            cursor.execute("""
                SELECT id, name FROM stores
                WHERE is_active = TRUE AND supports_vendas = TRUE
                ORDER BY name
            """)
            stores_vendas = cursor.fetchall()
            cursor.execute("SELECT id, name FROM stores WHERE is_active = TRUE ORDER BY name")
            stores_all = cursor.fetchall()
            stores_pool = stores_vendas if stores_vendas else stores_all

            loja_filter_warning = None
            if loja_filter:
                loja_lower = loja_filter.lower()
                stores = [(sid, sname) for sid, sname in stores_pool if loja_lower in sname.lower()]
                if not stores:
                    loja_filter_warning = (
                        f"Nenhuma loja activa encontrada com o nome '{loja_filter}'. "
                        f"A analisar todas as lojas com suporte a vendas: "
                        f"{', '.join(s[1] for s in stores_pool)}."
                    )
                    stores = stores_pool
            else:
                stores = stores_pool

            # Load forecast_meteo_config multipliers for heuristic fallback.
            # Guard with a savepoint in case migrations haven't run yet.
            meteo_config_by_loja: dict[str, list] = {}
            try:
                cursor.execute("SAVEPOINT sp_meteo_cfg")
                cursor.execute("""
                    SELECT loja, score_min, score_max, multiplicador
                    FROM forecast_meteo_config
                    ORDER BY loja, score_min
                """)
                for r in cursor.fetchall():
                    meteo_config_by_loja.setdefault(r[0], []).append(
                        {"score_min": r[1], "score_max": r[2], "multiplicador": float(r[3])}
                    )
                cursor.execute("RELEASE SAVEPOINT sp_meteo_cfg")
            except Exception:
                try:
                    cursor.execute("ROLLBACK TO SAVEPOINT sp_meteo_cfg")
                except Exception:
                    pass

            # ── 2. Correlation data per store ───────────────────────────────
            lojas_result = []
            for store_id, store_name in stores:
                # Daily composite weather (avg across sources per day)
                # No NULL filter on temperatura_max — allow days with only
                # precipitation/score data to contribute to those correlations.
                cursor.execute("""
                    SELECT
                        w.data,
                        ROUND(AVG(w.temperatura_max)::numeric, 1)  AS temp_max,
                        ROUND(AVG(w.precipitacao_mm)::numeric, 1)  AS precip,
                        ROUND(AVG(w.score)::numeric, 0)            AS score_medio
                    FROM weather_data w
                    WHERE w.store_id = %s
                      AND w.data >= %s AND w.data < %s
                    GROUP BY w.data
                    ORDER BY w.data
                """, (store_id, data_inicio, hoje))
                meteo_rows = {r[0]: {"temp_max": float(r[1]) if r[1] is not None else None,
                                     "precip": float(r[2]) if r[2] is not None else None,
                                     "score": int(r[3]) if r[3] is not None else None}
                              for r in cursor.fetchall()}

                # Daily sales (live vendas + historico fill-in)
                cursor.execute("""
                    SELECT data, valor_euros FROM vendas
                    WHERE loja = %s AND data >= %s AND data < %s
                    ORDER BY data
                """, (store_name, data_inicio, hoje))
                vendas_rows = {r[0]: float(r[1]) for r in cursor.fetchall()}

                cursor.execute("""
                    SELECT data, SUM(valor_euros) FROM sales_historico
                    WHERE loja = %s AND data >= %s AND data < %s
                    GROUP BY data
                """, (store_name, data_inicio, hoje))
                for r in cursor.fetchall():
                    if r[0] not in vendas_rows:
                        vendas_rows[r[0]] = float(r[1])

                # Intersect dates
                common_dates = sorted(set(meteo_rows.keys()) & set(vendas_rows.keys()))
                if not common_dates:
                    lojas_result.append({
                        "loja": store_name,
                        "aviso": "Sem dados meteorológicos e de vendas sobrepostos nesta janela.",
                        "pontos": [],
                        "correlacao_temp_vendas": None,
                        "correlacao_precip_vendas": None,
                        "correlacao_score_vendas": None,
                        "media_vendas_eur": 0.0,
                        "dias_analisados": 0,
                    })
                    continue

                # Build per-metric aligned pairs so nulls in one dimension
                # do not shift the paired sales values for another dimension.
                pontos = []
                temp_pairs: list[tuple] = []   # (temp_max, sales)
                precip_pairs: list[tuple] = [] # (precip_mm, sales)
                score_pairs: list[tuple] = []  # (score, sales)
                all_vendas: list[float] = []

                for d in common_dates:
                    m = meteo_rows[d]
                    v = vendas_rows[d]
                    pontos.append({
                        "data": d.isoformat(),
                        "temp_max": m["temp_max"],
                        "precip_mm": m["precip"],
                        "score_meteo": m["score"],
                        "vendas_eur": round(v, 2),
                    })
                    all_vendas.append(v)
                    if m["temp_max"] is not None:
                        temp_pairs.append((m["temp_max"], v))
                    if m["precip"] is not None:
                        precip_pairs.append((m["precip"], v))
                    if m["score"] is not None:
                        score_pairs.append((m["score"], v))

                r_temp = (
                    _pearson_correlation([p[0] for p in temp_pairs], [p[1] for p in temp_pairs])
                    if len(temp_pairs) >= 3 else None
                )
                r_precip = (
                    _pearson_correlation([p[0] for p in precip_pairs], [p[1] for p in precip_pairs])
                    if len(precip_pairs) >= 3 else None
                )
                r_score = (
                    _pearson_correlation([p[0] for p in score_pairs], [p[1] for p in score_pairs])
                    if len(score_pairs) >= 3 else None
                )

                # Mean sales across all common dates (used for tomorrow's suggestion)
                mean_vendas = sum(all_vendas) / len(all_vendas) if all_vendas else 0.0

                lojas_result.append({
                    "loja": store_name,
                    "dias_analisados": len(common_dates),
                    "pontos": pontos,
                    "correlacao_temp_vendas": r_temp,
                    "correlacao_precip_vendas": r_precip,
                    "correlacao_score_vendas": r_score,
                    "media_vendas_eur": round(mean_vendas, 2),
                })

            # ── 3. Tomorrow forecast + meteo ────────────────────────────────
            amanha = hoje + timedelta(days=1)
            amanha_meteo = {}
            selected_store_ids = [sid for sid, _ in stores]
            # Scope to selected stores; preferred source order: ipma > open_meteo > any
            cursor.execute("""
                SELECT
                    s.name,
                    ROUND(AVG(w.temperatura_max)::numeric, 1) AS temp_max,
                    ROUND(AVG(w.precipitacao_mm)::numeric, 1) AS precip,
                    ROUND(AVG(w.score)::numeric, 0)           AS score_medio,
                    COALESCE(
                        MAX(CASE WHEN w.fonte = 'ipma' THEN w.condicao END),
                        MAX(CASE WHEN w.fonte = 'open_meteo' THEN w.condicao END),
                        MAX(w.condicao)
                    )                                         AS condicao
                FROM weather_data w
                JOIN stores s ON s.id = w.store_id
                WHERE w.data = %s AND w.store_id = ANY(%s)
                GROUP BY s.name
            """, (amanha, selected_store_ids))
            for r in cursor.fetchall():
                amanha_meteo[r[0]] = {
                    "temp_max": float(r[1]) if r[1] is not None else None,
                    "precip_mm": float(r[2]) if r[2] is not None else None,
                    "score": int(r[3]) if r[3] is not None else None,
                    "condicao": r[4],
                }

            # Forecast from sales_forecasts table (selected stores only)
            selected_store_names = [sname for _, sname in stores]
            amanha_forecast = {}
            cursor.execute("""
                SELECT loja, previsao_eur, score_meteo, condicao_meteo, multiplicador_meteo
                FROM sales_forecasts
                WHERE data = %s AND loja = ANY(%s)
            """, (amanha, selected_store_names))
            for r in cursor.fetchall():
                amanha_forecast[r[0]] = {
                    "previsao_eur": float(r[1]) if r[1] is not None else None,
                    "score_meteo": r[2],
                    "condicao_meteo": r[3],
                    "multiplicador_meteo": float(r[4]) if r[4] is not None else None,
                }

            # If any stores lack a forecast for tomorrow, try to generate them now
            # so the suggestion uses actual model output rather than a heuristic.
            stores_missing_forecast = [
                sname for _, sname in stores
                if sname not in amanha_forecast
            ]
            stores_forecast_generated_now: list[str] = []
            if stores_missing_forecast:
                try:
                    from db.forecast import generate_forecasts
                    for sname in stores_missing_forecast:
                        fc_rows = generate_forecasts(sname, horizon_days=1)
                        if fc_rows:
                            row = fc_rows[0]
                            amanha_forecast[sname] = {
                                "previsao_eur": row.get("previsao_eur"),
                                "score_meteo": row.get("score_meteo"),
                                "condicao_meteo": row.get("condicao_meteo"),
                                "multiplicador_meteo": row.get("multiplicador_meteo"),
                            }
                            stores_forecast_generated_now.append(sname)
                except Exception as _fc_exc:
                    logger.warning("analyse_weather_sales_correlation: forecast generation skipped: %s", _fc_exc)

            # Build tomorrow suggestions per loja
            sugestoes_amanha = []
            for loja_info in lojas_result:
                lname = loja_info["loja"]
                meteo_a = amanha_meteo.get(lname, {})
                fc = amanha_forecast.get(lname, {})

                temp = meteo_a.get("temp_max")
                precip = meteo_a.get("precip_mm")
                score_meteo = meteo_a.get("score")
                if score_meteo is None:
                    score_meteo = fc.get("score_meteo")
                cond = meteo_a.get("condicao") or fc.get("condicao_meteo") or "–"
                previsao = fc.get("previsao_eur")
                mean_v = loja_info.get("media_vendas_eur", 0)

                if previsao is not None and mean_v:
                    pct_vs_media = round((previsao / mean_v - 1) * 100, 1)
                    sinal = "acima" if pct_vs_media >= 0 else "abaixo"
                    pct_abs = abs(pct_vs_media)
                    if temp is not None:
                        texto = (
                            f"Amanhã em {lname}: {cond}, {temp}°C"
                            + (f", {precip} mm de chuva" if precip is not None and precip > 0.5 else "")
                            + (f" (score meteo: {score_meteo})" if score_meteo is not None else "")
                            + f" — prevejo vendas {sinal} da média em {pct_abs}%"
                            + f" ({previsao:.0f}€ vs. média de {mean_v:.0f}€)."
                        )
                    else:
                        texto = (
                            f"Amanhã em {lname}: {cond}"
                            + (f" (score meteo: {score_meteo})" if score_meteo is not None else "")
                            + f" — prevejo vendas {sinal} da média em {pct_abs}%"
                            + f" ({previsao:.0f}€ vs. média de {mean_v:.0f}€)."
                        )
                elif score_meteo is not None and mean_v:
                    # Derive % from forecast_meteo_config for this store, or global defaults
                    cfg = meteo_config_by_loja.get(lname, [])
                    mult_heuristic = 1.0
                    for band in cfg:
                        if band["score_min"] <= score_meteo <= band["score_max"]:
                            mult_heuristic = band["multiplicador"]
                            break
                    else:
                        # Fallback to standard defaults when no config row matched
                        if score_meteo >= 85:
                            mult_heuristic = 1.15
                        elif score_meteo >= 60:
                            mult_heuristic = 1.00
                        elif score_meteo >= 30:
                            mult_heuristic = 0.85
                        else:
                            mult_heuristic = 0.70
                    pct = round((mult_heuristic - 1) * 100)
                    sinal = "acima" if pct >= 0 else "abaixo"
                    texto = (
                        f"Amanhã em {lname}: {cond}"
                        + (f", {temp}°C" if temp is not None else "")
                        + f" (score meteo: {score_meteo})"
                        + f" — estimo vendas {sinal} da média em {abs(pct)}%"
                        + f" (base histórica: {mean_v:.0f}€)."
                    )
                else:
                    texto = f"Sem previsão meteorológica para amanhã em {lname}."

                sugestoes_amanha.append({"loja": lname, "sugestao": texto,
                                         "previsao_eur": previsao, "meteo": meteo_a})

        payload = {
            "ok": True,
            "janela_dias": dias,
            "data_inicio": data_inicio.isoformat(),
            "data_fim": hoje.isoformat(),
            "lojas": lojas_result,
            "sugestoes_amanha": sugestoes_amanha,
            "instrucao_grafico": (
                "Para cada loja em 'lojas', usa render_chart com tipo='scatter' e os pontos "
                "de 'pontos' (x=temp_max, y=vendas_eur) para mostrar a dispersão temperatura vs vendas."
            ),
        }
        if stores_forecast_generated_now:
            payload["forecast_gerado_agora"] = stores_forecast_generated_now
        if loja_filter_warning:
            payload["aviso_filtro_loja"] = loja_filter_warning
        return json.dumps(payload, ensure_ascii=False, default=str)

    except Exception as exc:
        logger.error("analyse_weather_sales_correlation error: %s", exc)
        return json.dumps({"erro": str(exc)})


# ── Loop agentic principal ────────────────────────────────────────────────────

def processar_mensagem(
    user_id: int,
    conversa_id: int,
    mensagem: str,
    ficheiro_b64: str | None = None,
    ficheiro_nome: str | None = None,
    is_first_of_day: bool = False,
    historico_msgs: list | None = None,
) -> dict:
    """Processa uma mensagem do utilizador e devolve a resposta do agente.

    Returns:
        {
            "resposta": str,
            "charts": list,
            "operacao_pendente": dict | None,
        }
    """
    client = _get_client()
    if not client:
        return {"resposta": "Serviço de IA não disponível (Anthropic não configurado).", "charts": [], "operacao_pendente": None}

    from db.agente import get_memoria
    memoria = get_memoria(user_id)
    system_prompt = _build_system_prompt(memoria)

    messages = []
    if historico_msgs:
        for m in historico_msgs[-20:]:
            if m["role"] in ("user", "assistant"):
                messages.append({"role": m["role"], "content": m["content"]})

    charts: list = []
    operacao_pendente: dict | None = None

    user_content = mensagem
    if is_first_of_day:
        user_content = "[BRIEFING_MATINAL] " + mensagem

    if ficheiro_b64 and ficheiro_nome:
        tipo_dados = _inferir_tipo_dados_from_name(ficheiro_nome)
        pre_result_str, pre_op = _handle_import_data_from_file(
            {
                "conteudo_b64": ficheiro_b64,
                "nome_ficheiro": ficheiro_nome,
                "tipo_dados": tipo_dados,
                "conversa_id": conversa_id,
            },
            user_id,
        )
        try:
            pre_result = json.loads(pre_result_str)
        except Exception:
            pre_result = {}
        if pre_op and not operacao_pendente:
            operacao_pendente = pre_op
        if pre_result.get("ok"):
            file_context = (
                f"\n\n[FICHEIRO ANALISADO: {ficheiro_nome}]\n"
                f"Colunas: {', '.join(str(c) for c in pre_result.get('colunas', []))}\n"
                f"{pre_result.get('num_linhas_total', 0)} linhas detectadas.\n"
                f"Tipo inferido: {tipo_dados}\n"
                f"Primeiras linhas: {json.dumps(pre_result.get('preview_5_linhas', []), ensure_ascii=False)}\n"
                f"Operação de importação pendente criada (ID: {pre_result.get('pending_import_id')}) — aguarda confirmação do utilizador."
            )
        else:
            file_context = (
                f"\n\n[ERRO AO ANALISAR FICHEIRO: {ficheiro_nome}]\n"
                f"{pre_result.get('erro', 'Erro desconhecido')}"
            )
        user_content = user_content + file_context
    messages.append({"role": "user", "content": user_content})

    for iteration in range(_MAX_LOOP):
        try:
            response = client.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                system=system_prompt,
                tools=_TOOLS,
                messages=messages,
            )
        except Exception as exc:
            logger.error("Claude API error: %s", exc)
            return {"resposta": f"Erro ao contactar o serviço de IA: {exc}", "charts": [], "operacao_pendente": None}

        if response.stop_reason == "end_turn":
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            return {"resposta": text, "charts": charts, "operacao_pendente": operacao_pendente}

        if response.stop_reason != "tool_use":
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            return {"resposta": text, "charts": charts, "operacao_pendente": operacao_pendente}

        assistant_content = response.content
        messages.append({"role": "assistant", "content": [
            ({"type": "text", "text": b.text} if hasattr(b, "text") else
             {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            for b in assistant_content
        ]})

        tool_results = []
        for block in assistant_content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input

            if tool_name == "query_database":
                result_str = _handle_query_database(tool_input)
            elif tool_name == "render_chart":
                result_str = _handle_render_chart(tool_input)
                try:
                    spec = json.loads(result_str)
                    if spec.get("_is_chart"):
                        charts.append(spec["chart_spec"])
                except Exception:
                    pass
            elif tool_name == "propose_write_query":
                result_str, op_info = _handle_propose_write_query(tool_input, user_id)
                if op_info:
                    operacao_pendente = op_info
            elif tool_name == "import_data_from_file":
                result_str, import_op = _handle_import_data_from_file(tool_input, user_id)
                if import_op and not operacao_pendente:
                    operacao_pendente = import_op
            elif tool_name == "save_memory":
                result_str = _handle_save_memory(tool_input, user_id)
            elif tool_name == "create_tarefa":
                result_str = _handle_create_tarefa(tool_input)
            elif tool_name == "get_daily_briefing":
                result_str = _handle_get_daily_briefing()
            elif tool_name == "analyse_weather_sales_correlation":
                result_str = _handle_analyse_weather_sales_correlation(tool_input)
            else:
                result_str = json.dumps({"erro": f"Ferramenta desconhecida: {tool_name}"})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    return {"resposta": "Atingido o limite de iterações. Por favor reformula a pergunta.", "charts": charts, "operacao_pendente": operacao_pendente}
