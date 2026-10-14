"""Alíquota interestadual e DIFAL — fonte única da regra.

Resolução do Senado Federal 22/1989 e 13/2012:
  * 4%  — bens importados (conteúdo de importação > 40%);
  * 7%  — origem no Sul/Sudeste (PR, SC, RS, SP, RJ, MG) com destino no
          Norte, Nordeste, Centro-Oeste ou ES;
  * 12% — todos os outros casos;
  * mesma UF — sem alíquota interestadual e sem DIFAL.

DIFAL = alíquota interna do destino − alíquota interestadual.
"""
from decimal import Decimal

import pytest

from src.domain.icms_rates import (
    INTERNAL_RATES,
    aliquota_interestadual,
    calcular_difal,
)
from src.domain.difal_case import analyze_difal_case, answer_difal_case


# --------------------------------------------------------------------------- #
# aliquota_interestadual: os casos pedidos
# --------------------------------------------------------------------------- #
def test_pa_mg_12():
    assert aliquota_interestadual("PA", "MG") == Decimal("12")


def test_pa_sp_12():
    assert aliquota_interestadual("PA", "SP") == Decimal("12")


def test_mg_go_7():
    # origem Sul/Sudeste (MG) com destino Centro-Oeste (GO)
    assert aliquota_interestadual("MG", "GO") == Decimal("7")


def test_se_am_12():
    # origem Nordeste (SE) -> 12% em qualquer destino
    assert aliquota_interestadual("SE", "AM") == Decimal("12")


def test_sp_mg_12():
    # vendas entre estados do Sul/Sudeste
    assert aliquota_interestadual("SP", "MG") == Decimal("12")


def test_sp_es_7():
    # origem Sul/Sudeste (SP) com destino ES
    assert aliquota_interestadual("SP", "ES") == Decimal("7")


def test_es_sp_12():
    # ES fica FORA do grupo dos 7% como origem: 12%
    assert aliquota_interestadual("ES", "SP") == Decimal("12")


@pytest.mark.parametrize("origem,destino", [
    ("PA", "MG"), ("PA", "SP"), ("MG", "GO"), ("SE", "AM"),
    ("SP", "MG"), ("SP", "ES"), ("ES", "SP"),
])
def test_importado_sempre_4(origem, destino):
    assert aliquota_interestadual(origem, destino, importado=True) == Decimal("4")


def test_mesma_uf_sem_interestadual():
    assert aliquota_interestadual("AM", "AM") is None


def test_uf_invalida_levanta_erro():
    with pytest.raises(ValueError):
        aliquota_interestadual("XX", "MG")


# --------------------------------------------------------------------------- #
# calcular_difal: interna do destino − interestadual
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("origem,destino,esperado", [
    ("PA", "MG", Decimal("6")),   # 18 − 12
    ("PA", "SP", Decimal("6")),   # 18 − 12
    ("MG", "GO", Decimal("10")),  # 17 − 7
    ("SE", "AM", Decimal("8")),   # 20 − 12
    ("SP", "MG", Decimal("6")),   # 18 − 12
])
def test_difal_casos_pedidos(origem, destino, esperado):
    interna = INTERNAL_RATES[destino]
    assert calcular_difal(origem, destino, interna) == esperado


def test_difal_mesma_uf_none():
    assert calcular_difal("SP", "SP", Decimal("18")) is None


# --------------------------------------------------------------------------- #
# Caso do cliente (difal_case): o bot NÃO responde "Sim" antes de perguntar
# --------------------------------------------------------------------------- #
def _case(text: str) -> str:
    return answer_difal_case(analyze_difal_case(text), text=text)


_CONTADOR = "Confirme a alíquota interna e as regras do estado de destino com seu contador."


def test_pergunta_pago_difal_nao_responde_sim_direto():
    ans = _case(
        "tenho uma empresa do Pará que vende mesas e cadeiras, quero vender "
        "para um cliente de Minas Gerais, terei que pagar DIFAL?")
    assert "Sim" not in ans
    assert "consumidor final" in ans.lower()
    assert "revender" in ans.lower()
    assert "imobilizado" in ans.lower()   # lista os 3 casos


