"""Base de conhecimento fiscal para as respostas do bot.

Consolida as regras usadas para esclarecer perguntas sobre notas e
regimes de impostos:

  * LC 116/2003 — ISS: lista de serviços (art. 9), alíquota 2%–5% (art. 7),
    base = valor do serviço (art. 2), local de incidência (art. 3)
  * LC 123/2006 — Simples Nacional (anexos I a V)
  * LC 128/2008 — MEI
  * Lei 10.684/2003 — retenção de ISS pelo tomador (2% Simples / 3% demais)
  * IN RFB 1.234/2012 — retenções federais (IR, CSLL, PIS, COFINS)

O matching é determinístico (funciona sem LLM): cada regra tem palavras-
chave normalizadas e um texto de resposta em pt-BR.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .rules import normalize_name


@dataclass(frozen=True)
class FiscalRule:
    title: str
    keywords: tuple[str, ...]
    digest: str  # resumo em 1 linha (contexto p/ o LLM)
    text: str    # resposta completa em pt-BR
    # tópico ancorável nas notas da conversa (difal, icms, iss, pis_cofins, ipi,
    # retencao, aliquota, regime, reducao): pergunta genérica sobre esse tópico
    # vira análise das notas reais + explicação, nunca só a regra genérica
    topic: str | None = None


# --------------------------------------------------------------------------- #
# Regras (respostas prontas, em pt-BR, com base nas normas)
# --------------------------------------------------------------------------- #
RULES: list[FiscalRule] = [
    FiscalRule(
        title="LC 116/2003 — lista de serviços sujeitos ao ISS",
        keywords=("lc 116", "lc116", "lei complementar 116", "lista de servicos",
                  "lista lc 116", "servicos sujeitos ao iss"),
        digest="LC 116/2003 art. 9: lista de 21 grupos de serviços sujeitos ao ISS; cada serviço tem código (ex.: 17.20 contabilidade).",
        text=(
            "📜 LC 116/2003 — a lista de serviços do ISS\n"
            "A Lei Complementar 116/2003 unificou o ISS no Brasil. O art. 9 traz a lista dos "
            "serviços sujeitos ao imposto, em 21 grupos. Principais:\n"
            "• 01 — Informática (desenvolvimento, programação, consultoria, suporte)\n"
            "• 02 — Arquitetura, urbanismo e engenharia\n"
            "• 03 — Serviços jurídicos\n"
            "• 04 — Arte (apresentações, espetáculos)\n"
            "• 05 — Saúde (médico, odontológico, veterinário, farmácia)\n"
            "• 06 — Ensino\n"
            "• 07 — Engenharia, arquitetura, urbanismo, geologia, agronomia, química, física, "
            "biologia, matemática, estatística, mecânica, cartografia, topografia, entre outras\n"
            "• 08 — Esporte (atletas, técnicos)\n"
            "• 09 — Contabilidade\n"
            "• 10 a 16 — Intermediação, imóveis, bens móveis, segurança, alimentação e eventos\n"
            "• 17 — Apoio técnico e administrativo (consultoria, RH, assessoria)\n"
            "• 18 — Publicidade\n"
            "• 19 — Pesquisa e desenvolvimento\n"
            "• 20 — Serviços financeiros (factoring, arrendamento mercantil)\n"
            "• 21 — Outros serviços não relacionados\n\n"
            "Cada serviço tem um código (ex.: 17.20 — contabilidade). Código + cidade definem a "
            "alíquota de ISS — é assim que eu confiro o cálculo das suas notas."
        ),
    ),
    FiscalRule(
        title="ISS — o que é",
        keywords=("iss", "issqn", "imposto sobre servicos", "imposto municipal",
                  "o que e iss", "iss e o que", "sobre iss", "duvida sobre iss",
                  "pergunta sobre iss", "explica iss", "o imposto iss",
                  "duvida de iss", "duvida do iss", "tirar duvida sobre iss",
                  "quem paga o iss", "quem paga iss"),
        digest="ISS: imposto municipal sobre serviços da LC 116/2003; alíquota 2%–5% definida pelo município; base = valor do serviço; incide no município do tomador; retenção 2% (Simples) ou 3%.",
        text=(
            "🏛️ ISS — o imposto municipal sobre serviços\n"
            "• O ISS (Imposto Sobre Serviços) é MUNICIPAL e incide sobre os serviços da lista "
            "da LC 116/2003 (21 grupos: TI, engenharia, advocacia, contabilidade, saúde, "
            "educação, eventos, etc.).\n"
            "• Alíquota: de 2% a 5% — cada MUNICÍPIO define a alíquota de cada código de serviço.\n"
            "• Base de cálculo: o valor total do serviço.\n"
            "• Onde incide: no município do TOMADOR (quem contrata) — é lá que o ISS se paga.\n"
            "• Quem paga: o PRESTADOR do serviço é o contribuinte de verdade; o tomador pode "
            "reter na fonte (2% — Simples Nacional, ou 3% — demais regimes) e entregar ao fisco.\n"
            "• Serviços pagam ISS — mercadorias é que pagam ICMS.\n\n"
            "Posso detalhar qualquer um desses pontos (alíquota por cidade/código, retenções, "
            "base de cálculo, local de incidência) ou conferir o ISS de uma nota sua — "
            "me diga o número dela."
        ),
        topic="iss",
    ),
    FiscalRule(
        title="Alíquota do ISS (2% a 5%)",
        keywords=("aliquota iss", "aliquota do iss", "aliquota de iss", "quanto e o iss",
                  "percentual do iss", "tabela de aliquotas", "aliquota issqn",
                  "o iss e de quanto", "iss e de quanto"),
        digest="ISS: alíquota de 2% a 5% (LC 116 art. 7), definida pelo município; base = valor do serviço.",
        text=(
            "💰 Alíquota do ISS (LC 116/2003, art. 7)\n"
            "• Faixa nacional: 2% a 5% — cada MUNICÍPIO define a alíquota de cada serviço.\n"
            "• Base de cálculo: o valor total do serviço (art. 2).\n"
            "• Exemplos da minha tabela (SP):\n"
            "  - 17.20 (contabilidade): 3%\n"
            "  - 01.07 (TI): 2,9% em SP capital (incentivo)\n"
            "  - 17.19 (outros apoios): 2,5% em SP capital; 2% em Campinas, Sorocaba, Jundiaí e Osasco\n"
            "  - 07.02 (engenharia/construção): 5% no padrão, 3% em Campinas\n"
            "• Cidades na tabela: São Paulo, Campinas, Sorocaba, Jundiaí, Osasco (demais: padrão 3%).\n\n"
            "Se me disser a cidade + o código do serviço, eu calculo a alíquota exata."
        ),
        topic="iss",
    ),
    FiscalRule(
        title="Base de cálculo do ISS / ICMS",
        keywords=("base de calculo do iss", "base do iss", "base de iss",
                  "sobre o que incide o iss", "base de calculo"),
        digest="Base do ISS = valor total do serviço (LC 116 art. 2); base do ICMS = valor da operação (mercadoria + frete/seguro/outras).",
        text=(
            "🧮 Base de cálculo\n"
            "• ISS: a base é o VALOR TOTAL DO SERVIÇO (LC 116/2003, art. 2) — o preço cobrado.\n"
            "• Retenção na fonte NÃO reduz a base: só adianta a cobrança (quem retém entrega ao fisco).\n"
            "• ICMS: a base é o valor da operação — mercadoria + frete + seguro + outras despesas "
            "acessórias (descontos condicionais reduzem a base).\n"
            "• Em NFS-e com base mascarada (*), eu derivo a base pelo valor do serviço e confiro "
            "base × alíquota = valor do imposto."
        ),
    ),
    FiscalRule(
        title="Local de incidência do ISS",
        keywords=("local de incidencia", "onde incide", "municipio do iss",
                  "onde pagar o iss", "onde e cobrado o iss",
                  "servico prestado em outro municipio", "servico em outro municipio",
                  "imposto sobre servico prestado",
                  "prestacao de servico em outro municipio"),
        digest="ISS incide onde está o tomador (LC 116 art. 3, II); exceções: tomador no exterior e serviços ligados a imóvel.",
        text=(
            "📍 Onde incide o ISS (LC 116/2003, art. 3)\n"
            "• Regra geral: onde está o TOMADOR (quem contrata o serviço) — é o município dele que cobra.\n"
            "• Exceções: serviço prestado a tomador no exterior (incide no município do prestador) e "
            "serviços ligados a imóvel (incide onde o imóvel está).\n"
            "• Por isso a nota mostra o 'município da incidência' — é com ele que a alíquota da "
            "tabela é conferida."
        ),
    ),
    FiscalRule(
        title="Retenções na fonte",
        keywords=("retencao", "retencoes", "retido", "retido na fonte", "retencao na fonte",
                  "ir", "irrf"),
        digest="Retenções: IR 1,5% (jurídicos) ou 3%; CSLL 1% ou 3%; PIS 1,65%; COFINS 7,6%; ISS 2% (Simples) ou 3%.",
        text=(
            "✂️ Retenções na fonte (IN RFB 1.234/2012 + Lei 10.684/2003)\n"
            "Sobre serviços, o tomador pode reter:\n"
            "• IR: 1,5% (serviços jurídicos) ou 3% (demais)\n"
            "• CSLL: 1% (jurídicos) ou 3% (demais)\n"
            "• PIS: 1,65%   •  COFINS: 7,6%\n"
            "• ISS: 2% (prestador no Simples Nacional) ou 3% (demais regimes)\n\n"
            "A retenção não altera a base do imposto — só muda quem entrega ao fisco."
        ),
        topic="retencao",
    ),
    FiscalRule(
        title="ICMS",
        keywords=("icms", "imposto de circulacao de mercadorias"),
        digest="ICMS: estadual, incide sobre mercadorias e transportes; SP interno 18%; interestadual 4% (importados), 7% (origem Sul/Sudeste exceto ES -> Norte, Nordeste, Centro-Oeste ou ES), 12% (demais casos); destacado, não soma no total.",
        text=(
            "🚚 ICMS — o imposto estadual\n"
            "• Incide sobre CIRCULAÇÃO DE MERCADORIAS e transportes/comunicação interestadual — "
            "não sobre serviços da lista LC 116 (esses pagam ISS).\n"
            "• Alíquota interna (SP): 18%.\n"
            "• Interestadual (Res. SF 22/1989 e 13/2012): 4% para bens importados; "
            "7% quando a origem é do Sul/Sudeste (PR, SC, RS, SP, RJ, MG) e o "
            "destino é Norte, Nordeste, Centro-Oeste ou ES; 12% nos demais casos "
            "(inclui operações entre estados do Sul/Sudeste).\n"
            "• Na NFe, o ICMS é DESTACADO (não soma no total): "
            "vNF = produtos + frete/seguro/outras + IPI − descontos."
        ),
        topic="icms",
    ),
    FiscalRule(
        title="ICMS — sobre o que incide (quais produtos/mercadorias)",
        keywords=("tipos de produto", "tipo de produto", "preciso pagar icms",
                  "que produtos pagam icms", "quais produtos pagam icms",
                  "produtos sujeitos a icms", "sobre o que incide o icms",
                  "o que incide icms", "quando incide icms", "quem paga icms",
                  "que mercadorias pagam icms", "icms incide sobre", "o icms incide",
                  "produtos e icms", "em que produtos", "para quais produtos",
                  "sobre quais produtos", "meu produto", "meus produtos",
                  "produto paga icms", "produtos pagam icms", "tenho que pagar icms",
                  "quando eu pago icms", "em que casos pago icms", "o que e mercadoria",
                  "o que e considerado mercadoria", "quais mercadorias",
                  "mercadorias pagam icms", "vendo produtos", "compro produtos",
                  "icms sobre produtos", "pago icms", "icms do produto",
                  "icms da mercadoria"),
        digest="ICMS incide sobre a circulação de MERCADORIAS (praticamente todos os bens móveis: alimentos, vestuário, eletrônicos, veículos, insumos, cosméticos...) e transportes/comunicação interestadual; alíquota padrão 18% (SP) e reduzidas (alimentos, livros, informática); não incide sobre serviços LC 116 (ISS) nem imóveis (IT); muitos produtos entram com ICMS-ST.",
        text=(
            "🛒 Sobre o que incide o ICMS (quais produtos)\n"
            "• O ICMS incide sobre a CIRCULAÇÃO DE MERCADORIAS — na prática, sobre "
            "praticamente TODOS os bens móveis que são comprados e vendidos:\n"
            "  - Alimentos e bebidas (varejo e atacado)\n"
            "  - Vestuário, calçados, acessórios\n"
            "  - Móveis, eletrodomésticos, decoração\n"
            "  - Eletrônicos, informática, celulares e acessórios\n"
            "  - Veículos, motos, peças e autopeças\n"
            "  - Insumos e matérias-primas, combustíveis, lubrificantes\n"
            "  - Cosméticos, perfumaria, higiene pessoal\n"
            "  - Livros, papelaria, materiais de construção\n"
            "  - Máquinas, equipamentos e ferramentas\n"
            "  - Medicamentos (em regra, alíquota reduzida ou isenção)\n"
            "• Também incide sobre TRANSPORTES e COMUNICAÇÃO interestadual/internacional "
            "(carga, telecomunicações).\n"
            "• Alíquotas:\n"
            "  - Padrão (SP): 18% interno; interestadual 4% (bens importados), 7% "
            "(origem no Sul/Sudeste exceto ES com destino no Norte, Nordeste, "
            "Centro-Oeste ou ES) e 12% nos demais casos.\n"
            "  - Reduzidas (convênios/leis estaduais): alimentos (12-13%), livros e papel "
            "(em regra 12-13%), bens de informática (12-13%), energia elétrica, entre "
            "outros.\n"
            "  - Isenções: medicamentos, leite in natura e alguns insumos agrícolas "
            "(varia por estado).\n"
            "• NÃO incide sobre:\n"
            "  - SERVIÇOS da lista LC 116/2003 (TI, advocacia, contabilidade, saúde, "
            "construção...) — esses pagam ISS, não ICMS.\n"
            "  - IMÓVEIS (venda de casa/apartamento) — aí é IT (imposto municipal).\n"
            "  - Exportação (em regra, isenta).\n"
            "• Na prática:\n"
            "  - Vende produto (comércio/indústria) → ICMS em cada venda, destacado na "
            "NFe/NFCe (varejo para consumidor final = NFCe).\n"
            "  - Muitos produtos comuns (bebidas, combustíveis, eletrônicos, veículos, "
            "cosméticos, alimentos processados) entram com ICMS-ST — o imposto de toda a "
            "cadeia já vem pago por antecipação; pergunte 'o que é ICMS-ST' ou "
            "'quais produtos pagam ST' que eu detalho.\n"
            "• Resumindo: é PRODUTO/MERCADORIA que circula → tem ICMS. É SERVIÇO da "
            "LC 116 → tem ISS. Me diga o produto (ou o número da nota) que eu confiro a "
            "alíquota e o cálculo."
        ),
        topic="icms",
    ),
    FiscalRule(
        title="ICMS-ST (substituição tributária)",
        keywords=("substituicao tributaria", "icms st", "st de icms",
                  "substituicao", "o que e st", "o que e icms st", "st e o que",
                  "icms st e o que", "como funciona a substituicao", "como funciona o st",
                  "quem e o substituto", "substituto tributario", "antecipacao do icms",
                  "mecanismo de obrigatoriedade", "moc", "st", "substituicao do icms",
                  "icms de substituicao", "substituicao tributaria do icms",
                  "st na entrada", "st na saida", "credito de st", "credito do st",
                  "quem paga o st", "quem paga icms st", "quem paga o icms st",
                  "como calcula o st", "como calcula o icms st",
                  "como e calculado o st", "como e calculado o icms st",
                  "calcular o st", "calcular o icms st", "calcular icms st",
                  "quanto e o icms st", "quanto e o st", "mva",
                  "margem de valor adicionado", "margem de valor agregado",
                  "aliquota do icms st", "aliquota do st", "aliquota icms st",
                  "st na nfe", "icms st na nfe", "st na nota", "icms st na nota",
                  "tem st", "tem icms st", "o st e o que", "st significa",
                  "o que significa st", "icms antecipado", "tributo antecipado",
                  "diferenca entre icms e st", "diferenca entre icms e icms st",
                  "diferenca entre difal e st", "diferenca entre difal e icms st",
                  "difal e st", "st e difal", "st interestadual",
                  "icms st interestadual", "partilha do st", "st e obrigatorio",
                  "substituido tributario", "quem e o substituido",
                  "preciso pagar icms st", "tenho que pagar icms st",
                  "pago icms st", "pagar icms st", "st no faturamento"),
        digest="ICMS-ST: ICMS de toda a cadeia cobrado por antecipação por um substituto (produtor/importador/designado); calculado com MVA (margem de valor adicionado) sobre a base; comprador não paga ICMS na entrada/saída; cST 40/41/50/60; soma no total da NFe; contribuinte tem crédito, Simples não; venda a consumidor final não contribuinte: vendedor cobra o DIFAL por antecipação; ST interestadual com partilha (Conv. ICMS 68/19).",
        text=(
            "🔄 ICMS-ST (substituição tributária)\n"
            "• O que é: o estado ANTECIPA a cobrança do ICMS de TODA a cadeia de "
            "circulação. Em vez de cada elo (distribuidor, atacadista, varejista) pagar "
            "seu ICMS, um único SUBSTITUTO TRIBUTÁRIO paga tudo de uma vez.\n"
            "• Quem é o substituto: geralmente o PRODUTOR, FABRICANTE ou IMPORTADOR da "
            "mercadoria (o estado designa quem antecipa, por lei/convênio). O comprador é "
            "o 'substituído'.\n"
            "• Como funciona na prática:\n"
            "  - A mercadoria chega ao comprador com o ICMS-ST já destacado na NFe — ele "
            "NÃO paga ICMS na entrada (já foi pago pelo substituto) nem na venda/saída.\n"
            "  - O substituto recolhe o ST do fisco e abate o que já foi pago na cadeia.\n"
            "• Como é calculado (MVA): a alíquota é a do ICMS do estado (SP: 18%), mas "
            "incide sobre a base + MVA (margem de valor adicionado), que simula as "
            "margens de lucro da cadeia futura.\n"
            "  Exemplo (simplificado): venda de R$ 1.000, alíquota 18%, MVA 30% → base ST "
            "= 1.000 × 1,30 = R$ 1.300 → ICMS-ST = R$ 234,00 (em vez de R$ 180,00 do "
            "ICMS normal). A MVA varia por produto e estado.\n"
            "• Na NFe: o ICMS-ST é destacado e SOMA no total do documento (diferente do "
            "ICMS normal, que é destacado mas NÃO soma no total); o campo cST fica 40/41/"
            "50/60. Na auditoria eu trato o ST como despesa acessória no valor da nota.\n"
            "• O que o comprador faz:\n"
            "  - Não destaca ICMS na entrada (o ST já foi pago).\n"
            "  - Contribuinte (Presumido/Real) pode tomar CRÉDITO do ST pago — compensa "
            "com o ICMS que vai pagar.\n"
            "  - No Simples Nacional não há crédito: o ST pago fica como custo (pergunte "
            "'ICMS-ST no Simples' que eu detalho).\n"
            "• Em VENDA A CONSUMIDOR FINAL NÃO CONTRIBUINTE (ex.: pessoa física): o "
            "vendedor é obrigado a cobrar o DIFAL por antecipação pelo estado de "
            "destino — na NFe aparece como 'ICMS DIFAL'. É a mesma lógica de "
            "antecipação, aplicada ao DIFAL.\n"
            "• ST interestadual: o ST pode ser partilhado entre origem e destino "
            "(Convênio ICMS 68/19 e afins) — a NFe mostra a partilha.\n"
            "• Produtos comuns em ST (varia por estado): bebidas, combustíveis, "
            "eletrônicos, veículos e autopeças, cosméticos, alimentos processados, "
            "papel, medicamentos, máquinas.\n"
            "• Diferenças rápidas:\n"
            "  - ICMS normal: destacado, NÃO soma no total, pago por cada operação da "
            "cadeia.\n"
            "  - ICMS-ST: antecipado pelo substituto, SOMA no total, cobre a cadeia "
            "inteira de uma vez.\n"
            "  - DIFAL: diferencial de alíquota em operação interestadual — há no "
            "uso/consumo (ou ativo imobilizado) de contribuinte e na venda a "
            "pessoa física/não contribuinte; na revenda não há.\n\n"
            "Se me mandar a nota (número), eu confiro se o ST está correto, qual a MVA "
            "usada e como ele entra no total."
        ),
        topic="icms",
    ),
    FiscalRule(
        title="Produtos sujeitos ao ICMS-ST (substituição tributária)",
        keywords=("produtos com st", "produtos em st", "produtos sujeitos a st",
                  "produtos pagam st", "produtos que pagam st",
                  "que produtos pagam st", "quais produtos pagam st",
                  "tipos de produto com st", "em que produtos pago st",
                  "em que produtos incide st", "em que produtos preciso pagar st",
                  "produtos de st", "produtos em substituicao",
                  "produtos de substituicao", "lista de produtos st",
                  "que produtos pagam icms st", "quais produtos pagam icms st",
                  "produtos pagam icms st", "produtos que pagam icms st",
                  "em que produtos incide icms st", "em que produtos pago icms st",
                  "tipos de produto com icms st", "produtos com icms st",
                  "produtos em icms st"),
        digest="Produtos comuns em ST (varia por estado): bebidas, combustíveis, eletrônicos, veículos/autopeças, cosméticos, alimentos processados, papel, medicamentos, máquinas; na NFe aparece 'ICMS-ST' no total e cST 40/41/50/60; quem compra não paga ICMS na entrada/saída.",
        text=(
            "📦 Produtos sujeitos ao ICMS-ST (substituição tributária)\n"
            "O ST não vale para todos os produtos — o estado designa, por lei/convênio, "
            "quais entram na substituição. Os mais comuns:\n"
            "• Bebidas: refrigerantes, cerveja, sucos, água mineral, licores\n"
            "• Combustíveis e lubrificantes (gasolina, diesel, óleo)\n"
            "• Eletrônicos e informática (celulares, computadores, TVs, acessórios)\n"
            "• Veículos, motos e autopeças\n"
            "• Cosméticos, perfumaria e higiene\n"
            "• Alimentos processados (embalados)\n"
            "• Papel e papelaria\n"
            "• Medicamentos (em muitos estados)\n"
            "• Máquinas e equipamentos\n\n"
            "Como identificar na sua nota:\n"
            "• A NFe destaca 'ICMS-ST' (ou 'ICMS ST') no total — e o campo cST fica 40/41/"
            "50/60.\n"
            "• Se a nota vem com ST, você NÃO paga ICMS na entrada nem na saída — o "
            "substituto (produtor/importador) já pagou tudo por antecipação.\n"
            "• A lista exata varia por ESTADO e por produto — me diga o produto e o "
            "estado que eu te oriento; ou me manda a nota (número) que eu confiro se ela "
            "veio com ST."
        ),
        topic="icms",
    ),
    FiscalRule(
        title="ICMS-ST no Simples Nacional",
        keywords=("st no simples", "icms st no simples", "simples nacional e st",
                  "simples e st", "simples paga st", "paga icms st", "st no das",
                  "icms st no das", "st e simples", "icms st e simples",
                  "st no anexo", "icms st no anexo",
                  "simples nacional paga icms st", "st no simples nacional",
                  "pago st no simples"),
        digest="Simples (Anexos I-III): o ICMS da operação vai dentro do DAS; compra com ST não destaca ICMS na entrada e não gera crédito (o ST vira custo); a saída depende de produto/estado (ST ou sem destaque, ICMS via DAS).",
        text=(
            "🧾 ICMS-ST no Simples Nacional\n"
            "• No Simples (Anexos I, II e III — comércio/indústria), o ICMS da sua "
            "operação vai DENTRO do DAS mensal — você não paga ICMS separado.\n"
            "• Compra com ST: quando você compra mercadoria com ICMS-ST, o imposto de "
            "toda a cadeia já foi pago por antecipação pelo substituto — a nota vem com o "
            "ST no total e você NÃO destaca ICMS na entrada.\n"
            "• Sem crédito: no Simples NÃO há crédito de ICMS — o ST pago na entrada não "
            "compensa nada; ele fica como custo da mercadoria. (No Lucro Presumido/Real, "
            "o ST pago gera crédito.)\n"
            "• Venda: a forma de destacar (ou não) o ICMS/ST na sua NFe de saída depende "
            "do produto e do estado — em muitos casos a operação sai com ST ou sem "
            "destaque, com o ICMS recolhido via DAS.\n"
            "• Resumindo: Simples + ST = o imposto da cadeia já vem pago na nota de "
            "entrada; o seu ICMS segue dentro do DAS, sem crédito.\n\n"
            "Se me mandar a nota (número), eu confiro como o ST entrou no total dela."
        ),
    ),
    FiscalRule(
        title="PIS e COFINS",
        keywords=("pis", "cofins"),
        digest="PIS 0,65% + COFINS 3% (cumulativo, Lucro Presumido) ou COFINS 4% (não cumulativo, Lucro Real); Simples/MEI pagam dentro do DAS.",
        text=(
            "🏛️ PIS e COFINS — federais, sobre a receita\n"
            "• Regime CUMULATIVO (Lucro Presumido): PIS 0,65% + COFINS 3% = 3,65% da receita, sem créditos.\n"
            "• Regime NÃO CUMULATIVO (Lucro Real): PIS 0,65% + COFINS 4% = 4,65%, com créditos sobre insumos.\n"
            "• No Simples Nacional e no MEI, PIS/COFINS já vão dentro da guia única (DAS) — "
            "não se pagam separados."
        ),
        topic="pis_cofins",
    ),
    FiscalRule(
        title="IPI",
        keywords=("ipi", "aliquota de ipi", "aliquota ipi", "ipi aliquota",
                  "ipi de quanto", "quanto e o ipi", "ipi quanto", "tabela de ipi",
                  "tipi", "ipi na nfe", "ipi na fabricacao", "ipi na importacao",
                  "ipi isento", "aliquota zero no ipi", "ipi sobre quais produtos",
                  "quais produtos pagam ipi", "pago ipi", "preciso pagar ipi",
                  "ipi sobre produtos", "credito de ipi", "ipi no simples",
                  "aliquota do ipi"),
        digest="IPI: federal, alíquota por produto na TIPI (pelo NCM), 0% a 30%+; 0% em itens essenciais; na NFe soma no total; crédito para o industrial; serviços não pagam; não entra no DAS do Simples.",
        text=(
            "🏭 IPI — federal, sobre produtos industrializados\n"
            "• Incide na FABRICAÇÃO e na IMPORTAÇÃO de produto industrializado. "
            "Serviços NÃO pagam IPI.\n"
            "• ALÍQUOTA: não é uma alíquota única — é definida por produto na TIPI "
            "(Tabela de Incidência do IPI), consultada pelo código NCM. Varia de 0% a "
            "30%+ (alguns produtos chegam perto de 50%).\n"
            "• Seletividade: 0% nos itens essenciais (cesta básica — arroz, feijão, "
            "farinha —, medicamentos, livros e papel); alíquotas altas em produtos de "
            "luxo e nocivos (ex.: cigarros ~38-40%, automóveis — carro acima de "
            "R$ 400 mil paga IPI adicional de 7%).\n"
            "• Exemplos típicos (confira sempre a TIPI pelo NCM): cerveja ~13%, "
            "refrigerantes ~10%, automóveis 5-16% conforme a motorização.\n"
            "• Na NFe, o IPI é destacado por item e SOMA no total do documento "
            "(diferente do ICMS).\n"
            "• O industrial pode abater o IPI pago nas MATÉRIAS-PRIMAS como crédito "
            "do IPI da venda (sistema de créditos).\n"
            "• No Simples Nacional e no MEI o IPI NÃO vai no DAS: quem fabrica ou "
            "importa paga à parte (DARF) e pode tomar crédito."
        ),
        topic="ipi",
    ),
    FiscalRule(
        title="IRPJ e CSLL",
        keywords=("irpj", "csll", "imposto de renda da empresa", "imposto de renda",
                  "ir", "aliquota de irpj", "aliquota irpj", "aliquota do irpj",
                  "aliquota do ir", "aliquota de ir", "aliquota ir",
                  "aliquota de csll", "aliquota csll", "ir da empresa", "adir",
                  "adicional de irpj", "tabela de irpj",
                  "aliquota do imposto de renda", "o que e ir", "o que e o ir",
                  "ir no simples"),
        digest="IRPJ 15% + ADIR 10% acima de R$ 20 mil/trimestre; CSLL 9% (8% bancos); presumido 8-32%; no Simples/MEI vão no DAS; não confundir com IRRF (pessoa física).",
        text=(
            "💼 IRPJ e CSLL — federais, sobre o lucro da empresa\n"
            "• IRPJ: 15% sobre a renda tributável do trimestre + ADIR: 10% adicionais "
            "somente sobre a parte do trimestre que exceder R$ 20.000 (R$ 80 mil/ano).\n"
            "• CSLL: 9% do lucro (8% para bancos e instituições financeiras).\n"
            "• Regimes de apuração:\n"
            "  - Lucro Real: lucro de fato (receitas − custos/despesas dedutíveis).\n"
            "  - Lucro Presumido: lucro presumido por lei — 8% comércio e indústria, "
            "8% transporte, 32% serviços em geral (20% médicos/técnicos e profissionais "
            "liberais).\n"
            "  - Simples Nacional e MEI: IRPJ e CSLL NÃO têm alíquota própria — já vão "
            "dentro do DAS (alíquota do anexo).\n"
            "• Pagamento trimestral via DARF (no Simples, dentro do DAS); regime "
            "simplificado de apuração para receita de até R$ 180 mil/trimestre.\n"
            "• Não confundir: IRRF/IRPF é o imposto da PESSOA FÍSICA (salário, "
            "pró-labore) — pergunte 'tabela IRRF' para as faixas de 7,5% a 27,5%."
        ),
    ),
    FiscalRule(
        title="IRRF — alíquotas e faixas (IR retido na fonte)",
        keywords=("irrf", "imposto de renda retido", "ir retido", "irrf aliquota",
                  "aliquota irrf", "tabela irrf",
                  "tabela de irrf", "tabela do imposto de renda", "tabela do ir",
                  "tabela de ir", "irrf na folha", "imposto de renda na folha",
                  "ir do salario", "aliquota irrf", "aliquota do irrf",
                  "faixas de irrf", "faixas do ir", "ir na folha",
                  "quanto retira de ir", "imposto de renda na folha de pagamento",
                  "aliquota do ir na folha"),
        digest="IRRF: retenção do IR na fonte; salário: tabela progressiva 7,5-27,5% com parte isenta (atualizada a cada março); serviços de PJ: 1,5% (jurídicos) ou 3%.",
        text=(
            "✂️ IRRF — imposto de renda retido na fonte\n"
            "• Sobre SALÁRIO (folha): retenção mensal pela tabela progressiva "
            "(a Receita atualiza a cada março — confirme a tabela vigente no mês da "
            "folha). Tabela mar/2025:\n"
            "  - até R$ 2.428,80: isento\n"
            "  - até R$ 3.036,00: 7,5%\n"
            "  - até R$ 4.074,57: 15%\n"
            "  - até R$ 5.014,50: 22,5%\n"
            "  - acima de R$ 5.014,50: 27,5%\n"
            "• Aplica-se sobre a base de cálculo (salário − INSS e outras deduções "
            "legais); o 13º salário é tributado como mês normal (nas duas parcelas).\n"
            "• Sobre SERVIÇOS de PJ prestadora: retenção de 1,5% (serviços jurídicos) "
            "ou 3% (demais) — junto com CSLL 1%/3%, PIS 1,65% e COFINS 7,6%.\n"
            "• Quem recebe de PJ sem retenção (ex.: sócio) usa o carnê-leão com a "
            "mesma tabela progressiva."
        ),
    ),
    FiscalRule(
        title="MEI",
        keywords=("mei", "microempreendedor"),
        digest="MEI (LC 128/2008): até R$ 81 mil/ano; DAS mensal fixa por atividade; 1 empregado; acima do limite vira ME no Simples.",
        text=(
            "🏠 MEI (LC 128/2008)\n"
            "• Faturamento de até R$ 81.000/ano.\n"
            "• Paga uma guia MENSAL única (DAS) de valor fixo, que varia por atividade "
            "(comércio/indústria x serviços) e já inclui INSS (5% sobre a receita) e o ICMS/ISS.\n"
            "• Pode ter 1 empregado. Acima do limite, vira ME no Simples Nacional."
         ),
     ),
    FiscalRule(
        title="MEI — estourou o limite de faturamento (R$ 81 mil)",
        keywords=("ultrapassou o limite", "limite de faturamento", "acima do limite",
                  "estourou o limite", "extrapolou o limite", "excedeu o limite",
                  "passou do limite", "limite do mei", "mei ultrapassou",
                  "perdeu o mei", "perde o mei", "81 mil", "81 000",
                  "acima de 81 mil", "acima do limite do mei"),
        digest="MEI acima de R$ 81 mil/ano: excesso de até 10% (R$ 89,1 mil) = carência até 31/12; acima de 10% = vira ME no Simples a partir do mês seguinte ao excesso; declarar na DASN-SIMEI.",
        text=(
            "⚠️ MEI estourou o limite de R$ 81 mil — o que acontece?\n"
            "• Excesso de até 10% (até R$ 89.100): período de carência — você pode "
            "permanecer MEI até 31 de dezembro daquele ano (LC 128/2008, art. 74). No "
            "ano seguinte, se o faturamento voltar ao limite, continua MEI.\n"
            "• Excesso acima de 10%: perde o MEI a partir do MÊS SEGUINTE ao excesso e "
            "vira ME no Simples Nacional (o CNPJ é o mesmo; muda o regime tributário).\n"
            "• Nos dois casos: declare o faturamento real na DASN-SIMEI (declaração "
            "anual, prazo até 31 de maio).\n"
            "• Se virar ME: passa a pagar o DAS do Simples (Anexo III, IV ou V, "
            "conforme a atividade) a partir do mês seguinte ao excesso; o DAS do MEI "
            "é pago até o fim do período em que foi MEI.\n"
            "• O excesso em si não gera multa — muda o regime."
        ),
    ),
    FiscalRule(
        title="MEI + CLT (ou outro vínculo) ao mesmo tempo",
        keywords=("clt", "contrato clt", "clt ao mesmo tempo", "ao mesmo tempo",
                  "mei e clt", "clt e mei", "mei com clt", "clt com mei",
                  "pode ser clt e mei", "pode ser mei e clt",
                  "trabalhar com clt e mei", "simultaneamente",
                  "pensionista", "aposentado", "mei e permitido"),
        digest="Sim: MEI + CLT (ou pensionista/professor) ao mesmo tempo é permitido; o que o MEI não pode é ser sócio/gerente de sociedade limitada nem ter outro CNPJ; as rendas são independentes (CLT retido na fonte + DAS do MEI).",
        text=(
            "✅ MEI + CLT ao mesmo tempo: permitido\n"
            "• Sim, você pode ser MEI e ter um contrato CLT ao mesmo tempo (também "
            "vale para pensionista, professor, servidor). O emprego CLT não impede o "
            "MEI.\n"
            "• O que o MEI NÃO pode: ser sócio ou gerente de sociedade limitada nem "
            "ter outro CNPJ.\n"
            "• As duas rendas são independentes: o salário CLT é tributado na fonte "
            "(IRRF + INSS na folha); o MEI paga o DAS fixo.\n"
            "• Na DIRPF (se obrigado a declarar), informe as duas: a renda CLT "
            "(tributável, com retenção) e a receita do MEI.\n"
            "• CLT (Consolidação das Leis do Trabalho) é o regime de emprego formal "
            "com contrato registrado."
        ),
    ),
    FiscalRule(
        title="Simples Nacional",
        keywords=("simples nacional", "simples", "anexo i", "anexo ii", "anexo iii",
                  "anexo iv", "anexo v"),
        digest="Simples Nacional (LC 123/2006): até R$ 4,8 mi/ano; DAS único; anexos I (4%), II (6%), III (4%), IV (6%), V (15,5%) — progressivos.",
        text=(
            "🧾 Simples Nacional (LC 123/2006)\n"
            "• ME e EPP com faturamento de até R$ 4,8 milhões/ano.\n"
            "• Um único DAS mensal substitui IRPJ, CSLL, PIS, COFINS, INSS e ICMS ou ISS "
            "(conforme o anexo).\n"
            "• Anexos (alíquota inicial, progressiva com o faturamento):\n"
            "  I — comércio e indústria: 4%\n"
            "  II — alimentação/restaurantes: 6%\n"
            "  III — comércio varejista: 4%\n"
            "  IV — serviços profissionais (contabilidade, advocacia, engenharia…): 6%\n"
            "  V — demais serviços: 15,5%\n"
            "• A alíquota sobe por faixas de receita (tabela mensal progressiva).\n"
            "• 'Alíquota de IR no Simples?': não existe alíquota de IRPJ/CSLL à parte — "
            "elas já estão dentro do DAS (alíquota do anexo acima).\n"
            "• 'IPI no Simples?': o IPI NÃO entra no DAS — quem fabrica ou importa "
            "paga à parte (DARF), com crédito do IPI das matérias-primas."
          ),
      ),
    FiscalRule(
        title="Simples Nacional — alíquota por faturamento (serviços, Anexos IV e V)",
        keywords=("aliquota do simples", "aliquota simples nacional",
                  "aliquota para faturamento", "faturamento",
                  "empresa de servicos", "servicos com faturamento",
                  "quanto pago no simples", "quanto paga no simples",
                  "aliquota do anexo", "anexo iv", "anexo v",
                  "simples nacional para",
                  "faturando", "no simples", "300 mil",
                  "aliquota que eu pago", "aliquota que pago"),
        digest="Alíquota do Simples depende do anexo (atividade) e da receita MENSAL: Anexo IV (serviços profissionais) começa em 6%; Anexo V (demais serviços) começa em 15,5%; 1ª faixa até R$ 36 mil/mês; progressivo acima. Ex.: R$ 300 mil/ano = R$ 25 mil/mês -> 6% (Anexo IV) ou 15,5% (Anexo V).",
        text=(
            "📊 Simples Nacional — alíquota por faturamento (serviços)\n"
            "A alíquota depende do ANEXO (atividade) e da receita MENSAL (tabela "
            "progressiva, LC 123/2006):\n"
            "• Anexo IV — serviços profissionais/técnicos (contabilidade, advocacia, "
            "engenharia, medicina, consultoria de TI…): começa em 6%.\n"
            "• Anexo V — demais serviços não listados no Anexo IV (limpeza, segurança, "
            "publicidade, etc.): começa em 15,5%.\n\n"
            "Exemplo — R$ 300.000/ano = R$ 25.000/mês (1ª faixa, até R$ 36.000/mês):\n"
            "• Anexo IV: 6% → DAS ≈ R$ 1.500/mês.\n"
            "• Anexo V: 15,5% → DAS ≈ R$ 3.875/mês.\n\n"
            "Acima de R$ 36.000/mês a alíquota sobe por faixa (tabela progressiva).\n"
            "Me diga a atividade (qual anexo) e o faturamento mensal que eu calculo a "
            "alíquota exata."
        ),
    ),
    FiscalRule(
        title="Simples Nacional — estourou o limite de R$ 4,8 milhões",
        keywords=("limite do simples", "acima do limite do simples",
                  "ultrapassou o limite do simples", "perde o simples",
                  "perdeu o simples", "4 8 milhoes", "4 800 000",
                  "4800000", "acima de 4 8 milhoes",
                  "simples acima do limite"),
        digest="Simples acima de R$ 4,8 mi/ano: excesso de até 10% (R$ 5,28 mi) = carência até 31/12; acima de 10% = perde o Simples a partir do mês seguinte (Lucro Presumido/Real); declarar na DASN-Simples.",
        text=(
            "⚠️ Simples Nacional — estourou o limite de R$ 4,8 milhões\n"
            "• Excesso de até 10% (até R$ 5,28 milhões): período de carência — você "
            "pode permanecer no Simples até 31 de dezembro daquele ano (LC 123/2006, "
            "art. 14).\n"
            "• Excesso acima de 10%: perde o Simples a partir do MÊS SEGUINTE ao "
            "excesso e passa para Lucro Presumido ou Lucro Real (o que for mais "
            "favorável).\n"
            "• Declare o faturamento real na DASN-Simples (declaração anual, prazo "
            "até 31 de maio).\n"
            "• O excesso em si não gera multa — muda o regime."
        ),
    ),
    FiscalRule(
        title="Lucro Presumido",
        keywords=("lucro presumido", "presumido"),
        digest="Lucro Presumido: lucro presumido por lei (ex.: 32% serviços); PIS 0,65% + COFINS 3% cumulativos; IRPJ/CSLL sobre o lucro presumido.",
        text=(
            "📈 Lucro Presumido\n"
            "• Lucro presumido por lei (ex.: 8% no comércio, 8% na indústria, 8% no "
            "transporte, 32% na maioria dos serviços; 20% médicos/técnicos e "
            "profissionais liberais).\n"
            "• PIS 0,65% + COFINS 3% (cumulativo) sobre a receita; IRPJ 15% (+10% ADIR "
            "acima de R$ 20 mil/trimestre) e CSLL 9% sobre o lucro presumido.\n"
            "• ICMS/ISS continuam sobre a operação (não entram no regime)."
        ),
    ),
    FiscalRule(
        title="Lucro Real",
        keywords=("lucro real",),
        digest="Lucro Real: impostos sobre o lucro de fato; PIS 0,65% + COFINS 4% não cumulativos; obrigatório p/ bancos e receita > R$ 78 mi/ano.",
        text=(
            "📊 Lucro Real\n"
            "• Impostos sobre o lucro REAL (receitas − custos e despesas dedutíveis).\n"
            "• PIS 0,65% + COFINS 4% (não cumulativo, com créditos); IRPJ 15% (+10% ADIR "
            "acima de R$ 20 mil/trimestre) e CSLL 9%.\n"
            "• Obrigatório para bancos, seguradoras e empresas com receita acima de "
            "R$ 78 milhões/ano (limite de referência)."
        ),
    ),
    FiscalRule(
        title="Regimes tributários — qual aplicar",
        keywords=("regime", "regime tributario", "regime fiscal", "qual o regime",
                  "diferenca entre os regimes"),
        digest="Regimes (MEI, Simples, Presumido, Real) definem IRPJ/CSLL/PIS/COFINS; não mudam ICMS/ISS da operação.",
        text=(
            "🧭 Regimes tributários — qual aplicar?\n"
            "O regime define COMO a empresa calcula os tributos FEDERAIS (IRPJ, CSLL, PIS, COFINS):\n"
            "• MEI — até R$ 81 mil/ano, guia mensal fixa (DAS).\n"
            "• Simples Nacional — até R$ 4,8 mi/ano, DAS único por anexo (I a V).\n"
            "• Lucro Presumido — lucro presumido por lei; PIS/COFINS cumulativos (3,65%).\n"
            "• Lucro Real — lucro de fato; PIS/COFINS não cumulativos (4,65%).\n\n"
            "Importante: o regime NÃO muda o imposto da operação — mercadoria sempre tem ICMS e "
            "serviço da LC 116 sempre tem ISS."
        ),
        topic="regime",
    ),
    FiscalRule(
        title="ISS por regime (Simples x Presumido x Real)",
        keywords=("iss no regime", "impostos de iss", "iss no simples", "iss no presumido",
                  "iss no real", "regime do iss", "regime muda o iss",
                  "iss depende do regime", "iss muda de regime", "mesmo iss"),
        digest="O valor do ISS e o MESMO em qualquer regime (2-5% do servico, definido pelo municipio); o regime muda a forma de pagar (Simples: dentro do DAS; Presumido/Real: separado/retido) e os tributos federais (IRPJ, CSLL, PIS, COFINS).",
        text=(
            "ISS por regime — Simples x Presumido x Real\n"
            "• O VALOR do ISS e o MESMO em qualquer regime: 2% a 5% do valor do servico, "
            "definido pelo municipio (LC 116/2003). O regime NAO muda o ISS.\n"
            "• O que muda e a FORMA de pagar:\n"
            "  - Simples Nacional: o ISS vai dentro do DAS mensal (guia unica).\n"
            "  - Lucro Presumido/Real: o ISS e pago separadamente — normalmente RETIDO "
            "pelo tomador (2% no Simples, 3% nos demais) e entregue ao municipio.\n"
            "• O que o regime realmente muda sao os tributos FEDERAIS:\n"
            "  - Simples: IRPJ, CSLL, PIS, COFINS e INSS vaem dentro do DAS.\n"
            "  - Presumido: PIS 0,65% + COFINS 3% sobre a receita + IRPJ/CSLL sobre o "
            "lucro presumido.\n\n"
            "Resumo: para o ISS em si, da no mesmo. A diferenca de custo entre os regimes "
            "esta nos tributos federais (IRPJ, CSLL, PIS, COFINS) e na forma de recolher o ISS."
        ),
    ),
    FiscalRule(
        title="Comparacao de regimes (qual paga menos)",
        keywords=("qual regime paga menos", "regime que paga menos", "qual e melhor regime",
                  "melhor regime", "regime mais barato", "pagar menos imposto",
                  "simples ou presumido", "presumido ou simples", "simples ou real",
                  "qual regime e melhor", "vale a pena simples", "vale a pena presumido",
                  "regime que conviene", "mudar de regime"),
        digest="Nao ha regime 'melhor' universal: depende do faturamento, da atividade (anexo) e da margem de lucro. Simples = DAS unico (bom p/ faturamento baixo e margem alta); Presumido/Real = melhor quando a margem real e bem abaixo da presuncao.",
        text=(
            "Qual regime paga menos?\n"
            "Nao existe regime 'melhor' universal — depende de 3 fatores:\n"
            "• FATURAMENTO: ate R$ 81 mil/ano -> MEI; ate R$ 4,8 mi/ano -> Simples; "
            "acima -> Presumido ou Real.\n"
            "• ATIVIDADE: no Simples, o anexo (I a V) define a alíquota inicial (4% a 15,5%).\n"
            "• MARGEM DE LUCRO: o Simples cobra sobre a RECEITA (faturamento). O "
            "Presumido/Real cobra sobre o LUCRO (presumido ou real). Se sua margem de lucro "
            "e bem menor que a presuncao legal (ex.: 32% p/ servicos), o Presumido/Real pode "
            "custar menos.\n\n"
            "Regra pratica: faturamento baixo + margem alta -> Simples. Faturamento alto ou "
            "margem baixa -> Presumido/Real. Para o numero exato, preciso do seu faturamento "
            "anual e da atividade — me passa que eu estimo."
        ),
        topic="regime",
    ),
    FiscalRule(
        title="ICMS x ISS — as diferenças",
        keywords=("diferenca entre icms e iss", "diferenca entre iss e icms",
                  "qual a diferenca entre icms e iss", "icms e iss"),
        digest="ICMS: estadual, sobre mercadorias/transportes, alíquota do estado (SP 18%), NFe/SEFAZ. ISS: municipal, sobre serviços da LC 116, alíquota do município (2-5%), NFS-e. Produto -> ICMS; serviço -> ISS.",
        text=(
            "⚖️ ICMS x ISS — as diferenças\n"
            "• ICMS — imposto ESTADUAL (cobrado pela SEFAZ do estado): incide sobre a "
            "CIRCULAÇÃO DE MERCADORIAS e sobre transportes/comunicação interestadual e "
            "intermunicipal.\n"
            "• ISS — imposto MUNICIPAL (cobrado pela prefeitura): incide sobre os "
            "SERVIÇOS da lista da LC 116/2003 (TI, engenharia, advocacia, "
            "contabilidade, saúde, educação, eventos…).\n"
            "• Alíquota: o ICMS é definido pelo ESTADO (SP: 18% interno; "
            "interestadual 4% para importados, 7% da origem no Sul/Sudeste com "
            "destino no Norte, Nordeste, Centro-Oeste ou ES, 12% nos demais "
            "casos). O ISS é definido pelo MUNICÍPIO (2% a 5% por código de "
            "serviço).\n"
            "• Base de cálculo: ICMS = valor da operação (mercadoria + frete + "
            "seguro). ISS = valor do serviço.\n"
            "• Documento: mercadoria → NFe (SEFAZ). Serviço → NFS-e (município).\n"
            "• Regra prática: vendeu PRODUTO → ICMS. Prestou SERVIÇO → ISS. Mix "
            "(produto + instalação) → os dois, cada um sobre a sua parte."
        ),
    ),
    FiscalRule(
        title="ICMS ou ISS — qual incide",
        keywords=("icms ou iss", "iss ou icms", "icms e iss", "entre icms e iss",
                  "diferenca entre icms e iss", "qual imposto incide", "qual o imposto",
                  "quando incide icms", "quando incide iss", "imposto que incide"),
        digest="Mercadoria/transporte -> ICMS; serviço da LC 116 -> ISS; mix -> ICMS na mercadoria + ISS no serviço.",
        text=(
            "⚖️ ICMS ou ISS? Qual imposto incide?\n"
            "• MERCADORIA (venda de produto) → ICMS (estadual).\n"
            "• SERVIÇO da lista LC 116/2003 → ISS (municipal).\n"
            "• Transporte e comunicação interestadual → ICMS.\n"
            "• Mix (ex.: produto + instalação): ICMS sobre a mercadoria e ISS sobre o serviço — "
            "a nota deve destacar os dois.\n\n"
            "Na auditoria eu verifico qual imposto aparece na nota e se a base/alíquota conferem."
        ),
    ),
    FiscalRule(
        title="NFe x NFS-e x NFCe",
        keywords=("nota fiscal", "nfe", "nfs", "nf-e", "nfs-e",
                  "diferenca entre nfe e nfs", "chave de acesso", "codigo de verificacao"),
        digest="NFe = mercadorias/transporte (ICMS, SEFAZ); NFS-e = serviços LC 116 (ISS, município); NFCe = varejo (ICMS).",
        text=(
            "🧾 NFe x NFS-e x NFCe\n"
            "• NFe — mercadorias e transportes (ICMS, emitida à SEFAZ do estado).\n"
            "• NFS-e — serviços da LC 116 (ISS, emitida ao município).\n"
            "• NFCe — varejo para consumidor final (ICMS).\n\n"
            "A chave de acesso (NFe) e o código de verificação (NFS-e) permitem validar a nota "
            "no portal oficial."
        ),
    ),
    FiscalRule(
        title="Código do serviço (LC 116/2003)",
        keywords=("codigo do servico", "codigo lc 116", "item da lista",
                  "codigo de servico"),
        digest="Código do serviço LC 116 (ex.: 17.20 contabilidade) + município definem a alíquota do ISS (2%–5%).",
        text=(
            "🔢 Código do serviço (LC 116/2003)\n"
            "• Cada serviço da lista tem um código (ex.: 17.20 = contabilidade, "
            "01.01 = desenvolvimento de software).\n"
            "• O código + o município definem a alíquota do ISS (tabela do município, faixa 2%–5%).\n"
            "• Se me disser o código e a cidade, eu busco na tabela e confiro a alíquota da nota."
        ),
    ),
    FiscalRule(
        title="DIFAL (diferencial de alíquota de ICMS)",
        keywords=("difal", "icms difal", "diferencial de aliquota",
                  "diferencial de aliquota de icms", "diferencial de icms",
                  "mof", "icms interestadual", "difal do icms", "difal da nfe",
                  "difal da nota"),
        digest="DIFAL: em operação interestadual de mercadoria, DIFAL = alíquota interna do destino − a interestadual (4% importados; 7% origem Sul/Sudeste exceto ES -> Norte, Nordeste, Centro-Oeste ou ES; 12% demais casos); há DIFAL no uso/consumo (ativo imobilizado) de contribuinte (recolhe o destinatário) e na venda a pessoa física/não contribuinte (recolhe o remetente); revenda não tem DIFAL.",
        text=(
            "🧮 DIFAL — diferencial de alíquota de ICMS (EC 87/2015)\n"
            "• Alíquota interestadual (Res. SF 22/1989 e 13/2012): 4% para bens "
            "importados; 7% quando a origem é do Sul/Sudeste (PR, SC, RS, SP, RJ, MG) "
            "e o destino é Norte, Nordeste, Centro-Oeste ou ES; 12% nos demais casos "
            "(inclui operações entre estados do Sul/Sudeste).\n"
            "• O DIFAL é a DIFERENÇA entre a alíquota interna do estado de DESTINO e a "
            "interestadual, em pontos percentuais. Ex.: PA → MG: interestadual 12%, "
            "interna de MG 18% → DIFAL de 6 pontos percentuais. Ex.: MG → GO: "
            "interestadual 7%, interna de GO 17% → DIFAL de 10 pontos percentuais.\n"
            "• Quando há DIFAL em operação interestadual de mercadoria:\n"
            "  - destinatário CONTRIBUINTE que vai USAR/CONSUMIR ou imobilizar o bem "
            "(ativo imobilizado): há DIFAL — em regra o DESTINATÁRIO recolhe ao "
            "estado de destino;\n"
            "  - destinatário PESSOA FÍSICA ou NÃO CONTRIBUINTE (consumidor final): "
            "há DIFAL — o REMETENTE recolhe para o estado de destino;\n"
            "  - destinatário CONTRIBUINTE que vai REVENDER: não há DIFAL (0%) — o "
            "ICMS interestadual destacado pela origem é aproveitado como crédito.\n"
            "• Na NFe, o DIFAL aparece destacado (\"ICMS DIFAL\" / \"ICMS Interestadual DIFAL\") e "
            "SOMA no total do documento.\n"
            "• Operação interna (mesmo estado) não gera DIFAL.\n\n"
            "Se a pergunta for sobre uma nota específica, me dê o número dela (ex.: "
            "\"o DIFAL da nota 123?\") que eu confiro o que está destacado.\n\n"
            "Confirme a alíquota interna e as regras do estado de destino com seu "
            "contador."
        ),
        topic="difal",
    ),
    FiscalRule(
        title="Redução de base de cálculo (ICMS)",
        keywords=("reducao de base", "reducao da base", "reducao de base de calculo",
                  "base de calculo reduzida", "base reduzida", "reducao icms",
                  "reducao do icms", "reducao de base do icms", "reducao da base do icms",
                  "reducao de base da nfe", "reducao de base da nota"),
        digest="Redução de base (ICMS): benefício estadual (lei/convênio) que reduz a base de cálculo do ICMS; alíquota igual, base menor; não é desconto e não altera o total da nota.",
        text=(
            "📉 Redução de base de cálculo (ICMS)\n"
            "• É um BENEFÍCIO concedido pelo estado (lei estadual ou convênio) que reduz a BASE de "
            "cálculo do ICMS — a alíquota continua a mesma, mas incide sobre uma base menor.\n"
            "• Exemplo: operação de R$ 1.000,00 com redução de 50% → base R$ 500,00 → ICMS 18% = "
            "R$ 90,00 (em vez de R$ 180,00).\n"
            "• Na NFe, o sinal é a \"base de cálculo\" do ICMS MENOR que o valor da operação — é "
            "assim que eu detecto na auditoria das notas.\n"
            "• Diferenças importantes:\n"
            "  - Desconto INCONDICIONAL também reduz a base; desconto CONDICIONAL não reduz.\n"
            "  - Redução de ALÍQUOTA (ex.: 12% para bens de informática) muda o percentual, não a base.\n"
            "• A redução não altera o total da nota — só o ICMS devido.\n\n"
            "Para saber se uma nota sua teve redução, pergunte: \"houve redução de base na nota 123?\""
        ),
        topic="reducao",
    ),
    FiscalRule(
        title="Alíquotas principais (resumo)",
        keywords=("aliquotas", "aliquota", "tabela de aliquotas", "quais as aliquotas",
                  "aliquota de icms", "aliquota do icms", "aliquota de pis",
                  "aliquota de cofins"),
        digest="Resumo: ICMS SP 18% interno / interestadual 4% (importados), 7% (Sul/Sudeste exceto ES -> Norte, Nordeste, Centro-Oeste ou ES), 12% (demais); ISS 2-5% (município); PIS 0,65%; COFINS 3%/4%; IPI 0-30%+ (TIPI); IRPJ 15%+10% (ADIR); CSLL 9%; IRRF 7,5-27,5% (tabela).",
        text=(
            "📊 Alíquotas principais (resumo)\n"
            "• ICMS (SP): 18% interno; interestadual 4% (bens importados), 7% (origem "
            "no Sul/Sudeste exceto ES com destino no Norte, Nordeste, Centro-Oeste ou "
            "ES), 12% (demais casos); DIFAL = diferença até a alíquota interna do "
            "destino (uso/consumo ou ativo imobilizado de contribuinte e venda a "
            "pessoa física/não contribuinte; revenda não tem DIFAL).\n"
            "• ISS: 2% a 5% — definida pelo MUNICÍPIO para cada código LC 116/2003 "
            "(minha tabela cobre SP, Campinas, Sorocaba, Jundiaí e Osasco).\n"
            "• PIS: 0,65% da receita.  COFINS: 3% (cumulativo) ou 4% (não cumulativo).\n"
            "• IPI: por produto (TIPI, pelo NCM), de 0% a 30%+ — pergunte 'alíquota de IPI' "
            "para detalhes.\n"
            "• IRPJ: 15% (+10% ADIR acima de R$ 20 mil/trimestre).  CSLL: 9%.\n"
            "• IRRF (salário): faixas de 7,5% a 27,5% com parte isenta — pergunte "
            "'tabela IRRF' para os valores.\n\n"
            "Quer a alíquota exata de uma nota? Me diga o número dela (ou envie a foto) que eu "
            "busco na tabela e confiro o cálculo."
        ),
        topic="aliquota",
    ),
    FiscalRule(
        title="Guias de pagamento (DAS, DARF, GPS)",
        keywords=("das", "darf", "gps", "guia de pagamento", "guia mensal",
                  "guia unica", "o que e o das", "o que e o darf", "o que e o gps",
                  "como pagar o das", "vencimento do das"),
        digest="Guias: DAS = guia única mensal do Simples Nacional/MEI (IRPJ, CSLL, PIS, COFINS, INSS e ICMS/ISS); DARF = guia federal (fora do Simples); GPS = antiga guia do MEI.",
        text=(
            "🧾 Guias de pagamento de tributos\n"
            "• DAS — guia ÚNICA mensal do Simples Nacional e do MEI: reúne IRPJ, CSLL, PIS, "
            "COFINS, INSS e ICMS ou ISS em um único pagamento.\n"
            "• DARF — guia federal: IRPJ, CSLL, PIS, COFINS, IPI e outros tributos federais "
            "(usada por quem NÃO é do Simples).\n"
            "• GPS — antiga guia do MEI; hoje o MEI paga pelo DAS.\n\n"
            "A guia é gerada no portal do governo (Simples Nacional / e-CAC / Meu Imposto de "
            "Renda) e sai com código de barras para pagar em qualquer banco ou no app do banco. "
            "O vencimento costuma ser dia 20 de cada mês (DAS)."
        ),
    ),
    FiscalRule(
        title="INSS (previdência)",
        keywords=("inss", "previdencia", "contribuicao previdenciaria",
                  "aliquota inss", "aliquota do inss", "inss do funcionario",
                  "inss na folha"),
        digest="INSS: contribuição previdenciária; CLT em faixas progressivas (7,5% a 14%); MEI 5% da receita no DAS; Simples dentro do DAS; Presumido/Real 20% patronal sobre a folha.",
        text=(
            "🏥 INSS — contribuição previdenciária\n"
            "• É a contribuição para a previdência social (aposentadoria, auxílio-doença, "
            "salário-maternidade, 13º).\n"
            "• Empregado CLT: desconto em faixas progressivas do salário (de 7,5% a 14%).\n"
            "• MEI: 5% da receita mensal, já incluído no DAS.\n"
            "• Simples Nacional: vai dentro do DAS (conforme o anexo).\n"
            "• Lucro Presumido/Real: alíquota patronal de 20% sobre a folha de salários "
            "(além do desconto do empregado).\n\n"
            "O INSS não aparece destacado em NFe/NFS-e — ele é pago via guia (DAS/DARF) ou "
            "retido na folha."
        ),
    ),
    FiscalRule(
        title="IPTU (imposto sobre imóvel)",
        keywords=("iptu", "imposto predial", "imposto sobre propriedade",
                  "imposto do imovel", "imposto do aluguel"),
        digest="IPTU: imposto municipal anual sobre propriedade de imóvel urbano; pago pelo proprietário; não incide em NFe/NFS-e.",
        text=(
            "🏢 IPTU — Imposto Predial e Territorial Urbano\n"
            "• Imposto MUNICIPAL sobre a propriedade de imóvel (terreno ou edificação) em área "
            "urbana — cobrado anualmente pela prefeitura.\n"
            "• Quem paga: o PROPRIETÁRIO do imóvel. (O contrato de locação pode transferir o "
            "pagamento ao inquilino, mas a obrigação é do dono.)\n"
            "• Geralmente pago em 12 parcelas mensais ou à vista com desconto.\n"
            "• Não é imposto sobre operação comercial — não aparece em NFe/NFS-e nem na "
            "conciliação de faturas."
        ),
    ),
    FiscalRule(
        title="IRPF (imposto de renda pessoa física)",
        keywords=("irpf", "imposto de renda pessoa fisica", "imposto de renda do funcionario",
                  "imposto de renda do salario", "carne leao", "carnê leao",
                  "imposto de renda individual", "tabela irpf", "tabela de irpf"),
        digest="IRPF: imposto sobre renda de pessoas físicas; tabela progressiva mensal (isenta até R$ 2.428,80 em 2025, alíquotas 7,5% a 27,5%); carnê-leão para quem recebe sem desconto na fonte.",
        text=(
            "👤 IRPF — imposto de renda da pessoa física\n"
            "• Incide sobre a renda das PESSOAS FÍSICAS: salários, aluguéis, pró-labore, "
            "investimentos.\n"
            "• Tabela progressiva mensal: parte isenta + alíquotas de 7,5% a 27,5% (a "
            "tabela é atualizada a cada março pela Receita — valores completos: pergunte "
            "'tabela IRRF').\n"
            "• Salário de CLT: o empregador DESCONTA na fonte (aparece como IRRF na folha) e "
            "entrega à Receita.\n"
            "• Carnê-leão: quem recebe de pessoa jurídica SEM desconto na fonte (ex.: sócio "
            "prestador de serviço) declara e paga mensalmente.\n"
            "• A declaração anual (DIRPF) concilia tudo no ano seguinte."
          ),
      ),
    FiscalRule(
        title="IRPF — cálculo para autônomo (carnê-leão)",
        keywords=("como calcular o irpf", "calcular o irpf", "calcular irpf",
                  "irpf de um autonomo", "irpf do autonomo", "irpf de autonomo",
                  "imposto de renda do autonomo", "irrf de autonomo",
                  "quanto pago de irpf", "quanto e o irpf",
                  "irpf de um mes", "carne leao quanto",
                  "irrf de um mes"),
        digest="Autônomo sem retenção paga carnê-leão: tabela progressiva mensal (2024: isento até R$ 2.428,80; 7,5% a 27,5%). Ex.: R$ 8.500 -> faixa 27,5%: 8.500 x 0,275 - 1.357,58 = R$ 979,92; DARF até o dia 20 do mês seguinte.",
        text=(
            "🧮 IRPF — cálculo para autônomo (carnê-leão)\n"
            "Autônomo que recebe de empresa SEM retenção na fonte paga pelo carnê-leão: "
            "aplica a tabela progressiva MENSAL sobre a renda do mês.\n\n"
            "Tabela mensal vigente (atualizada anualmente pela RFB):\n"
            "• Até R$ 2.428,80: isento\n"
            "• R$ 2.428,81 a R$ 4.664,68: 7,5% (parcela a deduzir R$ 182,16)\n"
            "• R$ 4.664,69 a R$ 6.106,69: 15% (parcela a deduzir R$ 532,01)\n"
            "• R$ 6.106,70 a R$ 7.351,37: 22,5% (parcela a deduzir R$ 990,01)\n"
            "• Acima de R$ 7.351,37: 27,5% (parcela a deduzir R$ 1.357,58)\n\n"
            "Exemplo — R$ 8.500 no mês, sem outras deduções:\n"
            "• 8.500 × 27,5% = 2.337,50\n"
            "• 2.337,50 − 1.357,58 (parcela a deduzir) = R$ 979,92 de IRPF\n"
            "• Pague pelo DARF do carnê-leão até o dia 20 do mês seguinte e informe na "
            "DIRPF anual.\n\n"
            "Obs.: a tabela é atualizada anualmente pela RFB — confira a do ano vigente."
        ),
    ),
    FiscalRule(
        title="Rescisão trabalhista e IRPF",
        keywords=("rescisao", "rescisao trabalhista", "valor de rescisao",
                  "rescisao entra no irpf", "base de calculo do irpf",
                  "imposto de renda da rescisao", "aviso previo",
                  "multa de 40", "indenizacao", "fgts",
                  "rescisao e isenta", "irrf da rescisao"),
        digest="Rescisão: a parte indenizatória (aviso prévio indenizado, multa 40% FGTS, indenização por dispensa sem justa causa) é ISENTA (Lei 7.713/88 art. 6 V) e não entra na base do IRPF; a parte salarial (salários, férias + 1/3, 13º) é tributável — o empregador retém IR sobre o total e o empregado pede restituição na DIRPF.",
        text=(
            "📄 Rescisão trabalhista e IRPF\n"
            "O valor da rescisão se divide em duas partes:\n"
            "• ISENTA (indenizatória): aviso prévio indenizado, multa de 40% do FGTS e "
            "indenização por dispensa sem justa causa — NÃO entram na base de cálculo do "
            "IRPF (Lei 7.713/88, art. 6º, V). Declarar na DIRPF na seção \"Rendimentos "
            "Isentos e Não Tributáveis\".\n"
            "• TRIBUTÁVEL (salarial): salários do mês trabalhado, férias não gozadas + "
            "1/3 e 13º proporcional — são rendas sujeitas a IR.\n"
            "• Na prática: o empregador retém o IR sobre o TOTAL da rescisão (tabela "
            "mensal) e você pode pedir RESTITUIÇÃO na DIRPF do IR pago sobre a parte "
            "isenta.\n"
            "• Resumindo: a parte isenta não entra na base do IRPF; a parte salarial já "
            "é tributada na fonte (entra no total anual, mas o imposto já foi retido)."
        ),
    ),
    FiscalRule(
        title="Ganho de capital — venda de imóvel",
        keywords=("ganho de capital", "venda de imovel", "vendi um imovel",
                  "vendi imovel", "imovel com lucro", "lucro na venda de imovel",
                  "declarar ganho de capital", "imposto de ganho de capital",
                  "igc", "venda de imovel com lucro", "imovel", "imoveis"),
        digest="IGC = 15% do ganho (preço de venda - custo de aquisição + melhorias); imóvel: custo pelo valor real (sem correção); isenção p/ venda de imóvel até R$ 440 mil no mesmo ano; acima disso, DARF até o 2º mês + DIRPF; venda acima de R$ 350 mil exige DIRPF.",
        text=(
            "🏠 Ganho de capital — venda de imóvel\n"
            "• Ganho de capital (IGC) = 15% do LUCRO: preço de venda − custo de "
            "aquisição (+ melhorias comprovadas com notas/faturas).\n"
            "• Imóvel: o custo de aquisição é o valor REAL pago (sem correção "
            "monetária).\n"
            "• ISENÇÃO: venda de imóvel por valor total de até R$ 440.000 no mesmo "
            "ano-calendário → não há IGC (Lei 11.196/2005, art. 39).\n"
            "• Se a venda for acima de R$ 440 mil e houver ganho: pague 15% do ganho "
            "pelo DARF (código de IGC) até o último dia útil do 2º mês seguinte à "
            "venda.\n"
            "• Declare na DIRPF: o imóvel em \"Bens e Direitos\" (ano da venda) e o "
            "ganho na seção \"Ganho de Capital\".\n"
            "• Venda de imóvel por mais de R$ 350 mil (2023/2024) exige DIRPF mesmo "
            "sem ganho."
        ),
    ),
    FiscalRule(
        title="Ganho de capital — ações e FIIs",
        keywords=("ganho de capital em acoes", "venda de acoes",
                  "venda de acoes com lucro", "lucro com acoes",
                  "imposto de acoes", "imposto de renda de acoes",
                  "imposto de fiis", "fiis", "fundo imobiliario",
                  "acoes na dirpf", "vendi acoes", "venda de quotas"),
        digest="Ações (B3): venda até R$ 20 mil no mesmo mês é isenta; acima disso, 15% do ganho retido na fonte pela corretora. FIIs: 20% de IGC na venda de quotas (DARF) e 20% nas distribuições (carnê-leão); declarar na DIRPF.",
        text=(
            "📈 Ganho de capital — ações e FIIs\n"
            "• Ações (B3): venda de até R$ 20.000 no mesmo mês → ISENTO. Acima disso: "
            "15% do ganho, RETIDO NA FONTE pela corretora (aparece no informe de "
            "rendimentos; sem DARF separado).\n"
            "• FIIs: venda de quotas → IGC de 20% (DARF, 2º mês). Distribuições "
            "mensais → 20% via carnê-leão (quando não retido).\n"
            "• Declare na DIRPF: a venda em \"Ganho de Capital\" (ações) e as "
            "distribuições de FII em \"Rendimentos Tributáveis\"."
        ),
    ),
    FiscalRule(
        title="DIRPF — quem declara e quando",
        keywords=("dirpf", "declaracao de imposto de renda",
                  "declaracao de renda", "declarar imposto de renda",
                  "quem precisa declarar", "quem deve declarar",
                  "prazo da dirpf", "declaracao anual",
                  "imposto de renda anual", "declaracao de 2024"),
        digest="DIRPF: prazo 31 de março; quem declara: renda tributável acima do limite anual (~R$ 30 mil, atualizado pela RFB), renda isenta acima de R$ 40 mil, venda de imóvel acima de R$ 350 mil, ganhos de capital, rendas no exterior.",
        text=(
            "📝 DIRPF — quem declara e quando\n"
            "• Prazo: 31 de março (declaração anual do ano anterior).\n"
            "• Quem precisa declarar (principais critérios):\n"
            "  - Recebeu renda TRIBUTÁVEL acima do limite anual (cerca de R$ 30 mil — "
            "atualizado a cada ano pela RFB);\n"
            "  - Recebeu renda ISENTA acima de R$ 40.000;\n"
            "  - Vendeu ou adquiriu IMÓVEL (venda acima de R$ 350 mil obriga a "
            "declaração);\n"
            "  - Teve GANHO DE CAPITAL (venda de ações, imóveis, FIIs…);\n"
            "  - Recebeu renda no exterior ou é residente no Brasil com bens no "
            "exterior.\n"
            "• Os limites exatos são atualizados anualmente pela RFB."
        ),
    ),
    FiscalRule(
        title="Multa e juros por atraso no pagamento",
        keywords=("multa", "juros de mora", "multa de mora", "multa por atraso",
                  "atraso de imposto", "imposto atrasado", "paguei atrasado",
                  "atraso no pagamento", "quanto e a multa",
                  "atraso no das", "atraso no icms", "atraso no iss",
                  "atraso no pis", "atraso no cofins", "atraso no irpj",
                  "atraso no csll", "atraso no iptu", "atraso no inss"),
        digest="Atraso no pagamento: federais com multa de mora 0,33% ao mês (teto 20%) + juros SELIC (Lei 9.430/96); ICMS e ISS com regras próprias de cada estado/município (SP: 0,33% ao mês + IPCA-E).",
        text=(
            "⏰ Multa e juros por atraso no pagamento\n"
            "• Tributos FEDERAIS (IRPJ, CSLL, PIS, COFINS, IPI): multa de mora de 0,33% ao mês "
            "(ou fração de até 15 dias), com teto de 20% do valor do imposto, + juros SELIC ao mês "
            "(Lei 9.430/96).\n"
            "• ICMS (estadual): cada estado tem regra própria — em SP, multa de 0,33% ao mês + "
            "juros IPCA-E (Lei 6.374/89).\n"
            "• ISS (municipal): cada MUNICÍPIO tem regra própria (geralmente multa + juros).\n"
            "• O valor exato sai na guia: no e-CAC (federal) ou no portal da SEFAZ/prefeitura — "
            "quanto antes pagar, menos multa e juros acumulados.\n\n"
            "Se quiser, me diz qual imposto atrasou e o valor que eu estimo a multa + juros."
        ),
    ),
    FiscalRule(
        title="Parcelamento de débitos",
        keywords=("parcelamento", "parcelar", "parcelas", "parcelamento de imposto",
                  "parcelamento do icms", "parcelamento do iss", "parcelamento do das",
                  "parcelamento federal", "parcelamento estadual",
                  "parcelamento de multa", "divida parcelada", "dividir a divida"),
        digest="Parcelamento: federal em até 6 parcelas via e-CAC (programas especiais até 60+); ICMS em cada estado (SP: até 48); ISS em cada município; juros/multa sobre o saldo; atraso de uma parcela cancela o parcelamento.",
        text=(
            "📆 Parcelamento de débitos\n"
            "• Tributos FEDERAIS: parcelamento comum em até 6 parcelas mensais, pedido no e-CAC; "
            "programas especiais (REFIS, PERSE, PARAN) permitem até 60 ou mais parcelas.\n"
            "• ICMS (estadual): cada estado tem seu programa — em SP, até 48 parcelas (SEFAZ-SP).\n"
            "• ISS (municipal): cada município tem regra própria.\n"
            "• O saldo devedor continua com juros e multa até o pagamento; se você atrasar uma "
            "parcela, o parcelamento é CANCELADO e o saldo fica todo vencido.\n\n"
            "Para simular, me diz o imposto, o valor da dívida e o prazo que você quer."
         ),
     ),
    FiscalRule(
        title="SPED e EFD — fora do escopo",
        keywords=("sped", "sped fiscal", "sped contabil", "sped contábil",
                  "atraso na entrega do sped", "penalidades do sped",
                  "penalidade do sped", "multa do sped", "nao entrego o sped",
                  "entrega do sped", "penalidade", "penalidades",
                  "efd", "efd icms", "ecd", "ecf", "efc"),
        digest="SPED/EFD (prazos de pagamento e de entrega, penalidades e multas): FORA DO ESCOPO deste bot — não respondemos esse tema.",
        text=(
            "🚫 Esse assunto está fora do escopo deste bot\n"
            "• Prazos (de pagamento e de entrega) do SPED e da EFD, além de "
            "penalidades e multas dessas obrigações, estão FORA DO ESCOPO — não "
            "respondo sobre esse tema.\n"
            "• Para isso, fale com o seu contador ou consulte o portal do SPED / "
            "e-CAC da RFB.\n\n"
            "Segue à disposição com as dúvidas DENTRO do escopo: ISS, ICMS, "
            "ICMS-ST, DIFAL, PIS, COFINS, IPI, IRPJ, CSLL, INSS, IRPF, regimes "
            "(MEI, Simples, Presumido, Real), retenções, parcelamento, multa/juros "
            "por atraso e notas fiscais."
        ),
    ),
    FiscalRule(
        title="Serviços de tecnologia (TI) e software",
        keywords=("servicos de tecnologia", "servico de tecnologia",
                  "servicos de tecnologia da informacao", "servicos de ti",
                  "servico de ti", "tributacao de servicos de tecnologia",
                  "tributacao de software", "imposto de software", "iss de software",
                  "servicos de informatica", "servico de informatica",
                  "tributacao de servicos de informatica", "desenvolvimento de software"),
        digest="Prestação de serviço de TI (desenvolvimento, consultoria, suporte, SaaS) -> ISS (grupo 01 da LC 116, 2-5% pelo município do tomador); venda de software pronto (produto) -> ICMS.",
        text=(
            "💻 Serviços de tecnologia (TI) e software\n"
            "• PRESTAÇÃO DE SERVIÇO de TI (desenvolvimento, programação, consultoria, suporte, "
            "SaaS) → ISS — grupo 01 da LC 116/2003, alíquota de 2% a 5% definida pelo MUNICÍPIO "
            "do tomador.\n"
            "• VENDA de software pronto (produto/licença) → ICMS (circulação de mercadoria).\n"
            "• Mix (software + suporte/manutenção): ICMS sobre a parte de produto e ISS sobre a "
            "parte de serviço — a nota deve destacar os dois.\n"
            "• O tomador pode reter o ISS na fonte (2% — Simples Nacional, ou 3% — demais regimes).\n\n"
            "Se me disser a cidade do tomador e o código do serviço, eu confiro a alíquota exata."
        ),
    ),
]


# --------------------------------------------------------------------------- #
# Matching determinístico
# --------------------------------------------------------------------------- #
TOPIC_KEYWORDS = (
    "lc 116", "lc116", "lei complementar 116",
    "iss", "issqn", "icms", "ipi", "pis", "cofins", "csll", "ir", "irpj", "irrf",
    "substituicao tributaria", "substituicao", "st", "mva",
    "simples nacional", "simples", "mei",
    "microempreendedor", "lucro presumido", "lucro real",
    "regime", "aliquota", "tribut", "imposto", "tributo", "retencao", "retencoes",
    "base de calculo", "nota fiscal", "nf", "nfe", "nfs", "nfes", "nfses",
    "difal", "diferencial de aliquota", "mof",
    "reducao", "reducao de base", "reducao da base", "base reduzida",
    "codigo do servico", "codigo de servico",
    "o das", "guia das", "das do simples", "das do mei",
    "darf", "gps", "guia de pagamento",
    "inss", "previdencia", "iptu", "irpf", "carne leao",
    "imovel", "imoveis", "ganho de capital", "capital gain",
    "sped", "sped fiscal", "sped contabil", "efd", "ecd", "ecf",
    "rescisao", "rescisao trabalhista", "aviso previo", "indenizacao", "fgts",
    "clt", "autonomo", "pensionista", "aposentado",
    "dirpf", "declaracao", "declarar", "venda de acoes", "fiis",
    "anexo i", "anexo ii", "anexo iii", "anexo iv", "anexo v",
    "fiscal",
)


def _kw_in(kw: str, t: str) -> bool:
    """Palavra-chave normalizada dentro do texto normalizado.

    Palavras curtas (<= 3 letras) e frases (com espaço) exigem fronteira de
    palavra para não casar dentro de outras ('pis' não casa 'piso';
    'o das' não casa 'relatório das').
    """
    kw = normalize_name(kw)
    if not kw:
        return False
    if len(kw) <= 3 or " " in kw:
        return re.search(rf"\b{re.escape(kw)}\b", t) is not None
    return kw in t


def is_tax_topic(text: str) -> bool:
    """True se o texto fala de tributos/regimes (palavras-chave fiscais)."""
    t = normalize_name(text)
    return any(_kw_in(kw, t) for kw in TOPIC_KEYWORDS)


def match_rule(text: str) -> FiscalRule | None:
    """Melhor regra com 1+ palavra-chave no texto (ou None).

    Empate no número de palavras-chave é resolvido pela especificidade
    (soma do tamanho das palavras casadas): 'aliquota do iss' casa a regra
    de alíquota do ISS, não a regra genérica de ISS.
    """
    t = normalize_name(text)
    best: FiscalRule | None = None
    best_score: tuple[int, int] = (0, 0)
    for rule in RULES:
        matched = [kw for kw in rule.keywords if _kw_in(kw, t)]
        if not matched:
            continue
        score = (len(matched), sum(len(normalize_name(kw)) for kw in matched))
        if score > best_score:
            best, best_score = rule, score
    return best


__all__ = ["FiscalRule", "RULES", "TOPIC_KEYWORDS", "is_tax_topic", "match_rule"]
