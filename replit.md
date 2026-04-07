# Nivà Porto - Controlo de Rentabilidade

## Visão Geral
Aplicação Flask + Bootstrap 5 para controlo de rentabilidade das gelatarias Nivà no Porto: Matosinhos e Bolhão. Inclui gestão de produção de gelado, pastelaria e confeitaria. Interface mobile-first com suporte dark mode.

## Autenticação e Permissões
Sistema de autenticação com Flask sessions e permissões granulares por área, gerido pelo administrador.

### Utilizadores Predefinidos
- **carlosjcmoreira** (Password: itinerantaroma) - Acesso total
- **producao** (Password: producao123) - Euro/kg, Produção Gelado, Pastelaria, Confeitaria
- **vendas** (Password: vendas123) - Euro/kg, Vendas Bolhão
- **vendasmat** (Password: vendasmat123) - Euro/kg, Pastelaria, Confeitaria

### Permissões Granulares (colunas boolean na tabela users)
- **acesso_eurokg**: Acesso à página Euro/kg
- **acesso_producao**: Acesso à página Produção Gelado
- **acesso_vendas**: Acesso à página Vendas Bolhão
- **acesso_pastelaria**: Acesso à página Produção Pastelaria
- **acesso_confeitaria**: Acesso à página Produção Confeitaria
- **acesso_administrativo**: Acesso aos módulos Compras e Logística
- **acesso_gestor**: Acesso total (Gestor, filtros de loja, consumo teórico, configurações)

### Gestão de Utilizadores (Configurações do Gestor)
- Tabela editável com checkboxes por área
- Criar utilizador (username + password)
- Alterar password de utilizadores existentes
- Ativar/desativar utilizadores via checkbox na tabela

## Arquitetura

### Stack
- **Backend:** Flask (Python) com Jinja2 templates; servido por gunicorn 25.1.0 (4 workers sync)
- **Frontend:** Bootstrap 5 (mobile-first) + vanilla JS
- **Charts:** Plotly (renderizado client-side)
- **Auth:** Flask sessions com secure cookies
- **DB:** PostgreSQL via `db/` package (psycopg2); `database.py` é um shim de compatibilidade
- **Cache:** `db/cache.py` — TTL in-process por worker (sem dependências externas)
- **Serviços:** `flask_app/services/` — camada entre rotas e DB (KPI, sessão, produção)
- **Servidor:** gunicorn `gunicorn.conf.py` — 4 workers sync, scheduler apenas no worker 1
- **OCR:** Anthropic Vision (claude-haiku-4-5) via Replit AI Integrations
- **OneDrive:** Microsoft Graph API (opcional, requer AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID)

### Módulo de Faturas (Fase 4 — M0a)
- **Tabelas DB:** `invoices`, `suppliers`
- **OCR:** Upload PDF → extracção de campos (fornecedor, NIF, valor, IVA, datas) via Anthropic Vision com scores de confiança por campo
- **OneDrive:** Arquivo automático em `Faturas/{Ano}/{Mês}/{Subpasta}/{ficheiro}.pdf` via Graph API
- **Estados:** pending_review → scheduled → paid | cancelled | overdue
- **Importação em batch** via Excel (.xlsx/.xls) com mapeamento de colunas
- **Subpastas OneDrive:** Produção, Bolhão, Matosinhos, FP, Gestão, Distribuição, Eventos