def test_pa_mg_caso1_revenda_sem_difal():
    ans = _case(
        "vou revender mercadoria vinda do Pará para uma empresa de Minas "
        "Gerais, tem DIFAL?")
    assert "Não — nesse caso (revenda) não há DIFAL (0%)." in ans
    assert "12%" in ans            # interestadual PA -> MG
    assert "CRÉDITO" in ans
    assert _CONTADOR in ans
    assert "MOC" not in ans


def test_pa_mg_caso2_uso_consumo_difal_6_pontos():
    ans = _case(
        "mercadoria do Pará para uso e consumo da empresa em Minas Gerais, "
        "vou pagar DIFAL?")
    assert "Sim — há DIFAL de 6 pontos percentuais." in ans
    assert "12%" in ans            # interestadual PA -> MG
    assert "18%" in ans            # interna MG
    assert "DESTINATÁRIO" in ans   # contribuinte recolhe
    assert _CONTADOR in ans


def test_pa_mg_caso3_pessoa_fisica_difal_6_pontos():
    ans = _case(
        "mercadoria do Pará para uma pessoa física em Minas Gerais, "
        "vou pagar DIFAL?")
    assert "Sim — há DIFAL de 6 pontos percentuais." in ans
    assert "12%" in ans
    assert "18%" in ans
    assert "REMETENTE" in ans      # remetente recolhe
    assert _CONTADOR in ans


def test_sp_mg_caso3_difal_6_pontos():
    ans = _case(
        "compra de mercadoria de SP para uma pessoa física em MG, tem DIFAL?")
    assert "Sim — há DIFAL de 6 pontos percentuais." in ans
    assert "12%" in ans            # interestadual SP -> MG
    assert "18%" in ans            # interna MG
    assert _CONTADOR in ans


def test_pergunta_destinatario_consumidor_final_responde_com_numeros():
    ans = _case(
        "venda de mercadoria do Pará para uma pessoa física em Minas Gerais, "
        "consumidor final, terei que pagar DIFAL?")
    assert "Sim — há DIFAL de 6 pontos percentuais." in ans
    assert "12%" in ans  # interestadual PA -> MG
    assert "18%" in ans  # interna MG
    assert "MOC" not in ans


def test_revenda_nao_tem_difal():
    ans = _case(
        "vou revender mercadoria vinda do Pará para uma empresa de "
        "Minas Gerais, tem DIFAL?")
    assert "Não" in ans
    assert "revenda" in ans.lower()


def test_mg_go_consumidor_final_10():
    ans = _case(
        "mercadoria de Minas Gerais para um consumidor final em Goiás, "
        "vou pagar DIFAL?")
    assert "há DIFAL de 10 pontos percentuais." in ans
    assert "7%" in ans
    assert "17%" in ans


def test_estado_sem_interna_cadastrada_pede_nao_inventa():
    # destino PA não está na tabela de alíquotas internas
    ans = _case(
        "mercadoria de SP para uma pessoa física no Pará, tem DIFAL?")
    assert "Sim" not in ans
    assert "Pará" in ans
    assert "alíquota interna" in ans
    assert "inventar" in ans
    # e o cliente pode informar a alíquota: daí o cálculo sai
    ans2 = _case(
        "mercadoria de SP para uma pessoa física no Pará, tem DIFAL? "
        "a interna é 20%")
    assert "há DIFAL de 13 pontos percentuais." in ans2  # 20 − 7 (SP -> PA)


def test_valor_da_nota_gera_reais_com_alerta_base_por_dentro():
    ans = _case(
        "mercadoria do Pará para uma pessoa física em Minas Gerais, "
        "nota de R$ 10.000,00, vou pagar DIFAL?")
    assert "R$ 10.000,00" in ans
    assert "R$ 600,00" in ans       # 6% de 10.000
    assert "por dentro" in ans
    assert "FCP" in ans


