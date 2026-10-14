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
Isso significa que, para o emissor, a operação foi INTERNA (mesmo estado) — ou a operação é
interestadual em caso de REVENDA (destinatário contribuinte, sem DIFAL) — ou a nota não
destacou o DIFAL (o que, em operação interestadual devidamente sujeita a ele, seria um
problema a apurar com o fornecedor).

## Caso: Operação interna (mesmo estado)
quando: interna
resposta:
No SEU caso a operação é INTERNA (origem e destino no mesmo estado): NÃO incide DIFAL.
O ICMS é cobrado inteiro pelo estado, pela alíquota interna (ex.: 18% em SP e em MG).
O DIFAL só existe em operação interestadual (EC 87/2015).

## Caso: Interestadual — destinatário vai revender (contribuinte)
quando: interestadual; contribuinte; revenda
resposta:
No SEU caso a operação é INTERESTADUAL e o destinatário vai REVENDER (contribuinte de ICMS):
• O estado de ORIGEM cobra a alíquota interestadual (Res. SF 22/1989 e 13/2012 — função
  aliquota_interestadual): 4% para bens importados; 7% quando a origem é do Sul/Sudeste
  (PR, SC, RS, SP, RJ, MG) e o destino é Norte, Nordeste, Centro-Oeste ou ES; 12% nos
  demais casos.
• NÃO há DIFAL (0%): o ICMS interestadual destacado pela origem é aproveitado pelo
  destinatário como CRÉDITO.
• Ex.: PA → MG para quem revende: a origem cobra 12% e não há DIFAL.

## Caso: Interestadual — uso/consumo ou ativo imobilizado de contribuinte
quando: interestadual; contribuinte; uso_consumo
resposta:
No SEU caso a operação é INTERESTADUAL e o destinatário (contribuinte) vai USAR/CONSUMIR
ou lançar no ATIVO IMOBILIZADO:
• A origem cobra a alíquota interestadual (4% importados; 7% da origem no Sul/Sudeste
  com destino no Norte, Nordeste, Centro-Oeste ou ES; 12% nos demais casos).
• HÁ DIFAL = alíquota interna do destino − alíquota interestadual, em pontos percentuais.
  Ex.: PA → MG: 18% − 12% = 6 pontos percentuais.
• Em regra, o DESTINATÁRIO contribuinte recolhe ao estado de destino — confirme na
  legislação do estado.
• Confirme a alíquota interna e as regras do estado de destino com seu contador.

## Caso: Interestadual — venda a pessoa física / não contribuinte
quando: interestadual; nao_contribuinte
resposta:
No SEU caso a operação é INTERESTADUAL e o destinatário é PESSOA FÍSICA ou NÃO
CONTRIBUINTE (consumidor final):
• A origem cobra a alíquota interestadual (4% importados; 7% da origem no Sul/Sudeste
  com destino no Norte, Nordeste, Centro-Oeste ou ES; 12% nos demais casos).
• HÁ DIFAL = alíquota interna do destino − alíquota interestadual, em pontos percentuais.
  Ex.: SP → MG: 12% em SP e DIFAL de 6 pontos percentuais em MG (18% − 12%).
• O REMETENTE (a empresa que vende) recolhe o DIFAL para o estado de destino — na NFe
  aparece destacado como "ICMS DIFAL" / "ICMS Interestadual - DIFAL" e soma no total.
• Confirme a alíquota interna e as regras do estado de destino com seu contador.

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
2. Qual destes casos vale para o destinatário: 1) vai REVENDER (contribuinte, sem DIFAL);
   2) vai USAR/CONSUMIR ou imobilizar o bem (contribuinte, com DIFAL recolhido pelo
   destinatário); 3) é PESSOA FÍSICA ou NÃO CONTRIBUINTE (com DIFAL recolhido pelo
   remetente);
3. Se é mercadoria ou serviço.
Regra de referência: interna = 18% (SP e MG); interestadual = 4% (importados), 7% (origem no
Sul/Sudeste exceto ES com destino no Norte, Nordeste, Centro-Oeste ou ES), 12% (demais);
DIFAL = alíquota interna do destino − interestadual (em pontos percentuais). Confirme a
alíquota interna e as regras do estado de destino com seu contador.
