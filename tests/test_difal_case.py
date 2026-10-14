"""Perguntas sobre o CASO de DIFAL do cliente: resposta direta e
determinística (origem/destino + natureza da operação + destinatário),
sem depender de LLM nem das notas da conversa.

Cobre também:
  * "tem DIFAL?" sem referência, com 2+ notas -> responde a mais recente;
  * "qual nota tem DIFAL?" -> continua análise do conjunto;
  * legenda (caption) de documento com pergunta fiscal -> resposta direta
    sobre a nota ANTES do parecer completo.
"""
from decimal import Decimal

from src.domain.models import TaxLine
from src.domain.tax import InvoiceAudit
from src.llm.tax_advisor import TaxAdvisor


def _nfe_difal() -> InvoiceAudit:
    return InvoiceAudit(
        source="nfe1.pdf", issuer="Fornecedor A", number="1234",
        nf_type="NFe", amount=Decimal("1180.00"), subtotal=Decimal("1000.00"),
        tax_lines=[
            TaxLine(name="ICMS", base=Decimal("1000.00"),
                    rate=Decimal("0.04"), amount=Decimal("40.00")),
            TaxLine(name="ICMS DIFAL", base=Decimal("1000.00"),
                    rate=Decimal("0.14"), amount=Decimal("140.00")),
        ],
    )


def _nfe_interna() -> InvoiceAudit:
    return InvoiceAudit(
        source="nfe2.pdf", issuer="Fornecedor B", number="5678",
        nf_type="NFe", amount=Decimal("1180.00"), subtotal=Decimal("1000.00"),
        tax_lines=[
            TaxLine(name="ICMS", base=Decimal("1000.00"),
                    rate=Decimal("0.18"), amount=Decimal("180.00")),
        ],
    )


# --------------------------------------------------------------------------- #
# Caso do cliente: serviço (o caso real que motivou o recurso)
# --------------------------------------------------------------------------- #
def test_servico_bh_poa_sem_difal():
    adv = TaxAdvisor()
    ans = adv.answer(
        "tenho uma empresa de software em BH e vou prestar serviço para uma "
        "empresa de POA, vou pagar DIFAL?", [])
    assert "Não" in ans
    assert "DIFAL" in ans
    assert "ISS" in ans
    assert "Porto Alegre" in ans


def test_caso_hipotetico_nota_generica_minas_roraima():
    # "nota" genérico dentro do caso ("vou pagar DIFAL se emitir nota contra
    # ele?") não é referência a nota da conversa: responde o caso. Sem
    # palavra de natureza (serviço x mercadoria): responde os dois cenários.
    adv = TaxAdvisor()
    ans = adv.answer(
        "estou abrindo uma empresa de Minas para atender um cliente de "
        "Roraima, vou pagar DIFAL se emitir nota contra ele?", [])
    assert "Roraima" in ans
    assert "serviço" in ans.lower()
    assert "mercadoria" in ans.lower()
    # mercadoria MG -> RR: interestadual 7%, interna RR 19% -> DIFAL 12 pp
    assert "12 pontos percentuais" in ans


def test_caso_hipotetico_nota_generica_com_notas_na_conversa():
    # mesmo com notas na conversa, o caso não é sequestrado por elas
    adv = TaxAdvisor()
    ans = adv.answer(
        "estou abrindo uma empresa de Minas para atender um cliente de "
        "Roraima, vou pagar DIFAL se emitir nota contra ele?", [_nfe_difal()])
    assert "NFe 1234" not in ans
    assert "12 pontos percentuais" in ans


def test_referencia_na_nota_mantem_interpretacao_de_nota():
    # "na nota" (preposição + documento) com notas na conversa -> pergunta
    # sobre a nota, não sobre o caso
    adv = TaxAdvisor()
    ans = adv.answer("tem DIFAL na nota de BH?", [_nfe_difal()])
    assert "NFe 1234" in ans


def test_caso_servico_com_notas_na_conversa():
    # o caso é respondido pelo caso — a nota da conversa não sequestra
    adv = TaxAdvisor()
    ans = adv.answer(
        "tenho empresa em BH e vou prestar serviço para uma empresa de POA, "
        "vou pagar DIFAL?", [_nfe_difal()])
    assert "Não" in ans
    assert "ISS" in ans
    assert "NFe 1234" not in ans


