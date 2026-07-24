---
name: Produção manual = registo de controlo
description: Regra de negócio — entradas producao tipo='manual' nunca contam no Euro/kg
---

Entradas na tabela `producao` com `tipo='manual'` (e qualquer tipo fora da whitelist) são **registos de controlo** introduzidos pela equipa para comparar com o que a balança/produção regista.

**Rule:** nunca somar tipos fora de `('producao','balança')` nos cálculos de Produção/Consumo do Euro/kg; nunca apagar registos manuais; mantê-los visíveis em tabelas de comparação (Histórico de Produção, colunas Balança vs Manual).

**Why:** confirmado pelo utilizador em 24/07/2026 — "São mecanismos de controlo... Não é para remover, nem usar para o euro/kg." Fontes válidas para Euro/kg: `tipo='balança'` (extração da balança) e `tipo='producao'` (CSV CalybraBox), com exclusão das lojas não-produtoras (Bolhão, B2B) e dos sabores base sem conta_eurokg.

**How to apply:** queries de KPI Euro/kg sobre `producao` devem filtrar pela whitelist de tipos (constante partilhada em código). Um mês só com entradas manuais mostra 0 kg de produção — comportamento esperado, não é bug.