### Estrutura de Ficheiros
```
flask_app/
├── app.py                    # Flask app factory, config, auth decorators
├── ocr_invoice.py            # OCR via Anthropic Vision (extracção de campos de PDF)
├── onedrive_archive.py       # Arquivo no OneDrive via Microsoft Graph API
├── routes/
│   ├── auth.py               # Login/logout
│   ├── home.py               # Home navigation grid
│   ├── eurokg.py             # Euro/kg (Dashboard, Resumo Mensal, Consumo Teórico)
│   ├── producao.py           # Produção Gelado (9 tabs)
│   ├── vendas.py             # Vendas Bolhão (4 botões: Resumo Diário, Receção de Transferências, Registar Quebras, Pesagem Fim de Dia)
│   ├── pastelaria.py         # Pastelaria (7 botões: 4 operacionais + coberturas/tipologias/produtos)
│   ├── confeitaria.py        # Confeitaria (5 botões: 4 operacionais + produtos)
│   ├── compras.py            # Compras (Artigos de Fornecimento, Criar Ordem de Transferência)
│   ├── logistica.py          # Logística (Transferências Agendadas, Ordens de Transferência)
│   ├── faturas.py            # Faturas (upload/OCR, listagem, detalhe, fornecedores, importação Excel)
│   └── gestor.py             # Gestor (Uploads + config pages)
├── templates/
│   ├── base.html             # Base template (Bootstrap 5, navbar, dark mode)
│   ├── login.html
│   ├── home.html             # Navigation grid (CSS grid, square buttons)
│   ├── components/           # Reusable components (tabs, back button)
│   ├── eurokg/               # 3 templates
│   ├── producao/             # 11 templates
│   ├── vendas/               # 4 templates (resumo, transferencias, quebras, pesagem)
│   ├── pastelaria/           # 7 templates
│   ├── confeitaria/          # 5 templates
│   ├── compras/              # 2 templates (artigos, criar_ordem)
│   ├── logistica/            # 3 templates (index, agendadas, ordens)
│   ├── forecast/             # 4 templates (index, explicacao, precisao, meteo_config)
│   └── gestor/               # config pages + partials (inclui Catálogo de Materiais)
├── services/
│   ├── __init__.py
│   ├── kpi.py                # KPI calculation com caching inteligente (histórico vs corrente)
│   ├── producao.py           # Orquestração de produção + invalidação de cache
│   └── session.py            # Helpers de sessão (require_user, login_required decorator)
└── static/
    └── style.css             # Mobile-first CSS, dark mode, nav-grid
database.py                   # Shim de compatibilidade (re-exporta tudo de db/)
db/                           # Pacote de dados PostgreSQL (psycopg2)
├── __init__.py               # Re-exporta todos os módulos
├── cache.py                  # Cache TTL in-process (ttl_cache, invalidate)
├── connection.py             # Pool de conexões PostgreSQL
├── schema.py                 # Migrações e criação de tabelas (SCHEMA_VERSION=13)
├── producao.py               # Produção gelado (plano, KPIs, stock, pesagem)
├── plano.py                  # Plano de produção diário
├── pastelaria.py             # Pastelaria e confeitaria
├── vendas.py                 # Vendas e registos de loja
├── stores.py                 # Gestão de lojas
├── users.py                  # Utilizadores e sessões
├── artigos.py                # Artigos administrativos e compras
├── logistica.py              # Logística e transferências
├── faturas.py                # Faturas e fornecedores
├── forecast.py               # M2b: Motor de previsão de vendas (algoritmo 5 passos, MAPE, overrides)
├── sheets.py                 # Integração Google Sheets
└── utils.py                  # Utilitários partilhados
gunicorn.conf.py              # Configuração gunicorn (4 workers, scheduler guard)
```

### Base de Dados
PostgreSQL (Replit Database) via variável DATABASE_URL com as seguintes tabelas:
- `users` - Utilizadores com permissões granulares
- `producao` - Registos de produção de gelado (kg) com sabor
- `quebras` - Registos de quebras/perdas
- `vendas` - Registos de vendas (€)
- `stock_gelado` - Stock de gelado por sabor (início/fim do dia)
- `rececao_mercadoria` - Receções de mercadoria (auto-preenchido ao confirmar transferências)
- `producao_pastelaria` / `producao_confeitaria` - Registos históricos de produção por unidades
- `produtos_pastelaria` / `produtos_confeitaria` - Listas de produtos
- `plano_producao_pastelaria` / `plano_producao_confeitaria` - Planeamento de produção por produto (estimado/real); pastelaria inclui status (pendente/em_curso/concluido), nota, estimado por loja (bolhao/matosinhos)
- `stock_producao_pastelaria` / `stock_producao_confeitaria` - Stock de produção disponível por produto/dia
- `contagem_stock` - Contagens de stock de balcão (pastelaria/confeitaria)
- `vendas_detalhe` - Vendas detalhadas por produto
- `produtos_vendas_config` - Configuração de produtos para KPIs
- `receitas_gelado` - Mapeamento receitas → sabores
- `plano_producao` / `ordem_producao` - Planeamento de produção
- `stock_producao` - Stock de produção por sabor/loja/dia (agregado multi-data, redução FIFO); lojas suportadas: Bolhão, Matosinhos, Mouzinho, B2B
- `transferencias` - Registo de transferências de stock de produção para lojas
- `ordens_transferencia` - Ordens de transferência com workflow (pendente/confirmada/rejeitada) + `data_prevista`
- `artigos_administrativos` - Artigos de fornecimento (fornecedor + produto, ativo/inativo)
- `user_sessions` - Sessões de autenticação
- `sales_forecasts` — Previsões de vendas diárias por loja (previsao_eur, banda_min/max, score_meteo, condicao_meteo, factor_yoy, multiplicador_meteo, base_historica, override_manual, override_motivo, venda_real, erro_real_pct)
- `forecast_meteo_config` — Multiplicadores meteorológicos por loja e banda de score (0–29/30–59/60–84/85–100)