# --------------------------------------------------------------------------- #
# Caso do cliente: mercadoria
# --------------------------------------------------------------------------- #
def test_mercadoria_mesma_regiao():
    adv = TaxAdvisor()
    ans = adv.answer("se eu comprar de SP pra MG, tem DIFAL?", [])
    # SP -> MG: interestadual 12% (mesma região Sul/Sudeste) — mas o bot NÃO
    # responde "sim" direto: pergunta se é revenda ou consumidor final
    assert "Sim" not in ans
    assert "consumidor final" in ans.lower()
    assert "revender" in ans.lower()


def test_mercadoria_diferente_regiao():
    adv = TaxAdvisor()
    ans = adv.answer(
        "venda de mercadoria de SP para RS a um consumidor final, qual o DIFAL?",
        [])
    # SP -> RS: interestadual 12% (entre Sul/Sudeste), interna RS 17% -> 5 pp
    assert "5 pontos percentuais" in ans
    assert "12%" in ans
    assert "17%" in ans


def test_mercadoria_interna():
    adv = TaxAdvisor()
    ans = adv.answer("compra de mercadoria de BH para Uberlândia, tem DIFAL?", [])
    assert "Não" in ans
    assert "interna" in ans


def test_consumidor_final_pessoa_fisica():
    adv = TaxAdvisor()
    ans = adv.answer(
        "vou comprar um equipamento de SP para uma pessoa física em MG, "
        "tem DIFAL?", [])
    assert "Sim" in ans
    assert "6 pontos percentuais" in ans
    assert "consumidor final" in ans.lower()
    assert "MOC" not in ans


def test_revenda_sem_difal():
    adv = TaxAdvisor()
    ans = adv.answer(
        "vou comprar um equipamento de SP para revender a uma empresa de MG, "
        "tem DIFAL?", [])
    assert "Não" in ans
    assert "revenda" in ans.lower()
    assert "MOC" not in ans


def test_venda_licenca_software_e_icms():
    # venda de licença (transferência de propriedade) = ICMS, não ISS; sem
    # saber se o destinatário revende ou consome, o bot pergunta antes
    adv = TaxAdvisor()
    ans = adv.answer(
        "vou vender licença de software de MG para uma empresa em SP, "
        "tem DIFAL?", [])
    assert "Sim" not in ans
    assert "consumidor final" in ans.lower()
    # respondendo "é para revenda" (junção que o bot faz na conversa):
    # MG -> SP = 12% de interestadual, sem DIFAL
    ans2 = adv.answer(
        "vou vender licença de software de MG para uma empresa em SP, "
        "tem DIFAL? é para revenda", [])
    assert "Não" in ans2
    assert "revenda" in ans2.lower()


def test_caso_sem_estado_fica_incompleto():
    adv = TaxAdvisor()
    ans = adv.answer("vou comprar mercadoria de SP, tem DIFAL?", [])
    # só um estado: pede o outro e explica a regra
    assert "dois" in ans
    assert "interestadual" in ans


# --------------------------------------------------------------------------- #
# Pergunta sem número de nota -> resposta objetiva (conjunto/regra), sem
# supor/adivinhar qual nota o cliente quer
# --------------------------------------------------------------------------- #
def test_bare_tem_difal_sem_numero_responde_conjunto():
    # "tem DIFAL?" sem número: não supõe nota — responde o conjunto + regra
    adv = TaxAdvisor()
    ans = adv.answer("tem DIFAL?", [_nfe_difal(), _nfe_interna()])
    assert "NFe 5678" in ans
    assert "NFe 1234" in ans
    assert "desta conversa" in ans


def test_reducao_de_base_sem_numero_responde_objetivo():
    # "redução de base?" sem número: responde a regra (objetivo), não supõe
    # qual nota — mesmo com notas na conversa (sem dica "respondendo sobre...")
    adv = TaxAdvisor()
    ans = adv.answer("redução de base?", [_nfe_difal(), _nfe_interna()])
    assert "Redução de base de cálculo" in ans
    assert "respondendo sobre" not in ans


def test_qual_nota_tem_difal_mantem_conjunto():
    adv = TaxAdvisor()
    ans = adv.answer("qual nota tem DIFAL?", [_nfe_difal(), _nfe_interna()])
    assert "NFe 1234" in ans
    assert "NFe 5678" in ans


def test_definicao_com_notas_mantem_ancoragem():
    adv = TaxAdvisor()
    ans = adv.answer("o que é DIFAL?", [_nfe_difal(), _nfe_interna()])
    assert "desta conversa" in ans
    assert "EC 87/2015" in ans


