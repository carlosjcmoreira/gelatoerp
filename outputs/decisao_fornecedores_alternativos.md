# Fornecedores alternativos: opções para decisão

**Fontes consultadas:** snapshot publicado de 1 de outubro de 2026; inventário «15_01_2026» e respetiva reconciliação. Esta nota não consulta dados em tempo real nem altera registos.

## Pergunta

Quando o mesmo artigo pode ser comprado a empresas diferentes, deve existir uma ficha por fornecedor ou uma ficha de artigo partilhada com fornecedores alternativos?

## Factos disponíveis

- O catálogo atual permite no máximo um `fornecedor_oficial_id` por ficha de artigo. A criação de uma ficha nova exige selecionar um fornecedor; os registos históricos podem não ter fornecedor oficial confirmado.
- O snapshot contém 143 artigos: 115 têm fornecedor oficial associado e 28 não têm.
- Não há no snapshot um par de artigos com o mesmo nome de produto normalizado e IDs de fornecedores oficiais diferentes. Isto não prova que não existam artigos equivalentes; significa apenas que os dados atuais não os identificam com segurança.
- O inventário tem oito linhas cujo nome do produto coincide exatamente com um nome do catálogo. As oito ficaram para revisão por fornecedor ou unidade; nenhuma foi classificada como correspondência exata candidata.

## Exemplos que exigem validação humana

Estes exemplos ilustram limites dos dados; nenhum confirma que os registos representem o mesmo artigo ou fornecedores alternativos.

| Inventário de janeiro | Candidato no catálogo publicado | O que falta confirmar |
|---|---|---|
| Linha 166: «Saco congelação 3l», fornecedor «Makro», unidade «Cx» | Artigo **#11**, mesmo nome, fornecedor oficial **#96** «MAKRO CASH & CARRY PORTUGAL, S.A.», unidade «l» | A unidade do inventário é «Cx» e a do catálogo é «l». O texto «3l» no nome não permite converter nem confirmar a unidade de compra. |
| Linha 97: «Chá Black», fornecedor «Grasumos», unidade «Un» | Artigo **#96**, «Chá black»; etiqueta atual «CAFÉ ILLY»; fornecedor oficial **#60** «GRASUMOS, COMÉRCIO DE BEBIDAS,LDA.»; unidade não registada | O nome do fornecedor e a unidade precisam de confirmação. O ID oficial e a etiqueta não devem ser substituídos automaticamente pelo texto do inventário. |
| Linha 247: «Esfregona», fornecedor «Pingo Doce», unidade «Un» | Artigo **#90**, «Esfregona»; etiqueta atual «MATOSINHOS»; sem fornecedor oficial confirmado e sem unidade registada | A designação operacional «MATOSINHOS» não identifica um fornecedor. Não é evidência de que o Pingo Doce forneça o artigo do catálogo. |

## Opções

| Tema | A. Uma ficha por fornecedor | B. Uma ficha comum com fornecedores alternativos |
|---|---|---|
| **Encomendas** | A ficha identifica um fornecedor; o artigo encomendado já aponta para essa relação. | A encomenda tem de escolher o fornecedor alternativo aplicável e manter essa escolha no registo. |
| **Contagens** | O mesmo produto pode surgir em mais do que uma ficha. Só se somam quantidades entre fichas após agrupamento humano confirmado. | A contagem usa uma identidade comum; unidades e apresentações diferentes têm de ser compatíveis ou tratadas separadamente. |
| **Marca, embalagem e unidade** | Cada fornecedor pode manter a sua designação, marca, embalagem e unidade sem conversões implícitas. | A ficha comum só é segura para o mesmo artigo. É preciso preservar por fornecedor as apresentações e unidades de compra; conversões só podem ser usadas se forem explícitas e aprovadas. |
| **Faturas e histórico** | Cada linha permanece ligada à ficha e ao fornecedor correspondentes. Consultas entre fornecedores exigem agrupamento confirmado. | Faturas partilham a identidade do artigo, mas cada linha tem de preservar o fornecedor e a designação/unidade recebidas. Os documentos históricos não devem ser reescritos. |
| **Esforço e risco de transição** | Menor: mantém a regra atual e evita fusões com evidência incompleta. Pode duplicar artigos e dificultar análises agregadas. | Maior: exige validar quais fichas são o mesmo produto, representar diferenças por fornecedor e migrar sem perder histórico. Facilita visão agregada só depois dessas decisões. |

## Proposta provisória

Até haver confirmações humanas, manter fichas separadas por fornecedor é a opção de menor risco: corresponde ao modelo atual e evita fundir registos apenas porque os nomes coincidem. Isto é uma proposta de transição, não uma decisão permanente sobre o catálogo. Se a equipa aprovar a ficha comum, as diferenças de fornecedor, embalagem e unidade terão de continuar visíveis e o histórico terá de permanecer intacto.

Antes de agrupar quaisquer artigos, será necessário validar a identidade física do produto, marca/apresentação, unidade e fornecedor oficial. Os três exemplos acima continuam pendentes; nenhum comprova, por si só, a necessidade de fornecedores alternativos.

**Qual opção aprova para o modelo futuro: A) uma ficha distinta por fornecedor, ou B) uma ficha comum com fornecedores alternativos?**