# Tabela de alíquotas ISSQN para SP - Base LC 116/2003
# Mínimo 2% / Máximo 5%
import unicodedata


def _norm_cidade(cidade: str) -> str:
    """Normaliza para maiúsculas sem acento: 'São Paulo' -> 'SAO PAULO'."""
    s = unicodedata.normalize("NFD", cidade.strip().upper())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")

TABELA_PADRAO_SP = {
    # Grupo 01 - Informática
    "01.01": 0.03, "01.02": 0.03, "01.03": 0.03, "01.04": 0.03,
    "01.05": 0.03, "01.06": 0.03, "01.07": 0.03, "01.08": 0.03,
    # Grupo 07 - Engenharia / Construção
    "07.02": 0.05, "07.03": 0.05, "07.05": 0.03, "07.10": 0.03,
    # Grupo 08 - Educação
    "08.01": 0.02, "08.02": 0.02,
    # Grupo 10 - Intermediação
    "10.01": 0.05, "10.02": 0.05,
    # Grupo 14 - Bens móveis
    "14.01": 0.03, "14.02": 0.03, "14.05": 0.03, "14.06": 0.03,
    # Grupo 17 - Apoio técnico/adm
    "17.01": 0.05, "17.02": 0.03, "17.05": 0.05, "17.06": 0.05,
    "17.19": 0.02, "17.20": 0.03, # Contabilidade
    "17.22": 0.03,
    # Grupo 11, 12, 13 - Outros comuns
    "11.02": 0.03, "13.03": 0.03,
    "DEFAULT": 0.03
}

# Exceções por cidade - só preenche quem é diferente do padrão
EXCECOES_CIDADES_SP = {
    "SAO PAULO": {
        "14.01": 0.05, "14.02": 0.05,
        "17.19": 0.025, # 2.5% em SP capital
        "17.01": 0.05,
        "01.07": 0.029, # TI tem incentivo em SP
    },
    "CAMPINAS": {
        "17.19": 0.02,
        "07.02": 0.03,
    },
    "SOROCABA": {
        "14.01": 0.03, "14.02": 0.03,
        "17.19": 0.02,
        "17.01": 0.05,
        "07.02": 0.05,
    },
    "JUNDIAI": {
        "14.01": 0.03,
        "17.19": 0.02,
    },
    "OSASCO": {
        "17.19": 0.02,
    }
}

def get_aliquota(cidade: str, codigo_servico: str) -> float:
    cidade = _norm_cidade(cidade)
    codigo = codigo_servico.split(" ")[0].strip() # pega só "14.01"

    # 1. Tenta achar exceção da cidade
    if cidade in EXCECOES_CIDADES_SP:
        if codigo in EXCECOES_CIDADES_SP[cidade]:
            return EXCECOES_CIDADES_SP[cidade][codigo]

    # 2. Tenta padrão SP
    if codigo in TABELA_PADRAO_SP:
        return TABELA_PADRAO_SP[codigo]

    # 3. Fallback
    return TABELA_PADRAO_SP["DEFAULT"]

def calcular_issqn(valor_servico: float, cidade: str, codigo_servico: str) -> dict:
    aliquota = get_aliquota(cidade, codigo_servico)
    issqn = valor_servico * aliquota
    return {
        "base": valor_servico,
        "aliquota": aliquota,
        "aliquota_percent": f"{aliquota*100:.2f}%",
        "issqn": round(issqn, 2),
        "fonte": "EXCECAO" if _norm_cidade(cidade) in EXCECOES_CIDADES_SP else "PADRAO_SP"
    }