# --------------------------------------------------------------------------- #
# Legenda (caption) de documento com pergunta fiscal
# --------------------------------------------------------------------------- #
_NFS_TEXT = """NOTA FISCAL DE SERVIÇOS ELETRÔNICA (NFS-e)
Número da Nota: 12345
Data de Emissão: 10/08/2026
EMITENTE
Nome/Razão Social: EMPRESA SOFT LTDA
CNPJ: 12.345.678/0001-90
TOMADOR DO SERVIÇO
Nome: CLIENTE POA LTDA
CNPJ: 98.765.432/0001-10
Serviço: 17.20
Município da Incidência do ISSQN
PORTO ALEGRE - RS
Base de Cálculo ISSQN: 1.000,00
Alíquota: 3,00%
Valor ISSQN: 30,00
VALOR TOTAL DA NOTA
Líquido: 1.000,00
"""


def _agent(tmp_path):
    from src.bot.telegram_bot import ConciliationAgent
    cfg = {
        "bot": {"auto_run": False, "history_turns": 3},
        "matching": {
            "amount_tolerance": "0.05",
            "date_window_days": 3,
            "name_threshold": 0.8,
            "fuzzy_confidence_threshold": 0.9,
            "late_tolerance_days": 3,
            "low_confidence_threshold": 0.6,
        },
        "tax": {"tolerance": "0.05"},
    }
    return ConciliationAgent(cfg, None, None, tmp_path)


def test_caption_documento_responde_antes_do_full_report(tmp_path):
    agent = _agent(tmp_path)
    session = agent.get_session(1)
    resp = agent.handle_document(session, "nfs.txt", _NFS_TEXT.encode("utf-8"),
                                 caption="tem DIFAL?")
    texts = resp.texts
    # a pergunta da legenda é respondida DIRETAMENTE sobre a nota...
    difal_idx = next(i for i, t in enumerate(texts) if "não tem DIFAL" in t)
    # ...e ANTES do parecer completo da nota
    full_idx = next(i for i, t in enumerate(texts) if "📊 Impostos da nota" in t)
    assert difal_idx < full_idx
    # a resposta cita que é serviço (NFS-e): não incide ICMS/DIFAL
    assert "serviço" in texts[difal_idx].lower()
    assert "NFS-e" in texts[difal_idx]


def test_caption_documento_sem_pergunta_nao_responde(tmp_path):
    agent = _agent(tmp_path)
    session = agent.get_session(1)
    resp = agent.handle_document(session, "nfs.txt", _NFS_TEXT.encode("utf-8"),
                                  caption="olha essa nota")
    assert not any("não tem DIFAL" in t for t in resp.texts)


# --------------------------------------------------------------------------- #
# Pergunta fiscal GERAL com notas na conversa: não é sequestrada pela última
# nota — vai para a regra (regime/alíquota/comparação)
# --------------------------------------------------------------------------- #
def test_geral_iss_por_regime_com_nota_na_conversa():
    # "ISS no Simples ou no Presumido?" é pergunta GERAL de regime: mesmo com
    # uma nota na conversa, responde a regra (não a última nota)
    adv = TaxAdvisor()
    ans = adv.answer(
        "Minha empresa paga mais impostos de ISS no regime Simples Nacional "
        "ou Lucro Presumido?", [_nfe_difal()])
    assert "ISS por regime" in ans
    assert "NFe 1234" not in ans
    assert "MESMO" in ans  # o valor do ISS é o mesmo nos dois regimes


def test_geral_iss_por_regime_com_varias_notas():
    # mesmo com 2+ notas na conversa, a pergunta geral vai para a regra
    adv = TaxAdvisor()
    ans = adv.answer(
        "ISS no Simples ou no Presumido é o mesmo?",
        [_nfe_difal(), _nfe_interna()])
    assert "ISS por regime" in ans
    assert "NFe 1234" not in ans
    assert "NFe 5678" not in ans


def test_bare_tem_difal_com_1_nota_responde_conjunto():
    # sem número, "tem DIFAL?" responde o conjunto (a única nota) + regra —
    # não trata a pergunta como se fosse específica daquela nota
    adv = TaxAdvisor()
    ans = adv.answer("tem DIFAL?", [_nfe_difal()])
    assert "NFe 1234" in ans
    assert "desta conversa" in ans