## Módulos da Aplicação

### Home (10 módulos)
Euro/kg | Produção Gelado | Produção Pastelaria | Produção Confeitaria | Vendas Bolhão | Compras | Logística | Gestor | Financeiro | Eventos

### Previsão de Vendas — M2b (`/forecast/`)
Motor de previsão de vendas com algoritmo de 5 passos, ajuste meteorológico e factor YoY.
- **Dashboard 7 dias** (`/forecast/`): vista consolidada ou por loja, previsão em € com banda de confiança (min/max), condição meteorológica, indicador de época (Alta/Baixa) e factor YoY
- **Detalhe/Explicação** (`/forecast/explicacao?loja=X&data=YYYY-MM-DD`): decomposição do modelo por componente (base histórica, YoY, meteo), base das últimas 8 semanas com pesos, tabela de multiplicadores meteorológicos, formulário de override manual com motivo
- **Precisão** (`/forecast/precisao`): MAPE por loja e tipo de dia (semana/fim-de-semana), evolução temporal do erro, dias com maior erro histórico
- **Configuração Meteo** (`/forecast/meteo-config`): multiplicadores por score meteorológico (0–29/30–59/60–84/85–100), editáveis por loja

**Algoritmo de 5 passos:**
1. Base histórica ponderada: últimas 8 semanas do mesmo dia-da-semana, pesos 1→8 (mais recente = 8)
2. Factor YoY: mean(últimos 60 dias) / mean(mesmo período no ano anterior), limitado a 0.5–2.5
3. Ajuste meteorológico: multiplicador por banda de score (0.70 a 1.15), calibrável por loja
4. Banda de confiança: ±desvio padrão histórico + penalização por divergência entre fontes meteo
5. Override manual: substituição total da previsão para eventos especiais, com registo de motivo

**Tabelas DB:**
- `sales_forecasts` — previsões diárias por loja (previsao_eur, banda_min/max, score_meteo, factor_yoy, multiplicador_meteo, override_manual, venda_real, erro_real_pct)
- `forecast_meteo_config` — multiplicadores meteorológicos calibráveis por loja e banda de score

**Recalibração:** `associate_real_sales()` associa vendas reais do upload POS ao forecast correspondente e calcula `erro_real_pct`. MAPE calculado sobre dados históricos.

### Financeiro
Hub com 5 módulos activos: Crédito, Faturas, Pagamentos, IVA, Liquidez. Cash Flow previsto para Fase 8.

#### Pagamentos & IVA — M0b (`/financeiro/pagamentos/`)
- **Dashboard** (`/financeiro/pagamentos/`): resumo faturas pending/agendadas, próximo IVA a pagar, liquidez 4 semanas
- **Faturas** (`/financeiro/pagamentos/faturas`): listagem com filtros por estado (pending_review/scheduled/paid/overdue/cancelled), link para detalhe
- **Nova Fatura** (`/financeiro/pagamentos/faturas/nova`): registo manual de fatura de fornecedor com autocomplete de fornecedores
- **Detalhe Fatura** (`/financeiro/pagamentos/faturas/<id>`): edição, confirmação de data de pagamento, marcar como paga, cancelar, eliminar
- **IVA** (`/financeiro/pagamentos/iva`): dashboard de períodos IVA (histórico + futuros), alertas de prazos, regime mensal
- **Período IVA** (`/financeiro/pagamentos/iva/<year>/<month>`): recalcular IVA automaticamente, registar declaração, registar pagamento
- **Liquidez Semanal** (`/financeiro/pagamentos/liquidez`): próximas 8 semanas com entradas (POS est. + eventos) e saídas (faturas + crédito + IVA), saldo acumulado, identificação de semanas críticas

