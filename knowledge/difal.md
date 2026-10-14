# DIFAL — Diferencial de Alíquota de ICMS

keywords: difal, diferencial de aliquota, mof, icms interestadual, difal do icms, difal da nfe, difal da nota, difal da fatura

## Caso: Nota da conversa já tem DIFAL destacado
quando: com_difal
resposta:
Analisando a sua situação com a nota que está na conversa: ela JÁ tem DIFAL destacado, ou
seja, a operação é interestadual e o estado de destino cobrou a diferença de alíquota.
O valor/base/alíquota destacados aparecem logo abaixo — é o DIFAL do SEU caso, não uma regra
genérica. Se o valor não bater com o esperado, me diga os estados de origem e destino que eu
recalculo.

## Caso: Nota da conversa não tem DIFAL
quando: sem_difal
resposta:
Analisando a sua situação com a nota que está na conversa: ela NÃO tem DIFAL destacado.
Isso significa que, para o emissor, a operação foi INTERNA (mesmo estado) — ou a nota não
destacou o DIFAL (o que, em operação interestadual, seria um problema a apurar com o
fornecedor). Se a operação na verdade é interestadual, o DIFAL devido é a diferença entre a
alíquota interna do estado de destino e a interestadual (4%/7%/12%).

## Caso: Operação interna (mesmo estado)
quando: interna
resposta:
No SEU caso a operação é INTERNA (origem e destino no mesmo estado): NÃO incide DIFAL.
O ICMS é cobrado inteiro pelo estado, pela alíquota interna (ex.: 18% em SP).
O DIFAL só existe em operação interestadual (EC 87/2015).

## Caso: Interestadual — destinatário contribuinte (com crédito)
quando: interestadual; contribuinte
resposta:
No SEU caso a operação é INTERESTADUAL e o destinatário é CONTRIBUINTE de ICMS:
• O estado de ORIGEM cobra a alíquota interestadual: 4% (com crédito) ou 12%
  (operação na mesma região — Sul/Sudeste — com crédito).
• O estado de DESTINO cobra o DIFAL: a diferença entre a alíquota interna dele e a
  interestadual. Ex.: SP → MG, ambos com 18% interno: origem cobra 4% e MG cobra 14% de DIFAL.
• Na NFe o DIFAL aparece destacado ("ICMS DIFAL" / "ICMS Interestadual - DIFAL") e soma no total.

## Caso: Interestadual — destinatário não contribuinte (MOC)
quando: interestadual; nao_contribuinte
resposta:
No SEU caso a operação é INTERESTADUAL e o destinatário é NÃO CONTRIBUINTE (pessoa física /
consumidor final) — é o chamado MOC (Mecanismo de Obrigatoriedade de Cobrança, EC 87/2015
art. 13-A e LC 190/2022):
• O DIFAL é cobrado por ANTECIPAÇÃO pelo estado de DESTINO (substituição tributária).
• O recolhimento normalmente é feito pelo vendedor (ou pelo comprador, se combinado),
  destacado na NFe como "ICMS DIFAL" / "MOC".
• Ex.: compra de SP por pessoa física do RJ: SP cobra 4% (ou 12%) e RJ cobra, por
  antecipação, a diferença até a alíquota interna dele.

## Caso: Serviço (NFS-e)
quando: servico
resposta:
No SEU caso se trata de SERVIÇO: DIFAL NÃO incide — ele é mecanismo do ICMS (mercadorias e
transportes). Serviço interestadual paga ISS, que incide no município do TOMADOR (LC 116/2003).

## Caso: Sem detalhes suficientes para o caso
quando:
resposta:
Para eu analisar o SEU caso específico (e não dar a regra geral), preciso de 3 informações:
1. Estado de ORIGEM (de onde a mercadoria sai) e de DESTINO (para onde vai);
2. Se o destinatário é CONTRIBUINTE de ICMS (empresa com CNPJ) ou NÃO CONTRIBUINTE (pessoa física);
3. Se é mercadoria ou serviço.
Regras de referência: interna não tem DIFAL; interestadual para contribuinte, o destino cobra
a diferença (ex.: 18% − 4% = 14%); para não contribuinte, o destino cobra por antecipação (MOC).
