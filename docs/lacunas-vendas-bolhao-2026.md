# Lacunas nas vendas de Bolhão — 02/02 e 22/03/2026

Verificação em 28/09/2026. **Não foi feita nenhuma alteração aos dados de produção.**

## 02/02/2026

- Existem dois exports originais de vendas **por produto**, de 01/01 a 05/02 e
  de 01/01 a 08/02, em `attached_assets/venda_produtos_01_jan_a_5_fev_1770332579080.xls`
  e `attached_assets/niva_-_venda_prod_0101_a_0802_1770585417632.xls`.
  Ambos incluem exatamente as mesmas linhas deste dia: **Loja 1 — Niva
  Matosinhos**, 7 linhas, 17 unidades, 61,90 € (54,781 € S/IVA); **Loja 3 —
  Niva Porto / Bolhão**, 26 linhas, 80 unidades, 353,30 € (309,902 € S/IVA).
  O export diário por data/hora de 01/02 a 04/02 confirma os totais com IVA
  por loja (61,90 € e 353,30 €).
- Na consulta de produção, `vendas_detalhe` contém **Bolhão: 7 linhas,
  17 unidades, 61,90 €** e **Matosinhos: 26 linhas, 80 unidades, 353,30 €**.
  As linhas coincidem com as dos originais, mas encontram-se atribuídas às
  lojas contrárias. Nenhuma destas lojas foi alterada.
- O ficheiro `exports/bolhao-vendas-2026-02-02-para-gestor.xlsx` é uma seleção
  fiel das 26 linhas de `Loja : 3` do original (não é uma estimativa). Uma
  simulação **sem gravação na base de dados** do importador XLSX do Gestor
  confirmou 26 linhas, apenas Bolhão e apenas 02/02, com 353,30 € C/IVA e
  309,902 € S/IVA. O importador substituiria as linhas existentes de Bolhão
  para essa data numa transação.
- **Pendente:** não fazer essa substituição isolada antes de resolver a
  atribuição aparentemente trocada de Matosinhos: caso contrário, os 353,30 €
  passariam a constar nas duas lojas. Confirmar a atribuição com o responsável
  pelos exports e planear uma correção segura para os dois pares loja/data.

## 22/03/2026

- Nos ficheiros de vendas por produto disponíveis não há um original diário
  desse dia para Bolhão. O export XLSX capturado em 23/03 refere-se ao período
  **01/01/2025 a 31/12/2025**, não a março de 2026. O ficheiro trimestral
  agrega valores por mês/produto e não serve para recuperar linhas diárias.
- Na consulta de produção, `vendas_detalhe` contém **zero linhas para Bolhão**
  e **30 linhas para Matosinhos** nesse dia. Mantém-se a lacuna: é necessário
  receber o export original de vendas **por produto** que inclua 22/03/2026,
  com a identificação de loja, para validar e importar somente as linhas
  comprovadas de Bolhão. Não extrapolar totais nem fabricar linhas.

Após qualquer importação autorizada, comparar por par loja/data o número de
linhas, quantidade e valor bruto/líquido com o original, verificar a ausência
de linhas duplicadas e confirmar que outras datas e lojas não mudaram.