**Tabelas DB:**
- `suppliers` — fornecedores (name, nif, categoria, store_id, ativo)
- `invoices` — faturas de fornecedores (supplier_name, nif, invoice_number, amount_eur, vat_amount_eur, issue_date, due_date, store_id, categoria, status, notes)
- `invoice_payments` — pagamentos de faturas (invoice_id UNIQUE, proposed_date, confirmed_date, paid_date, amount_eur, status, confirmed_by)
- `vat_periods` — períodos de IVA (year, month, vat_collected_eur, vat_deductible_eur, vat_due_eur, estimates, declaration_date, payment_date, status: estimated→declared→paid)

**Lógica de IVA:**
- Regime mensal (>650k€): declaração dia 10 do M+2, pagamento dia 25 do M+2
- IVA dedutível: soma `vat_amount_eur` das faturas de fornecedores com issue_date no período
- IVA liquidado: estimativa baseada em vendas POS (23%) + quote_items de eventos adjudicados (23%)
- Taxas 6%/13%/23% por categoria ainda não implementadas (aguarda confirmação contabilidade)

**Migrations:** `run_migrations_m0()` em `database.py` — cria tabelas idempotentemente em cada startup

#### Crédito (Responsabilidades de Crédito)
- **Dashboard** (`/financeiro/credito/`): KPIs (saldo em dívida, custo mensal fixo, nº contratos, saídas 30 dias), tabela de contratos ativos, próximas saídas, alertas de contratos a vencer (90 dias) e caucionada com dados pendentes.
- **Contratos** (`/financeiro/credito/contratos`): CRUD de contratos (leasing, crédito automóvel, empréstimo, conta caucionada). Formulário adaptado por tipo: overdraft mostra plafond e oculta prestação/dia débito. Filtro por estado.
- **Calendário** (`/financeiro/credito/calendario`): Lista de saídas previstas nas próximas 13 semanas. Distingue saídas certas (prestação fixa) de estimadas (juros caucionada).
- **Caucionada** (`/financeiro/credito/caucionada`): Formulário de registo de saldo bancário semanal. Cálculo automático de utilização (MAX(0, plafond − saldo)) e custo de juros estimado (utilização × TAN/100 / 365 × 30). Histórico de entradas.
- **Upload Documento** (`/financeiro/credito/upload`): Upload de PDF ou imagem de documento bancário. IA (GPT-5 via Replit AI Integrations) extrai automaticamente dados de contratos. Extração de texto de PDF com pdfplumber + fallback para vision. Formulário de revisão e confirmação antes de criar contratos.

**Tabelas DB:**
- `credit_contracts` — contratos de crédito (tipo, label, banco, loja, capital_inicial, saldo_divida, tan, prestacao_mensal, dia_debito, data_inicio, data_fim, plafond, estado, notas)
- `bank_balance_entries` — registos de saldo bancário semanal (data, loja, saldo, contrato_id, utilizacao_calculada, custo_juros_estimado, notas)

### Produção Gelado (9 botões)
Planear Produção | Produzir | Transferir para Loja | Ordem de Produção | Stock Gelado | Registar Quebra de Produção | Dashboard Produção | Receitas de Gelado | Lista de Sabores

### Vendas Bolhão (4 botões)
Resumo Diário | Receção de Transferências | Registar Quebras | Pesagem Fim de Dia

### Compras (2 botões)
Artigos de Fornecimento | Criar Ordem de Transferência

### Logística (2 botões)
Transferências Agendadas (agrupadas por data_prevista + loja) | Ordens de Transferência (lista completa)

### Pastelaria (5 botões)
Stock de Balcão | Planear Produção | Produzir | Transferir para Loja | Gerir Produtos

#### Gerir Produtos (3 sub-páginas)
Lista de Produtos (read-only, bulk delete) | Coberturas | Tipologias

### Confeitaria (5 botões)
Stock de Balcão | Planear Produção | Produzir | Transferir para Loja | Produtos