def test_mesma_uf_sem_difal():
    ans = _case("compra de mercadoria de BH para Uberlândia, tem DIFAL?")
    assert "Não" in ans
    assert "interna" in ans


def test_servico_sem_difal():
    ans = _case(
        "tenho uma empresa de software em BH e vou prestar serviço para uma "
        "empresa de POA, vou pagar DIFAL?")
    assert "Não" in ans
    assert "ISS" in ans


def test_respostas_sem_moc_e_sem_asteriscos():
    for text in (
        "compra de mercadoria de SP para uma pessoa física em MG, tem DIFAL?",
        "vou revender mercadoria vinda do Pará para uma empresa de MG, tem DIFAL?",
        "venda de mercadoria do Pará para um cliente de Minas Gerais, teria DIFAL?",
    ):
        ans = _case(text)
        assert "MOC" not in ans
        assert "**" not in ans
        assert "só existe na venda a consumidor final" not in ans.lower()


# --------------------------------------------------------------------------- #
# Conversa multi-turno no bot: pergunta -> cliente responde -> número
# --------------------------------------------------------------------------- #
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


def test_bot_pergunta_e_depois_calcula_difal(tmp_path):
    agent = _agent(tmp_path)
    session = agent.get_session(1)
    r1 = agent.handle_text(
        session,
        "tenho uma empresa do Pará que vende mesas e cadeiras, quero vender "
        "para um cliente de Minas Gerais, terei que pagar DIFAL?")
    ans1 = " ".join(r1.texts)
    assert "Sim" not in ans1
    assert "consumidor final" in ans1.lower()
    # cliente responde e o bot junta com a pergunta pendente
    r2 = agent.handle_text(session, "é para um consumidor final")
    ans2 = " ".join(r2.texts)
    assert "há DIFAL de 6 pontos percentuais." in ans2
    assert "12%" in ans2
    assert "18%" in ans2
    assert _CONTADOR in ans2


def test_bot_resposta_numerica_2_uso_consumo(tmp_path):
    agent = _agent(tmp_path)
    session = agent.get_session(1)
    agent.handle_text(
        session,
        "vou comprar mercadoria do Pará para a empresa em MG, tem DIFAL?")
    r2 = agent.handle_text(session, "2")
    ans2 = " ".join(r2.texts)
    assert "há DIFAL de 6 pontos percentuais." in ans2
    assert "uso/consumo" in ans2
    assert _CONTADOR in ans2


def test_bot_resposta_numerica_1_revenda(tmp_path):
    agent = _agent(tmp_path)
    session = agent.get_session(1)
    agent.handle_text(
        session,
        "vou comprar mercadoria do Pará para a empresa em MG, tem DIFAL?")
    r2 = agent.handle_text(session, "1")
    ans2 = " ".join(r2.texts)
    assert "não há DIFAL" in ans2
    assert "revenda" in ans2.lower()


def test_bot_ped_aliquota_interna_depois_calcula(tmp_path):
    agent = _agent(tmp_path)
    session = agent.get_session(1)
    r1 = agent.handle_text(
        session,
        "vou comprar mercadoria de SP para uma pessoa física no Pará, "
        "tem DIFAL?")
    ans1 = " ".join(r1.texts)
    assert "Sim" not in ans1
    assert "alíquota interna" in ans1
    # cliente informa a alíquota (em %) e o bot calcula: 20 − 7 = 13 pp
    r2 = agent.handle_text(session, "a interna é 20%")
    ans2 = " ".join(r2.texts)
    assert "há DIFAL de 13 pontos percentuais." in ans2
    assert "7%" in ans2
    assert "20%" in ans2


def test_bot_revenda_nao_tem_difal(tmp_path):
    agent = _agent(tmp_path)
    session = agent.get_session(1)
    agent.handle_text(
        session,
        "tenho uma empresa do Pará que vende mesas e cadeiras, quero vender "
        "para um cliente de Minas Gerais, terei que pagar DIFAL?")
    r2 = agent.handle_text(session, "é para revender")
    ans2 = " ".join(r2.texts)
    assert "Não" in ans2
    assert "revenda" in ans2.lower()
