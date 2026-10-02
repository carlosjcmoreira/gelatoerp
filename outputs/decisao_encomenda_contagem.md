# Encomendas e contagens: regra para aprovação

**Base:** leitura do código dos três fluxos e da proposta do catálogo mestre, datada de 1 de outubro de 2026. Não foram consultados dados em tempo real nem alterado qualquer registo.

## Regra atual

- As encomendas semanais, os pedidos urgentes e as contagens disponibilizam artigos **ativos**. A validação no servidor também rejeita artigos inativos.
- A falta de fornecedor oficial confirmado não bloqueia um artigo ativo nos três fluxos. O fornecedor é carregado como informação opcional; não é requisito para selecionar ou submeter um artigo ativo já existente.
- Ainda não existe um campo separado de elegibilidade para encomenda: hoje, todos os artigos ativos podem ser pedidos e contados.
- «Artigo novo por validar» não é um estado geral usado por estes fluxos. A criação manual atual exige um fornecedor oficial, mas isso não equivale a uma validação completa do artigo. Numa futura importação, o facto de o fornecedor estar por confirmar e o artigo estar por validar devem continuar a ser situações distintas.

## Duas opções futuras — propostas, ainda não aprovadas

Em ambas, a elegibilidade para encomenda passaria a limitar novas encomendas semanais e urgentes. A decisão em aberto é se essa elegibilidade também limita novas contagens:

**Opção A — contar todos os artigos ativos.** Um artigo ativo pode ser contado mesmo quando não é encomendável. A contagem mantém-se independente da compra; não é necessário criar um terceiro campo «pode contar».

**Opção B — contar apenas artigos encomendáveis.** Um artigo ativo que não possa ser encomendado também deixa de aparecer em novas contagens. A elegibilidade funciona como limite para encomendas e contagens; também não exige um terceiro campo.

Na matriz, a ordem dos três valores é **encomenda semanal / pedido urgente / contagem**. «Proposta» assinala um comportamento futuro, não uma regra já ativa.

| Cenário | Hoje | Opção A — proposta | Opção B — proposta |
|---|---|---|---|
| Ativo e encomendável | Sim / Sim / Sim | Sim / Sim / Sim | Sim / Sim / Sim |
| Ativo e não encomendável — estado futuro | Não existe esta distinção; hoje, um artigo ativo é encomendável. | Não / Não / Sim | Não / Não / Não |
| Inativo | Não / Não / Não | Não / Não / Não | Não / Não / Não |
| Novo, ainda por validar | Não há estado geral de validação. Se estiver ativo, hoje é Sim / Sim / Sim; se estiver inativo ou ainda não registado, é Não / Não / Não. | Se for aprovado como ativo e não encomendável: Não / Não / Sim. A validação, por si só, não cria esse estado. | Se for aprovado como ativo e não encomendável: Não / Não / Não. A validação, por si só, não cria esse estado. |
| Ativo com fornecedor por confirmar | Sim / Sim / Sim | A confirmação do fornecedor, por si só, não bloqueia. Se for encomendável: Sim / Sim / Sim; se não, aplica-se a linha «não encomendável». | A confirmação do fornecedor, por si só, não bloqueia. Se for encomendável: Sim / Sim / Sim; se não, aplica-se a linha «não encomendável». |

## Histórico e disponibilidade existente

Em ambas as opções, a mudança deve afetar apenas novas linhas de encomenda ou contagem, depois de aprovada. Encomendas já submetidas, versões e linhas guardadas, envios, receções e contagens submetidas têm de permanecer preservados. Desativar um artigo ou mudar a sua elegibilidade não deve apagar nem reescrever esses registos.

Até haver aprovação explícita, todos os artigos ativos existentes mantêm a disponibilidade atual para encomenda e contagem — incluindo os que têm fornecedor por confirmar. A proposta da reconciliação para novos artigos só ficarem encomendáveis depois de validados é uma proposta, não uma regra aplicada.

## Recomendação para aprovação

**Recomendo a Opção A:** a contagem regista o que a loja encontra fisicamente; não poder comprar um artigo não torna essa observação inútil. A Opção B simplifica a lista de contagem, mas pode ocultar quantidades existentes precisamente quando um artigo está bloqueado para compra ou aguarda validação.

**Qual regra aprova para novas contagens de artigos ativos não encomendáveis: Opção A, permitir a contagem, ou Opção B, impedir a contagem?**