## Lógica de Negócio

### KPI Principal
```
KPI (€/kg) = Vendas Gelado (€) / Consumo (kg)
```

**Cálculo do Consumo por vista (usa tabela `transferencias` como fonte):**
- **Global Porto:** Stock Início + Produção - Stock Fim - Quebras
- **Bolhão:** Stock Início + Transferências Recebidas - Stock Fim - Quebras
- **Matosinhos:** Stock Início + (Produção - Transferências p/ Bolhão) - Stock Fim - Quebras

**Pesagens e Datas:**
- **Matosinhos:** pesagem tipo='inicio' (início do dia) → stock_ini=inicio(d), stock_fim=inicio(d+1)
- **Bolhão:** pesagem tipo='fim' (fim do dia) → stock_ini=fim(d-1), stock_fim=fim(d)
- **Global Porto:** soma independente dos stocks de cada loja

### Targets Dinâmicos
- **Época Alta (Mai-Set):** 24€/kg
- **Época Baixa (Out-Abr):** 25€/kg

### Geração de Lotes
- **Pastelaria:** P + DD/MM/YY (ex: P050226)
- **Confeitaria:** C + DD/MM/YY (ex: C050226)

### Ordens de Transferência
- Todas as transferências (gelado, pastelaria, confeitaria, compras) criam ordens com status `pendente` e `data_prevista`
- Vendas Bolhão > "Receção de Transferências" permite confirmar/rejeitar ordens pendentes para Bolhão
- Ao confirmar: stock de destino é atualizado automaticamente (rececao_mercadoria para gelado, contagem_stock para pastelaria/confeitaria)
- Logística > "Transferências Agendadas" mostra ordens agrupadas por data_prevista + loja_destino
- Logística > "Ordens de Transferência" lista todas as ordens com histórico completo

### Stock de Produção
- Gelado: `stock_producao` agrega stock de TODAS as datas (SUM por sabor/loja); redução FIFO (dias mais antigos primeiro)
- Transferir para Bolhão: se stock produção Bolhão insuficiente, auto-transfere de stock produção Matosinhos → Bolhão
- Pastelaria/Confeitaria: `stock_producao_pastelaria/confeitaria` agrega stock de TODAS as datas; redução FIFO

## Executar a Aplicação
```bash
python flask_app/app.py
```

## Deployment
- **Dev:** `python flask_app/app.py` na porta 5000
- **Prod:** `gunicorn --bind=0.0.0.0:5000 --preload --timeout 120 flask_app.app:create_app()` (autoscale)
- Healthcheck em `/healthcheck` (200 OK)
- Dev login: `/dev-login/nivadev2026`

## Dependências
- flask
- pandas
- plotly
- beautifulsoup4
- psycopg2-binary
- bcrypt
- gunicorn
- openpyxl, xlrd, lxml, html5lib

### Transferências
- Todas as transferências (gelado, pastelaria, confeitaria, compras) são apenas para Bolhão
- Gelado mantém info de stock Matosinhos como referência visual; auto-pull de Matosinhos quando stock Bolhão insuficiente
- Ordens criadas com `criar_ordem_transferencia()` + `data_prevista`

### Eventos (Fase 1 — CRM e Orçamentação)
- **Pipeline** — Vista de todos os eventos com filtro por estado (lead/contacted/proposal_sent/negotiating/won/lost/cancelled). Criação, edição e eliminação de eventos.
- **Leads** — Lista de leads com importação automática do Google Sheet de formulário Nivà (sync manual via botão), criação manual, detalhes e conversão em evento.
- **Artigos** — CRUD de artigos padrão de evento com preço base ajustável e nível de custo (Alto/Médio/Baixo).
- **Clientes** — Registo de clientes de eventos (marketing consent, histórico de eventos, analytics de recorrência).
- **Orçamentação** — Dentro de cada evento: adicionar/editar/remover quote_items, cálculo automático de total.
- **Adjudicação** — Ao transitar para 'won': invoice_amount_eur calculado dos quote_items, expected_payment_date = event_date + 30 dias.
- **Transições validadas** — Cada estado só permite transições válidas; lost requer motivo de perda.
- **Google Sheets** — Integração via Replit Connector (OAuth) para importar leads do Sheet 18uOVTPh74uz47zJTw9hCeTikNZDarm0hQUDmhrQMTgk.

#### Tabelas DB (Eventos)
- `lead_requests` — Leads (manual e Google Sheets), com google_sheet_row_id único para evitar duplicados
- `events` — Eventos com pipeline CRM completo, campos financeiros (invoice_amount_eur, expected_payment_date, payment_status)
- `artigos_evento` — Artigos padrão de evento com preço base e cost_tier
- `quote_items` — Itens de orçamento por evento (total GENERATED)
- `event_clients` — Clientes de eventos com marketing_consent, ligados a eventos via client_id FK

#### Ficheiros Novos
- `flask_app/routes/eventos.py` — Blueprint completo com todas as rotas
- `flask_app/google_sheets_sync.py` — Sync de leads do Google Sheet via API REST
- `flask_app/templates/eventos/` — Templates (index, pipeline, evento_detail, evento_form, leads, lead_detail, lead_form, artigos, clientes, cliente_detail, _macros)

## Dependências AI
- `openai` — Replit AI Integrations (env vars: AI_INTEGRATIONS_OPENAI_BASE_URL, AI_INTEGRATIONS_OPENAI_API_KEY)
- `pdfplumber` — extração de texto de PDFs
- `tenacity` — retry com backoff para chamadas API

## Fase 8 — M3: Cash Flow 13 Semanas

Motor central de Cash Flow que agrega todas as fontes de entradas e saídas e projecta semana a semana.

### Ficheiros
- `db/cashflow.py` — Engine de agregação, migrations, config helpers, alert builder
- `flask_app/routes/cashflow.py` — Blueprint `cashflow_bp` em `/financeiro/cashflow/`
- `flask_app/templates/financeiro/cashflow/` — Templates: index, semana (drill-down), configuracoes
- `database.py` — Expõe `db/cashflow.*` via star import

### Tabela DB
- `cashflow_config` — Configurações (threshold alerta, salários, débitos directos em JSON)

### Funcionalidades
- Dashboard 13 semanas: entradas/saídas por categoria, saldo projectado e acumulado
- 7 categorias de saída: leasings/créditos (certa), juros caucionada (estimada), débitos directos (certa), salários impostos (~dia 15), salários líquido (~dia 28), faturas agendadas, IVA
- 2 categorias de entrada: vendas forecast (YoY ou média 4 semanas), recebimentos de eventos
- Lógica caucionada: saldo_acumulado < 0 mas coberto pelo plafond → usa_caucionada; excede plafond → risco_real
- Sistema de alertas: 🔴 CRÍTICO (saldo negativo sem caucionada; fatura vencida >3 dias), 🟡 ATENÇÃO (saldo abaixo threshold; sobreposição saídas; IVA não declarado; faturas por decidir)
- Drill-down por semana com detalhe de cada entrada/saída e links para módulos
- Configurações: threshold de alerta, salários (impostos + líquido + dias), débitos directos recorrentes

## Tile Visibility Management (Task #87)

Sistema DB-backed de visibilidade de tiles por módulo.

### Ficheiros
- `db/tiles.py` — Migration (`run_migrations_tile_config`, lock 202613), helpers: `get_tile_visibility`, `set_tile_visibility`, `seed_tile_config`, `get_all_tile_config`
- `flask_app/routes/gestor.py` — Rota `GET/POST /gestor/gestao-tiles` com toggle de visibilidade; tile `gestao_tiles` adicionado ao TABS
- `flask_app/templates/gestor/gestao_tiles.html` — UI table com botões Visível/Oculto por tile

### Tabela DB
- `tile_config(module, tile_id, label, visible, updated_at)` — PK composto (module, tile_id)

### Comportamento
- Tiles não presentes na DB → visível por defeito (True)
- Defaults hidden: `producao/ordem`, `producao/receitas`, `producao/sabores_ativos`
- Módulos com filtro: producao, pastelaria, vendas, gestor, financeiro
- `seed_tile_config` é chamado no index de financeiro; restantes módulos são auto-seeded quando visitados via gestor

## Última Atualização
2026-04-07 - Task #87: Tile visibility management implementado. DB table tile_config, helpers em db/tiles.py, migration 202613, filtro em todos os módulos, UI gestor para toggle.
