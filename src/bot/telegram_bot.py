"""Bot conversacional de conciliação no Telegram.

Fluxo:
  1. O cliente manda mensagem ou arquivo (extrato / faturas) para o bot.
  2. O "cérebro" (LLM via Ollama, ou template determinístico) decide o
     próximo passo: pedir arquivo que falta, listar arquivos, recomeçar ou
     rodar a conciliação.
  3. Quando há pelo menos 1 extrato e 1 fatura, o bot roda o pipeline na
     pasta do chat e devolve o resumo + relatórios (CSV/MD).

O pipeline de conciliação roda determinístico por padrão (rápido). O LLM
entra na conversa (perguntas) e, opcionalmente, na conciliação
(bot.pipeline_llm: true).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from ..domain.difal_case import (
    analyze_difal_case,
    precisa_destinatario,
    precisa_interna,
)
from ..domain.models import Invoice, Transaction
from ..domain.rules import DeterministicMatcher, normalize_name
from ..domain.tax import InvoiceAudit, _brl, format_tax_lines
from ..engine import Reconciler
from ..mail import EmailFetchError, build_email_config, fetch_invoice_files
from ..llm import FallbackClient, OllamaClient, TaxAdvisor, audit_to_invoice, build_audit
from ..notify.telegram import TelegramNotifier
from ..parsing import OCRError, ocr_image, parse_directory, parse_file
from ..reporting import write_report


WELCOME = (
    "👋 Olá! Sou o assistente de conciliação e auditoria de faturas.\n\n"
    "Você tem 2 caminhos para conciliar:\n\n"
    "📎 CAMINHO 1 — Enviar os arquivos\n"
    "1️⃣ Envie seu EXTRATO bancário (PDF, CSV, Excel ou OFX).\n"
    "2️⃣ Envie suas FATURAS (PDF, CSV, Excel — ou FOTOS de notas/faturas).\n"
    "3️⃣ Eu concilio os pagamentos com as faturas e verifico o cálculo dos impostos.\n"
    "4️⃣ Você recebe o resumo + relatório (CSV/Markdown) com os alertas.\n\n"
    "📧 CAMINHO 2 — Buscar as notas no seu e-mail\n"
    "Digite \"buscar no email\" (ex.: \"buscar no email de julho\") que eu busco as "
    "notas/faturas em anexo na sua caixa de entrada, calculo os impostos de cada "
    "uma e te mando o resultado na hora — sem precisar digitar nada depois. Se o "
    "extrato já estiver na conversa, eu rodo a conciliação junto.\n\n"
    "💡 Também respondo perguntas fiscais: LC 116/2003, alíquotas (ISS, ICMS, PIS, COFINS, "
    "IPI), DIFAL, ICMS-ST (substituição tributária), redução de base, regimes (Simples "
    "Nacional, MEI, Presumido, Real), "
    "retenções — genéricas, sobre uma nota específica (ex.: 'tem retenção na nota 123?') "
    "ou sobre o conjunto das notas/extrato (ex.: 'qual nota tem retenção?'). "
    "Se você já mandou notas, eu analiso a pergunta sobre elas (ex.: 'me explica o DIFAL' "
    "com as suas notas na conversa). E respondo sobre o SEU CASO: me conta a situação "
    "(ex.: 'tenho empresa de software em BH e vou prestar serviço para uma empresa de "
    "POA — vou pagar DIFAL?') que eu te digo na lata.\n\n"
    "Pode começar pelo caminho 1 (enviando o extrato ou as faturas) ou pelo "
    "caminho 2 (digitando \"buscar no email\")."
)


# --------------------------------------------------------------------------- #
# API do Telegram (long polling)
# --------------------------------------------------------------------------- #
class TelegramAPI:
    def __init__(self, token: str, timeout: float = 35.0):
        self.token = token
        self.timeout = timeout
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.Client(timeout=timeout)

    def _call(self, method: str, *, json_body=None, data=None, files=None):
        url = f"{self._base}/{method}"
        if files is not None:
            r = self._client.post(url, data=data or {}, files=files)
        elif json_body is not None:
            r = self._client.post(url, json=json_body)
        else:
            r = self._client.get(url, params=data or {})
        r.raise_for_status()
        payload = r.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method}: {payload}")
        return payload.get("result")

    def get_me(self) -> dict:
        return self._call("getMe") or {}

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        return self._call("getUpdates", json_body={
            "offset": offset, "timeout": timeout, "allowed_updates": ["message"],
        }) or []

    def send_text(self, chat_id, text: str, parse_mode: str | None = None):
        # sem parse_mode o Telegram mostra o texto CRU: "**negrito**" aparece
        # com asteriscos soltos — remove o marcador antes de enviar
        if not parse_mode:
            text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
        body: dict = {"chat_id": chat_id, "text": text[:4096]}
        if parse_mode:
            body["parse_mode"] = parse_mode
        try:
            return self._call("sendMessage", json_body=body)
        except Exception:  # noqa: BLE001 - Markdown inválido -> tenta sem formatação
            return self._call("sendMessage", json_body={"chat_id": chat_id, "text": text[:4096]})

    def send_chat_action(self, chat_id, action: str = "typing"):
        try:
            self._call("sendChatAction", json_body={"chat_id": chat_id, "action": action})
        except Exception:  # noqa: BLE001 - indicador visual; falha não importa
            pass

    def send_document(self, chat_id, path: str | Path, caption: str = ""):
        path = Path(path)
        with path.open("rb") as f:
            return self._call("sendDocument",
                              data={"chat_id": chat_id, "caption": caption},
                              files={"document": (path.name, f)})

    def download_file(self, file_id: str) -> bytes:
        info = self._call("getFile", json_body={"file_id": file_id})
        url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
        r = self._client.get(url, timeout=120)
        r.raise_for_status()
        return r.content


# --------------------------------------------------------------------------- #
# Estado por conversa
# --------------------------------------------------------------------------- #
@dataclass
class ChatSession:
    chat_id: int
    workdir: Path
    files: list[dict] = field(default_factory=list)
    period: str | None = None
    history: list[dict] = field(default_factory=list)  # mensagens p/ o LLM
    executed_file_count: int = 0
    invoices_from_photos: list[Invoice] = field(default_factory=list)
    audits: list[InvoiceAudit] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)  # sha256 -> nome (anti-duplicata)
    welcomed: bool = False
    last_report: object | None = None          # último relatório p/ responder sobre alertas
    last_report_paths: tuple | None = None     # (csv, md) do último relatório
    pending_invoice_ids: set[str] = field(default_factory=set)  # faturas aguardando extrato
    pending_difal_text: str | None = None  # caso de DIFAL aguardando estado/natureza
    pending_email_period: bool = False  # "buscar no email" aguardando mês/data
    last_discussed: InvoiceAudit | None = None  # nota em pauta: última analisada/consultada

    @property
    def has_extrato(self) -> bool:
        return any(f["n_txns"] > 0 for f in self.files)

    @property
    def has_faturas(self) -> bool:
        return (any(f["n_invoices"] > 0 for f in self.files)
                or len(self.invoices_from_photos) > 0)

    @property
    def file_count(self) -> int:
        return len(self.files) + len(self.invoices_from_photos)

    def files_label(self) -> str:
        parts = [
            f"{f['name']} ({f['n_txns']} transações / {f['n_invoices']} faturas)"
            for f in self.files
        ]
        if self.invoices_from_photos:
            parts.append(f"{len(self.invoices_from_photos)} fatura(s) de foto")
        return ", ".join(parts) if parts else "nenhum"


@dataclass
class Decision:
    reply: str
    action: str = "ask"  # ask | run | list_files | reset | fetch_email
    period: str | None = None


@dataclass
class BotResponse:
    texts: list[str] = field(default_factory=list)
    report: object | None = None
    audits: list[InvoiceAudit] = field(default_factory=list)
    csv_path: Path | None = None
    md_path: Path | None = None


# --------------------------------------------------------------------------- #
# Extração determinística de período ("julho de 2026", "2026-07", "07/2026")
# --------------------------------------------------------------------------- #
_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def parse_period(text: str) -> str | None:
    import re
    t = text.lower()
    m = re.search(r"\b(20\d{2})\s*[-/]\s*(\d{1,2})\b", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})\s*[-/]\s*(20\d{2})\b", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
    for name, mo in _MONTHS.items():
        m = re.search(rf"\b{name}\b\D{{0,6}}?(20\d{{2}})?", t)
        if m:
            y = int(m.group(1)) if m.group(1) else date.today().year
            return f"{y:04d}-{mo:02d}"
    return None


_MONTH_NAMES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho",
                "julho", "agosto", "setembro", "outubro", "novembro", "dezembro")


def period_label(period: str) -> str:
    """'2026-08' -> 'agosto/2026'; '2026-08-15' -> '15/08/2026'."""
    parts = [int(x) for x in period.split("-")]
    if len(parts) == 3:
        return f"{parts[2]:02d}/{parts[1]:02d}/{parts[0]:04d}"
    return f"{_MONTH_NAMES[parts[1] - 1]}/{parts[0]:04d}"


# data específica na resposta à pergunta de período do e-mail; os guardas
# (?<![\d/-.]) / (?![\d/-.]) evitam casar "26-08" dentro de "2026-08"
_DAY_RE = re.compile(
    r"(?<![\d/.\-])(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](20\d{2}))?(?![\d/.\-])")


def parse_day_period(text: str) -> str | None:
    """Dia específico: DD/MM/AAAA, DD-MM-AAAA, AAAA-MM-DD ou DD/MM (ano atual).
    Retorna 'AAAA-MM-DD' ou None (mês/ano fica por conta do parse_period)."""
    m = re.search(r"\b(20\d{2})[/.\-](\d{1,2})[/.\-](\d{1,2})\b", text)
    if m and 1 <= int(m.group(2)) <= 12:
        try:
            date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = _DAY_RE.search(text)
    if m and 1 <= int(m.group(2)) <= 12:
        y = int(m.group(3)) if m.group(3) else date.today().year
        try:
            date(y, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


# --------------------------------------------------------------------------- #
# Cumprimentos e agradecimentos -> resposta calorosa (não "Entendido.")
# --------------------------------------------------------------------------- #
def _normalize_text(text: str) -> str:
    """minúsculas, sem acentos e sem pontuação (p/ comparar palavras)."""
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_GREETING_PHRASES = {
    "oi", "oii", "oi oi", "ola", "ola ola", "opa", "eai", "e ai", "eae", "e ae",
    "hey", "oye", "salve", "fala", "fala ai", "bom dia", "boa tarde", "boa noite",
    "oi tudo bem", "ola tudo bem", "bom dia tudo bem", "boa tarde tudo bem",
    "boa noite tudo bem", "tudo bem", "td bem", "bom te",
}
_GREETING_STARTS = (
    "oi", "oii", "ola", "opa", "eai", "e ai", "eae", "e ae", "hey", "oye",
    "salve", "fala", "bom dia", "boa tarde", "boa noite",
)
_CONV_TAILS = {
    "", "tudo bem", "td bem", "tudo bom", "tudo certo", "tudo tranquilo",
    "como voce esta", "como ta", "como vai", "e voce", "e ai tudo bem",
}
_THANKS_RE = re.compile(r"\b(obrigad[oa]|valeu|vlw|agradecid[oa]|obg|thanks)\b",
                        re.IGNORECASE)


def is_greeting(text: str) -> bool:
    """True se a mensagem é um cumprimento curto, sem pedido embutido."""
    t = _normalize_text(text)
    if not t:
        return False
    if t in _GREETING_PHRASES:
        return True
    for g in _GREETING_STARTS:
        if t.startswith(g + " ") and t[len(g) + 1:] in _CONV_TAILS:
            return True
    return False


def is_thanks(text: str) -> bool:
    """True se a mensagem é um agradecimento curto (sem comando embutido)."""
    return bool(_THANKS_RE.search(text)) and len(text.split()) <= 4


# --------------------------------------------------------------------------- #
# "voltar ao início" -> abandona o raciocínio e volta para a 1ª mensagem
# --------------------------------------------------------------------------- #
# Único pedido que reinicia a conversa (nenhum outro texto faz isso):
# "voltar ao início", "volta ao inicio", "quero voltar pro início", "/start".
_HOME_RE = re.compile(
    r"(?:^|\s)/(?:start|inicio)\b"
    r"|\bvolta(?:r)?\b[^?!.]{0,30}\bin[ií]cio\b",
    re.IGNORECASE,
)


def is_home(text: str) -> bool:
    return bool(_HOME_RE.search(text))


# --------------------------------------------------------------------------- #
# Cérebros (decisão da conversa)
# --------------------------------------------------------------------------- #
class TemplateBrain:
    """Cérebro determinístico: sem LLM, rápido e previsível.

    Reconhece intenções (listar, recomeçar, rodar, ajuda, dúvida) e, para
    qualquer outra mensagem, devolve um fallback útil e contextual — nunca
    uma resposta fora de contexto.
    """

    _LIST_RE = re.compile(
        r"\b(listar|listar arquivos|arquivos|o que tem|o que voc[eê] tem|"
        r"meus arquivos|que arquivos|quais arquivos)\b", re.IGNORECASE)
    _RESET_RE = re.compile(
        r"\b(recome[cç]ar|recomecar|limpar|reset|do zero|come[cç]ar de novo|zerar)\b",
        re.IGNORECASE)
    _RUN_STRONG = ("rodar", "conciliar")
    _RUN_SHORT = ("sim", "pode", "vai", "go", "fazer", "concorde", "concordo", "bora")
    _HELP_RE = re.compile(
        r"\b(ajuda|help|o que voc[eê] faz|como funciona|como uso|op[cç][õo]es|"
        r"menu|comandos|me ajuda|pode me ajudar|o que voc[eê] sabe fazer)\b",
        re.IGNORECASE)
    _DOUBT_RE = re.compile(
        r"(d[úu]vida|pergunta|quero saber|quero tirar|quero perguntar|esclarec|"
        r"explica|me explica|n[ãa]o entendi|nao entendi|ficou confuso)", re.IGNORECASE)
    _REPORT_RE = re.compile(
        r"\b(resumo|relat[óo]rio|me mostra|me manda|reenvia|reenviar)\b", re.IGNORECASE)
    _ALERTS_RE = re.compile(
        r"(alerta|alertas|baixa confian[cç]a|n[ãa]o conciliou|nao conciliou|n[ãa]o bateu|"
        r"diverg[êe]ncia|inconsist[êe]ncia|sem correspond[êe]ncia|o que significa)",
        re.IGNORECASE)
    # caminho 2: "buscar no email", "busca notas no gmail", "baixa as faturas
    # do e-mail"... (verbo de busca perto da caixa de entrada)
    _EMAIL_FETCH_RE = re.compile(
        r"\b(busca|busque|buscar|procura|procure|procurar|baixa|baixar|puxa|puxar)\b"
        r"[:;,.\-]*\s*(?:\s+\w+){0,3}\s*[:;,.\-]?\s*"
        r"(email|e-mail|gmail|inbox|caixa de entrada)\b"
        r"|\b(email|e-mail|gmail|inbox|caixa de entrada)\b"
        r"[:;,.\-]*\s*(?:\s+\w+){0,3}\s*[:;,.\-]?\s*"
        r"(busca|busque|buscar|procura|procure|procurar|baixa|baixar|puxa|puxar)\b",
        re.IGNORECASE)

    @classmethod
    def is_email_fetch(cls, text: str) -> bool:
        """'buscar no email', 'baixa as faturas do e-mail de julho'..."""
        return bool(cls._EMAIL_FETCH_RE.search(text))

    _ALERT_EXPLAIN = {
        "low_confidence": "baixa confiança — encontrei um pareamento provável, mas a "
                          "certeza ficou abaixo do limite; confira manualmente",
        "amount_mismatch": "valor diferente — pagamento e fatura casam, mas os valores não batem",
        "missing_payment": "fatura sem pagamento — não achei o pagamento no extrato "
                           "(pode estar pendente, pago em outro cartão/conta, ou extrato incompleto)",
        "missing_invoice": "pagamento sem fatura — o pagamento não tem fatura correspondente "
                           "(pode ser taxa/anuidade, ou falta enviar a fatura)",
        "duplicate_payment": "pagamento duplicado — dois pagamentos parecem ser da mesma fatura",
        "late_payment": "pagamento atrasado — pago depois do vencimento",
    }

    def decide(self, session: ChatSession, text: str) -> Decision:
        d = self.match(session, text)
        if d is not None:
            return d
        return Decision(reply=self._fallback(session), action="ask")

    def match(self, session: ChatSession, text: str) -> Decision | None:
        """Intenções determinísticas (rápidas). None = não casou com nada."""
        if self._EMAIL_FETCH_RE.search(text):
            return Decision(reply="", action="fetch_email")
        t = text.lower()
        if self._LIST_RE.search(text):
            return Decision(reply="", action="list_files")
        if self._RESET_RE.search(text):
            return Decision(reply="", action="reset")
        if session.has_extrato and session.has_faturas:
            if any(k in t for k in self._RUN_STRONG):
                return Decision(reply="", action="run")
            # "sim", "pode", "vai" só valem em mensagem curta (evita "pode me ajudar?")
            # \b...\b: palavra inteira — "go" não pode casar dentro de "agosto"
            if len(text.split()) <= 3 and any(re.search(rf"\b{k}\b", t) for k in self._RUN_SHORT):
                return Decision(reply="", action="run")
        if self._HELP_RE.search(text):
            return Decision(reply=self._help(session), action="ask")
        if self._REPORT_RE.search(text):
            return self._report(session)
        if self._ALERTS_RE.search(text):
            return Decision(reply=self._alerts(session), action="ask")
        if self._DOUBT_RE.search(text):
            return Decision(reply=self._doubt(session), action="ask")
        return None

    # -- respostas específicas com os dados da conversa -------------------- #
    def _report(self, session: ChatSession) -> Decision:
        """Pede o relatório/resumo: reenvia o último (ou roda a conciliação)."""
        if session.last_report is None and session.has_extrato and session.has_faturas:
            return Decision(reply="", action="run")
        return Decision(reply="", action="send_report")

    def _alerts(self, session: ChatSession) -> str:
        """Pergunta sobre alertas/conciliação: explica com os dados REAIS do
        último relatório (não resposta genérica)."""
        report = session.last_report
        if report is None:
            base = ("Ainda não rodei a conciliação nesta conversa, então não há alertas "
                    "para explicar.")
        elif not report.inconsistencies:
            base = "✅ A última conciliação não gerou alertas — tudo bateu."
        else:
            lines = [f"🔎 Última conciliação: {len(report.inconsistencies)} alerta(s). "
                     "O que cada tipo significa:"]
            seen: set[str] = set()
            for inc in report.inconsistencies:
                if inc.type.value in seen:
                    continue
                seen.add(inc.type.value)
                lines.append("• " + self._ALERT_EXPLAIN.get(inc.type.value, inc.type.value))
            lines.append("")
            lines.append("Alertas desta conversa:")
            for inc in report.inconsistencies[:10]:
                emoji = {"high": "🔴", "medium": "🟠", "low": "🟡"}[inc.severity.value]
                lines.append(f"{emoji} {inc.message}")
            if len(report.inconsistencies) > 10:
                lines.append(f"… e mais {len(report.inconsistencies) - 10}.")
            lines.append("Para o relatório completo, diga 'me manda o relatório'.")
            return "\n".join(lines)
        if session.has_extrato and session.has_faturas:
            base += " Digite 'rodar' que eu concilio e explico cada alerta."
        else:
            missing = []
            if not session.has_extrato:
                missing.append("extrato bancário")
            if not session.has_faturas:
                missing.append("faturas")
            base += f" Para gerar o relatório, preciso de: {', '.join(missing)}."
        return base

    # -- respostas de apoio ------------------------------------------------ #
    def _doubt(self, session: ChatSession) -> str:
        """Cliente quer tirar uma dúvida: pergunta o que exatamente precisa."""
        lines = [
            "Claro, posso ajudar! Qual é a sua dúvida? 😊",
            "",
            "Pode ser sobre:",
            "• uma NOTA específica — me diga o número (ex.: 'qual o ISS da nota 123?');",
            "• um IMPOSTO (ISS, ICMS, ICMS-ST, PIS, COFINS, retenções, DIFAL);",
            "• o REGIME da empresa (Simples Nacional, MEI, Presumido, Real);",
            "• a CONCILIAÇÃO (como funciona, o que significa um alerta).",
        ]
        nums = sorted({a.number for a in session.audits if a.number})
        if nums:
            lines.append(f"\nAs notas que já recebi: {', '.join(nums[:8])}."
                         + ("…" if len(nums) > 8 else ""))
        lines.append("\nÉ só me contar o que você precisa!")
        return "\n".join(lines)

    def _help(self, session: ChatSession) -> str:
        """Cliente pede ajuda / quer saber o que o bot faz."""
        return (
            "Claro! Te resumo o que eu faço:\n\n"
            "📎 CONCILIAÇÃO — você me manda o EXTRATO bancário + as FATURAS/NOTAS "
            "(PDF, CSV, Excel, OFX ou FOTOS) e eu concilio cada pagamento com a fatura.\n\n"
            "🧾 AUDITORIA FISCAL — para cada nota, verifico o cálculo dos impostos "
            "(ISS, ICMS, PIS, COFINS, retenções), o regime da empresa e aponto divergências.\n\n"
            "💬 PERGUNTAS FISCAIS — respondo sobre alíquotas, regimes, retenções, "
            "DIFAL, ICMS-ST (substituição tributária), LC 116/2003 etc. — genéricas, "
            "sobre uma nota, sobre o conjunto "
            "das notas ou sobre o SEU CASO (ex.: 'empresa de software em BH prestando "
            "serviço para POA: pago DIFAL?').\n\n"
            "Comandos úteis:\n"
            "• 'rodar' — concilia agora\n"
            "• 'listar arquivos' — mostra o que já recebi\n"
            "• 'recomeçar' — limpa a conversa\n"
            "• 'buscar no email' — puxo as notas da sua caixa de entrada e concilio\n\n"
            "Como posso te ajudar?"
        )

    def _fallback(self, session: ChatSession) -> str:
        """Mensagem que não casou com nenhuma intenção: orienta com opções
        reais conforme o estado da conversa (nunca resposta fora de contexto)."""
        lines = ["Como posso te ajudar? 😊"]
        if session.has_extrato and session.has_faturas:
            lines += [
                "",
                "Já recebi extrato e faturas. Você pode:",
                "• digitar 'rodar' para eu conciliar agora;",
                "• perguntar sobre uma nota (ex.: 'qual o ISS da nota 123?');",
                "• perguntar sobre impostos, regimes ou retenções;",
                "• me mandar mais arquivos.",
            ]
        else:
            missing = []
            if not session.has_extrato:
                missing.append("extrato bancário")
            if not session.has_faturas:
                missing.append("faturas/notas")
            lines += [
                "",
                f"Para conciliar, preciso de: {' e '.join(missing)}.",
                "Pode enviar os arquivos (PDF, CSV, Excel, OFX ou FOTOS).",
                "",
                "Ou, se preferir, me faça uma pergunta fiscal (impostos, regimes, "
                "retenções) ou sobre uma nota específica.",
            ]
        lines += ["", "O que você precisa?"]
        return "\n".join(lines)


class LLMBrain:
    """Cérebro via LLM (Ollama): faz as perguntas certas em linguagem natural."""

    SYSTEM = (
        "Você é o Conciliador, assistente de conciliação de contas e faturas no Telegram. "
        "Estado: arquivos={files}; período={period}.\n"
        "Responda SOMENTE JSON válido, sem texto fora:\n"
        '{{"reply": "resposta curta em pt-BR (máx. 2 frases), direta à pergunta do cliente", '
        '"action": "ask"|"run"|"list_files"|"reset", "period": "AAAA-MM"|null}}\n'
        "Regras: \"run\" só com ≥1 extrato E ≥1 fatura E cliente quer prosseguir; "
        "falta arquivo → \"ask\" dizendo o que falta; ver arquivos → \"list_files\"; "
        "recomeçar → \"reset\"; \"period\" só se o cliente citou mês/ano claro, senão null. "
        "Seja breve e direto; responda perguntas específicas com a informação pedida."
    )

    def __init__(self, client: OllamaClient, history_turns: int = 6):
        self.client = client
        self.history_turns = history_turns
        self._fallback = TemplateBrain()

    def decide(self, session: ChatSession, text: str) -> Decision:
        system = self.SYSTEM.format(files=session.files_label(),
                                    period=session.period or "não definido")
        messages = session.history[-self.history_turns * 2:] + [
            {"role": "user", "content": text}
        ]
        try:
            data = self.client.chat_json(system, messages)
            reply = str(data.get("reply", "")).strip()
            action = str(data.get("action", "ask")).strip().lower()
            if action not in ("ask", "run", "list_files", "reset"):
                action = "ask"
            period = data.get("period")
            period = str(period).strip() if period else None
            if not reply:
                reply = "Pode me contar um pouco mais?"
            return Decision(reply=reply, action=action, period=period)
        except Exception:  # noqa: BLE001 - LLM indisponível/lento -> template
            return self._fallback.decide(session, text)


class HybridBrain:
    """Cérebro híbrido: comandos/intenções conhecidas respondem na hora
    (determinístico); perguntas livres vão para o LLM (resposta específica).
    Se o LLM falhar/estourar o timeout, cai no template contextual."""

    def __init__(self, client: OllamaClient, history_turns: int = 3):
        self._template = TemplateBrain()
        self._llm = LLMBrain(client, history_turns=history_turns)

    def decide(self, session: ChatSession, text: str) -> Decision:
        d = self._template.match(session, text)
        if d is not None:
            return d
        return self._llm.decide(session, text)


# --------------------------------------------------------------------------- #
# Agente (orquestra sessão, arquivos e conciliação)
# --------------------------------------------------------------------------- #
class ConciliationAgent:
    def __init__(self, cfg: dict, brain, pipeline_llm, base_dir: str | Path,
                 extract_llm=None, advisor=None, api=None):
        self.cfg = cfg
        self.brain = brain
        self.pipeline_llm = pipeline_llm
        self.base_dir = Path(base_dir)
        # API do Telegram: feedback imediato em operações longas (busca no
        # e-mail) — sem isso o chat fica mudo por minutos e parece travado
        self.api = api
        self.sessions: dict[int, ChatSession] = {}
        self.auto_run = bool(cfg["bot"].get("auto_run", True))
        self.history_turns = int(cfg["bot"].get("history_turns", 6))
        # conselheiro fiscal: responde perguntas sobre tributos/notas (LC 116 etc.)
        self.advisor = advisor or TaxAdvisor()
        # caminho 2: busca de notas no e-mail do cliente (IMAP); None = desligado
        self.email_cfg = build_email_config(cfg)

        # auditoria fiscal de fotos
        self.extract_llm = extract_llm or FallbackClient()
        self.tax_tolerance = Decimal(str(cfg.get("tax", {}).get("tolerance", "0.05")))
        self.ocr_cfg = cfg.get("ocr", {}) or {}

        m = cfg["matching"]
        self._matcher = DeterministicMatcher(
            amount_tolerance=Decimal(m["amount_tolerance"]),
            date_window_days=int(m["date_window_days"]),
            name_threshold=float(m["name_threshold"]),
        )
        self._recon_kwargs = dict(
            amount_tolerance=Decimal(m["amount_tolerance"]),
            fuzzy_confidence_threshold=float(m["fuzzy_confidence_threshold"]),
            late_tolerance_days=int(m["late_tolerance_days"]),
            low_confidence_threshold=float(m["low_confidence_threshold"]),
        )

    _STATE_NAME = ".estado_bot.json"

    def get_session(self, chat_id: int) -> ChatSession:
        if chat_id not in self.sessions:
            workdir = self.base_dir / str(chat_id)
            workdir.mkdir(parents=True, exist_ok=True)
            session = ChatSession(chat_id=chat_id, workdir=workdir)
            session.hashes = self._index_workdir(workdir)
            self._restore_session(session)
            if session.files:
                # conversa já em andamento (arquivos persistidos em disco):
                # após restart do bot não reenvio o welcome inteiro
                session.welcomed = True
            # estado persistido: o bot não 'esquece' que já rodou a conciliação
            # (sem isso, após restart o auto_run re-rodava e reenviava o relatório)
            state = self._load_state(workdir)
            if state:
                session.executed_file_count = int(state.get("executed_file_count", 0))
                session.pending_email_period = bool(state.get("pending_email_period", False))
            self.sessions[chat_id] = session
        return self.sessions[chat_id]

    @staticmethod
    def _load_state(workdir: Path) -> dict:
        p = workdir / ConciliationAgent._STATE_NAME
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - estado corrompido: ignora
            return {}

    @staticmethod
    def _save_state(workdir: Path, executed_file_count: int,
                    pending_email_period: bool = False) -> None:
        p = workdir / ConciliationAgent._STATE_NAME
        try:
            p.write_text(json.dumps({
                "executed_file_count": executed_file_count,
                "pending_email_period": pending_email_period,
            }), encoding="utf-8")
        except Exception:  # noqa: BLE001 - falha de disco não derruba o bot
            pass

    @staticmethod
    def _index_workdir(workdir: Path) -> dict[str, str]:
        """Mapa sha256 -> nome dos arquivos já no diretório (anti-duplicata)."""
        idx: dict[str, str] = {}
        for f in sorted(workdir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                idx.setdefault(hashlib.sha256(f.read_bytes()).hexdigest(), f.name)
        return idx

    def _restore_session(self, session: ChatSession):
        """Reconstrói o estado em memória a partir dos arquivos já no diretório
        (após reinício do bot, a sessão não 'esquece' o que já recebeu)."""
        for r in parse_directory(session.workdir):
            if not r.transactions and not r.invoices and not r.audits:
                continue
            session.files.append({
                "name": Path(r.path).name,
                "n_txns": len(r.transactions),
                "n_invoices": len(r.invoices),
                "errors": r.errors,
            })
            session.transactions.extend(r.transactions)
            session.audits.extend(r.audits)

    # -- arquivos ---------------------------------------------------------- #
    def _dup_response(self, session: ChatSession, existing_name: str,
                      caption: str | None = None) -> BotResponse:
        """Arquivo/foto já recebido antes: reenvia a análise da nota — o cliente
        manda a mesma nota de novo justamente para ver a análise e tirar dúvidas
        sobre ela. A nota vira a mais recente da conversa, então perguntas com
        pronome ('qual o ISS dela?') passam a apontar para ela. Pergunta fiscal na
        legenda ('tem DIFAL?') é respondida sobre a nota, como no caminho normal."""
        resp = BotResponse()
        audit = next((a for a in session.audits if a.source == existing_name), None)
        if audit is not None:
            for i, a in enumerate(session.audits):
                if a is audit:
                    session.audits.pop(i)
                    break
            session.audits.append(audit)
            session.last_discussed = audit  # a nota reenviada vira a em pauta
            # nota repetida: vai DIRETO no cálculo dos impostos — sem avisar que
            # já estava na conversa e sem pedir para rodar de novo / enviar algo
            if caption and self.advisor.is_tax_question(caption, session.audits):
                resp.texts.append(self.advisor.answer_for_document(caption, [audit]))
            resp.texts.append(self.advisor.full_report(audit))
            return resp
        # arquivo repetido que não é nota (ex.: extrato) — mantém o aviso
        resp.texts.append(
            f"🔁 Este arquivo já está na conversa ({existing_name}) — vou usar a "
            "cópia que já recebi."
        )
        if caption and self.advisor.is_tax_question(caption, session.audits):
            resp.texts.append(
                "Não tenho a análise desta nota em mãos agora — me pergunte citando o "
                "número dela (ex.: \"tem DIFAL na nota 123?\") ou me manda a nota de novo."
            )
        if session.has_extrato and session.has_faturas:
            resp.texts.append(
                "Para conciliar tudo com o extrato, digite 'rodar' — ou me pergunte "
                "algo específico sobre uma nota.")
        else:
            missing = []
            if not session.has_extrato:
                missing.append("extrato bancário")
            if not session.has_faturas:
                missing.append("faturas")
            resp.texts.append(f"Ainda falta: {', '.join(missing)}. Pode enviar?")
        return resp

    def handle_document(self, session: ChatSession, filename: str, data: bytes,
                        caption: str | None = None) -> BotResponse:
        safe = Path(filename).name or "arquivo"
        digest = hashlib.sha256(data).hexdigest()
        if digest in session.hashes:
            return self._dup_response(session, session.hashes[digest], caption)
        dest = session.workdir / safe
        n = 1
        while dest.exists():
            dest = session.workdir / f"{n}_{safe}"
            n += 1
        dest.write_bytes(data)
        session.hashes[digest] = dest.name

        parsed = parse_file(dest)
        session.audits.extend(parsed.audits)
        if parsed.audits:
            session.last_discussed = parsed.audits[-1]  # nota recebida vira a em pauta
        session.transactions.extend(parsed.transactions)
        session.files.append({
            "name": dest.name,
            "n_txns": len(parsed.transactions),
            "n_invoices": len(parsed.invoices),
            "errors": parsed.errors,
        })

        resp = BotResponse()
        err_note = f" (avisos: {'; '.join(parsed.errors)})" if parsed.errors else ""
        nf_note = ""
        if parsed.invoices:
            types = sorted({i.nf_type for i in parsed.invoices if i.nf_type})
            if types:
                nf_note = f" [{', '.join(types)}]"
        resp.texts.append(
            f"✅ Recebi {dest.name}: {len(parsed.transactions)} transações / "
            f"{len(parsed.invoices)} faturas{nf_note}{err_note}."
        )
        # pergunta fiscal na legenda ("mandou a nota com 'tem DIFAL?'"):
        # resposta direta sobre a nota, ANTES do parecer completo
        if caption and self.advisor.is_tax_question(caption, session.audits):
            new_audits = [a for a in session.audits if a.source == dest.name]
            if new_audits:
                resp.texts.append(self.advisor.answer_for_document(caption, new_audits))
        # parecer fiscal completo das notas extraídas do arquivo
        for a in parsed.audits:
            resp.texts.append(self.advisor.full_report(a))
        if not parsed.transactions and not parsed.invoices:
            resp.texts.append("⚠️ Não consegui extrair dados deste arquivo. "
                              "Confira o formato/cabeçalhos e envie de novo.")
            return resp

        if not session.has_extrato:
            resp.texts.append("Falta o extrato bancário. Pode enviá-lo?")
        elif not session.has_faturas:
            resp.texts.append("Faltam as faturas. Pode enviá-las?")
        elif self.auto_run and session.executed_file_count == 0:
            # 1ª vez que o conjunto fica completo: concilia tudo
            resp.texts.append("Extrato e faturas completos. Rodando a conciliação…")
            self._run(session, resp)
        elif parsed.transactions:
            # EXTRATO novo após conciliação já ter rodado: reconcilia APENAS este
            # extrato com as faturas do período dele (+ faturas pendentes) — sem
            # misturar com períodos anteriores e gerar alertas aleatórios
            self._run_period(session, resp, extrato_file=dest.name)
        else:
            # FATURA nova: se já há extrato na conversa, concilia na hora (o _run
            # filtra pelo período e pede o arquivo que faltar); sem extrato a
            # fatura fica pendente até chegar um extrato que cubra o pagamento
            if session.has_extrato:
                resp.texts.append("⏳ Rodando a conciliação… pode levar alguns segundos.")
                self._run(session, resp)
            else:
                for inv in parsed.invoices:
                    session.pending_invoice_ids.add(inv.id)
                dues = sorted({i.due_date.isoformat() for i in parsed.invoices if i.due_date})
                due_txt = f" (vencimento: {', '.join(dues)})" if dues else ""
                resp.texts.append(
                    f"Para conciliar essa(s) fatura(s){due_txt}, me manda um extrato que "
                    "contenha o pagamento dela(s) — do período do vencimento. Assim que ele "
                    "chegar, eu concilio esse extrato com as faturas do período.")
        return resp

    # -- fotos de fatura --------------------------------------------------- #
    def handle_photo(self, session: ChatSession, filename: str, data: bytes,
                     caption: str | None = None) -> BotResponse:
        """Recebe uma foto: OCR -> extração -> auditoria fiscal -> fatura p/ conciliar."""
        safe = Path(filename).name or "foto.jpg"
        if not safe.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            safe = f"foto_{safe}"
        digest = hashlib.sha256(data).hexdigest()
        if digest in session.hashes:
            return self._dup_response(session, session.hashes[digest], caption)
        dest = session.workdir / safe
        n = 1
        while dest.exists():
            dest = session.workdir / f"{n}_{safe}"
            n += 1
        dest.write_bytes(data)
        session.hashes[digest] = dest.name

        resp = BotResponse()

        # 1) OCR
        try:
            text = ocr_image(
                dest,
                tesseract_bin=self.ocr_cfg.get("tesseract_bin"),
                languages=self.ocr_cfg.get("languages", "por+eng"),
                psm=int(self.ocr_cfg.get("psm", 3)),
                timeout=float(self.ocr_cfg.get("timeout", 120)),
            )
        except OCRError as e:
            resp.texts.append(f"⚠️ Não consegui ler a imagem: {e}")
            return resp
        if len(text.strip()) < 20:
            resp.texts.append(
                "⚠️ Não encontrei texto legível nesta foto. Tente uma imagem mais "
                "nítida, bem iluminada e de frente para a fatura.")
            return resp

        # 2) extração + auditoria fiscal
        audit = build_audit(self.extract_llm, text, source=dest.name,
                            tolerance=self.tax_tolerance)
        if audit is None or audit.amount is None:
            resp.texts.append(
                "⚠️ Não identifiquei uma fatura nesta imagem. Confirme que é uma foto "
                "de fatura/nota fiscal legível e envie de novo.")
            return resp

        # 3) registra a fatura p/ conciliação + guarda o parecer
        inv = audit_to_invoice(audit, dest.name)
        if inv:
            session.invoices_from_photos.append(inv)
        session.audits.append(audit)
        session.last_discussed = audit  # foto analisada vira a nota em pauta

        resp.texts.append(
            f"✅ Fatura {inv.number} ({inv.issuer}) de R$ {inv.amount} recebida.")
        # pergunta fiscal na legenda ("mandou a foto com 'tem DIFAL?'"):
        # resposta direta sobre a nota, ANTES do parecer completo
        if caption and self.advisor.is_tax_question(caption, session.audits):
            resp.texts.append(self.advisor.answer(caption, [audit]))
        resp.texts.append(self.advisor.full_report(audit))

        # 4) orienta o próximo passo
        if not session.has_extrato:
            resp.texts.append("Falta o extrato bancário. Pode enviá-lo?")
        elif self.auto_run and session.executed_file_count == 0:
            # 1ª vez que o conjunto fica completo: concilia tudo
            resp.texts.append("Extrato e faturas completos. Rodando a conciliação…")
            self._run(session, resp)
        else:
            # fatura nova: fica pendente até chegar um extrato que cubra o
            # pagamento dela (não re-rodo a conciliação com o extrato antigo)
            if inv:
                session.pending_invoice_ids.add(inv.id)
            due_txt = f" (vencimento: {inv.due_date.isoformat()})" if inv and inv.due_date else ""
            resp.texts.append(
                f"Para conciliar a fatura{due_txt}, me manda um extrato que contenha o "
                "pagamento dela — do período do vencimento. Assim que ele chegar, eu "
                "concilio esse extrato com as faturas do período.")
        return resp

    # -- caminho 2: notas do e-mail ---------------------------------------- #
    def _audit_missing_files(self, session: ChatSession, names: list[str]) -> None:
        """Anexos SEM parecer fiscal (faturas genéricas / NF que o parser
        específico não cobriu): lê o texto do arquivo e monta a auditoria —
        assim as faturas do e-mail entram no cálculo dos impostos mesmo
        quando o arquivo já estava na conversa (sem re-parse)."""
        from ..parsing.pdf_parser import extract_fatura_audit, pdf_to_text
        for name in names:
            if any(a.source == name for a in session.audits):
                continue
            path = session.workdir / name
            if not path.exists():
                continue
            text = ""
            try:
                if path.suffix.lower() == ".pdf":
                    text = pdf_to_text(path)
                elif path.suffix.lower() in (".txt", ".log", ".text"):
                    text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - PDF ilegível não derruba a busca
                continue
            if not text:
                continue
            audit = extract_fatura_audit(text, source=name)
            if audit is not None:
                session.audits.append(audit)
                session.last_discussed = audit


    def handle_fetch_email(self, session: ChatSession, resp: BotResponse,
                           period: str | None = None) -> None:
        """Busca notas/faturas em anexo no e-mail do cliente, calcula os
        impostos de TODAS (novas e as que já estavam repetidas na conversa)
        e envia o resultado na hora — sem pedir para o cliente digitar
        'rodar' nem enviar nada. Se o extrato já está na conversa, a
        conciliação também roda sozinha, junto com o cálculo.

        Sem período (no comando nem na conversa), pergunta o mês/data e
        guarda a pendência: a próxima mensagem com data/mês dispara a busca.
        """
        if self.email_cfg is None:
            resp.texts.append(
                "⚠️ A busca no e-mail ainda não está configurada (falta "
                "EMAIL_IMAP_USER e EMAIL_IMAP_PASSWORD no .env, e email.enabled: true "
                "no config.yaml). Por enquanto, me manda o extrato e as faturas por "
                "aqui mesmo, que eu concilio na hora."
            )
            return
        if period is None:
            # SEM mês na mensagem: PERGUNTA. O session.period de mensagens
            # anteriores NÃO vale para a busca no e-mail — o cliente pediu
            # "buscar no email" sem dizer o mês, então a busca só acontece
            # depois que ele responde qual mês (pending_email_period)
            session.pending_email_period = True
            self._save_state(session.workdir, session.executed_file_count,
                             pending_email_period=True)
            resp.texts.append(
                "📧 De qual mês (ou data) devo buscar as notas no e-mail?\n"
                "Ex.: 'agosto', '2026-08' ou '15/08/2026'.")
            return
        period_txt = f" de {period_label(period)}"
        # a busca define o período de trabalho: um "rodar" depois disso
        # concilia SÓ as notas deste mês (senão mistura com notas antigas)
        session.period = period
        buscando = (f"📧 Buscando suas notas no e-mail{period_txt}… "
                    "pode levar alguns segundos.")
        if self.api is not None:
            # envia na hora: a resposta final (resp.texts) só sai depois que
            # a busca termina, e ela pode levar minutos
            self.api.send_text(session.chat_id, buscando)
        else:
            resp.texts.append(buscando)
        try:
            found = fetch_invoice_files(self.email_cfg, period=period)
        except EmailFetchError as e:
            resp.texts.append(f"⚠️ Não consegui acessar o e-mail: {e}")
            return
        if not found:
            resp.texts.append(
                f"📭 Não encontrei notas/faturas em anexo no e-mail{period_txt}. "
                "Se as notas chegam com outro assunto, me avisa que eu ajusto a busca. "
                "Ou me manda os arquivos por aqui mesmo."
            )
            return

        # anexos novos -> parse; anexos repetidos -> reusa o que já está na
        # conversa. Nos DOIS casos o cálculo é feito e enviado: nota repetida
        # não é motivo para ficar sem resposta
        new_files: list[str] = []
        dup_files: list[str] = []
        n_txns = 0
        n_invs = 0
        dup_txns = 0
        dup_invs = 0
        entries = {f["name"]: f for f in session.files}
        for ef in found:
            safe = Path(ef.filename).name or "anexo"
            digest = hashlib.sha256(ef.data).hexdigest()
            if digest in session.hashes:
                stored = session.hashes[digest]
                dup_files.append(stored)
                ent = entries.get(stored)
                if ent:
                    dup_txns += ent["n_txns"]
                    dup_invs += ent["n_invoices"]
                continue
            dest = session.workdir / safe
            n = 1
            while dest.exists():
                dest = session.workdir / f"{n}_{safe}"
                n += 1
            dest.write_bytes(ef.data)
            session.hashes[digest] = dest.name
            parsed = parse_file(dest)
            session.audits.extend(parsed.audits)
            if parsed.audits:
                session.last_discussed = parsed.audits[-1]
            session.transactions.extend(parsed.transactions)
            session.files.append({
                "name": dest.name,
                "n_txns": len(parsed.transactions),
                "n_invoices": len(parsed.invoices),
                "errors": parsed.errors,
            })
            n_txns += len(parsed.transactions)
            n_invs += len(parsed.invoices)
            new_files.append(dest.name)

        # faturas que viraram Invoice mas SEM parecer fiscal (PDF genérico,
        # ex.: fatura de operadora): monta a auditoria a partir do texto do
        # arquivo — assim os impostos são lidos e calculados, igual às NF-e
        self._audit_missing_files(session, [*new_files, *dup_files])

        # parecer fiscal de cada nota encontrada (novas + repetidas), sem
        # repetir a mesma nota duas vezes
        audits: list[InvoiceAudit] = []
        seen: set = set()
        for name in [*new_files, *dup_files]:
            for a in session.audits:
                if a.source != name:
                    continue
                k = (a.number, str(a.amount), (a.issuer or "").lower())
                if k in seen:
                    continue
                seen.add(k)
                audits.append(a)
        if audits:
            session.last_discussed = audits[-1]

        lines: list[str] = []
        if new_files:
            lines.append(f"✅ Encontrei {len(new_files)} arquivo(s) de nota no seu e-mail:")
            for name in new_files[:10]:
                lines.append(f"• {name}")
            if len(new_files) > 10:
                lines.append(f"… e mais {len(new_files) - 10}.")
            lines.append(f"({n_txns} transações / {n_invs} faturas extraídas)")
        if dup_files:
            lines.append(f"🔁 {len(dup_files)} anexo(s) já estava(m) na conversa — "
                         "recalculei e segue o resultado:")
        if lines:
            resp.texts.append("\n".join(lines))

        if not audits and not n_txns and not n_invs and not dup_txns and not dup_invs:
            resp.texts.append(
                "⚠️ Não consegui extrair dados dos anexos encontrados. Confira o "
                "formato (PDF, CSV, Excel, OFX) e me avisa.")
            return

        # "calcular tudo e mandar": parecer fiscal completo de cada nota
        for a in audits:
            resp.texts.append(self.advisor.full_report(a))

        # só concilia na hora se um EXTRATO de verdade cobre o MÊS de
        # referência — com extrato de outro período a conciliação sai errada;
        # "transações" de fatura/reprint (1-2 linhas, ou fatura+pagamento no
        # mesmo arquivo) não valem como extrato; senão, pede o extrato do mês
        try:
            _y, _m = (int(x) for x in period.split("-")[:2])
            _non_extrato = {f["name"] for f in session.files
                            if f["n_txns"] > 0 and f["n_invoices"] > 0} | \
                           {f["name"] for f in session.files
                            if 0 < f["n_txns"] <= 2 and f["n_invoices"] == 0}
            extrato_cobre = any(
                t.date and t.date.year == _y and t.date.month == _m
                and t.source_file not in _non_extrato
                for t in session.transactions
            )
        except (ValueError, AttributeError):
            extrato_cobre = False
        if extrato_cobre and session.has_extrato and session.has_faturas:
            # extrato do período já está na conversa: concilia na hora
            resp.texts.append("⏳ Rodando a conciliação com o extrato da conversa…")
            self._run(session, resp)
        elif not session.has_extrato or not extrato_cobre:
            resp.texts.append(
                f"📎 Notas salvas nesta conversa e impostos calculados acima. "
                f"Para conciliar, me manda o extrato bancário de "
                f"{period_label(period)} (PDF, CSV, Excel ou OFX) — assim que ele "
                "chegar, eu concilio com as notas do mesmo período.")
        else:
            # extrato cobre o período, mas as notas não viraram faturas
            resp.texts.append(
                "⚠️ Não identifiquei faturas nestes anexos. Confira se os anexos do "
                "e-mail são as notas (PDF, CSV ou Excel).")
        return

    # -- texto ------------------------------------------------------------- #
    def _greeting_reply(self, session: ChatSession) -> str:
        """Cumprimento de volta + passo a passo + oferta de ajuda."""
        lines = [
            "Olá! Tudo bem? 😊 Que bom ter você por aqui.",
            "",
            "Te explico como funciona, em 4 passos:",
            "1️⃣ Envie seu EXTRATO bancário (PDF, CSV, Excel ou OFX).",
            "2️⃣ Envie suas FATURAS/NOTAS (PDF, CSV, Excel — ou FOTOS).",
            "3️⃣ Eu concilio cada pagamento com a fatura e verifico o cálculo dos "
            "impostos (ISS, ICMS, PIS, COFINS, retenções) e o regime da empresa.",
            "4️⃣ Você recebe o resumo + relatório (CSV/Markdown) com os alertas.",
            "",
            "Ou, pelo caminho 2, digite 'buscar no email' que eu puxo as notas da "
            "sua caixa de entrada, calculo tudo e te mando o resultado na hora.",
            "",
        ]
        if session.has_extrato and session.has_faturas:
            lines.append("Já recebi seu extrato e suas faturas. Digite 'rodar' para eu "
                         "conciliar agora, ou me mande mais arquivos.")
        elif session.has_extrato:
            lines.append("Já recebi seu extrato — agora só faltam as faturas/notas.")
        elif session.has_faturas:
            lines.append("Já recebi suas faturas — agora só falta o extrato bancário.")
        else:
            lines.append("Pode começar enviando o extrato ou as faturas.")
        lines += [
            "",
            "Também respondo perguntas fiscais (alíquotas, regimes, retenções, DIFAL…).",
            "",
            "Como posso te ajudar?",
        ]
        return "\n".join(lines)

    # -- continuação de caso de DIFAL (multi-turno) -------------------------- #
    # O bot pede os dois estados ou a natureza da operação; o cliente responde
    # sem repetir a pergunta ("origem SP, destino MG", "é mercadoria"). Sem
    # palavra fiscal na resposta, ela cairia no cérebro template — por isso o
    # texto pendente é juntado à pergunta original e o caso reanalisado.
    _DIFAL_NATURE_WORDS = (
        "servico", "servicos", "mercadoria", "mercadorias", "produto",
        "produtos", "venda", "vender", "compra", "comprar", "software",
        "licenca", "origem", "destino",
        # destinatário (a pergunta dos 3 casos: revenda x uso/consumo x PF)
        "revenda", "revender", "usar", "consumir", "consumo", "imobilizado",
        "fisica", "consumidor", "contribuinte", "nao contribuinte",
    )

    # resposta numérica à lista de casos ("1", "2", "3") -> frase que o
    # classificador de destinatário entende
    _DIFAL_CASE_NUM = {
        "1": "é para revenda",
        "2": "vai usar e consumir na própria empresa ou ativo imobilizado",
        "3": "é pessoa fisica nao contribuinte",
    }

    def _difal_followup(self, t: str) -> tuple[bool, object]:
        """Se o texto completa um caso de DIFAL pendente.

        Retorna ``(is_followup, follow_case)``: ``follow_case`` é o caso
        parseado do texto (None se não cita UF/cidade — ex.: "é mercadoria").
        """
        follow_case = analyze_difal_case(t)
        if follow_case is not None:
            return True, follow_case
        if any(w in t for w in self._DIFAL_NATURE_WORDS):
            return True, None
        # alíquota interna informada ("a interna é 19%", "19%")
        if re.search(r"interna|\d{1,2}\s*%", t):
            return True, None
        return False, None

    def _pending_difal(self, text: str) -> str | None:
        """Pergunta de caso de DIFAL incompleta (faltou um estado, a
        natureza da operação, o caso do destinatário ou a alíquota interna
        do destino) -> guardar p/ juntar com a resposta do cliente
        na próxima mensagem."""
        if "difal" not in normalize_name(text):
            return None
        case = analyze_difal_case(text)
        if case is None:
            return None
        if case.kind in ("desconhecido", "ambiguo"):
            return text
        if case.kind == "mercadoria" and (case.origin is None) != (case.destination is None):
            return text
        # mercadoria interestadual: ainda depende de saber o caso do
        # destinatário (revenda x uso/consumo x PF) ou a alíquota interna
        if precisa_destinatario(case) or precisa_interna(case):
            return text
        return None

    def _is_period_request(self, text: str) -> bool:
        """Mensagem curta pedindo a conciliação de um período (ex.: 'agosto',
        'relatório de julho'), sem apontar para um documento específico."""
        t = text.lower()
        if len(t.split()) > 8:
            return False
        # cita nota/fatura com número -> é sobre um documento específico,
        # não um pedido de conciliação de período
        if re.search(r"\d", t) and re.search(r"\b(nota|nf|nfe|nfse|fatura)\b", t):
            return False
        return True

    def go_home(self, session: ChatSession) -> None:
        """Abandona o raciocínio em andamento e volta para a 1ª mensagem.

        Limpa o histórico do LLM (o 'raciocínio' da conversa), as pendências
        (caso de DIFAL em andamento, busca de e-mail aguardando período) e a
        nota em pauta. Arquivos, notas, extrato e relatório são MANTIDOS —
        só a conversa recomeça do zero.
        """
        session.history.clear()
        session.pending_difal_text = None
        session.pending_email_period = False
        self._save_state(session.workdir, session.executed_file_count,
                         pending_email_period=False)
        session.last_discussed = None
        session.welcomed = True  # a 1ª mensagem (welcome) é enviada por aqui

    def handle_text(self, session: ChatSession, text: str) -> BotResponse:
        # "voltar ao início" (/start): SEMPRE antes de qualquer outro fluxo
        # (pendência de e-mail, DIFAL, pergunta fiscal, cérebro) — abandona o
        # raciocínio em andamento e devolve a 1ª mensagem. Os arquivos/notas já
        # enviados ficam na conversa; nenhum outro texto faz isso.
        if is_home(text):
            self.go_home(session)
            resp = BotResponse(texts=[WELCOME])
            if session.file_count:
                resp.texts.append(
                    "📂 Mantive o que você já enviou nesta conversa: "
                    f"{session.files_label()} — digite 'listar arquivos' para ver.")
            return resp

        period = parse_period(text)
        if period:
            session.period = period

        # "buscar no email" sem período: perguntei o mês/data e o cliente
        # respondeu (ex.: "agosto", "15/08/2026") -> busco naquele período
        if session.pending_email_period:
            p = parse_day_period(text) or period
            if p is not None:
                # mês/data informado: limpa a pendência e busca + calcula
                # os impostos na hora (sem cair no fluxo de extrato)
                session.pending_email_period = False
                self._save_state(session.workdir, session.executed_file_count,
                                 pending_email_period=False)
                session.history.append({"role": "user", "content": text})
                session.history.append({"role": "assistant",
                                        "content": "[busquei as notas no e-mail]"})
                session.history = session.history[-self.history_turns * 2:]
                resp = BotResponse()
                self.handle_fetch_email(session, resp, period=p)
                return resp
            # resposta sem data/mês: MANTÉM a pendência e repergunta o mês,
            # em vez de cair no fluxo normal (que pede o extrato)
            resp = BotResponse()
            resp.texts.append(
                "📧 Não entendi o mês. Me diz o período das notas, por favor:\n"
                "Ex.: 'agosto', '2026-08' ou '15/08/2026'.")
            return resp

        # cumprimento ("oi", "bom dia", ...) -> responde com calor + passo a passo
        if is_greeting(text):
            session.history.append({"role": "user", "content": text})
            session.history.append({"role": "assistant", "content": "[cumprimento]"})
            session.history = session.history[-self.history_turns * 2:]
            return BotResponse(texts=[self._greeting_reply(session)])

        # agradecimento ("obrigado", "valeu") -> responde com carinho
        if is_thanks(text):
            session.history.append({"role": "user", "content": text})
            session.history.append({"role": "assistant", "content": "[agradecimento]"})
            session.history = session.history[-self.history_turns * 2:]
            return BotResponse(texts=[
                "De nada! 😊 Posso ajudar com mais alguma coisa? Se quiser, me mande o "
                "extrato e as faturas ou pergunte sobre qualquer imposto."
            ])

        # continuação de caso de DIFAL: o bot pediu os dois estados, a
        # natureza da operação, o caso do destinatário (1/2/3) ou a alíquota
        # interna — o cliente responde sem repetir a pergunta -> junta a
        # pergunta original com a resposta e reanalisa o caso
        if session.pending_difal_text:
            t = normalize_name(text).strip()
            m = re.fullmatch(r"([123])\s*[).,–-]*", t)
            if m:
                # resposta numérica à lista de casos -> frase equivalente
                text = self._DIFAL_CASE_NUM[m.group(1)]
                t = normalize_name(text)
                print(f"[bot] DIFAL: resposta numérica {m.group(1)} -> {text!r}")
            is_follow, follow_case = self._difal_followup(t)
            if "difal" not in t and is_follow:
                if follow_case is not None and follow_case.origin \
                        and follow_case.destination:
                    # resposta traz os DOIS estados: a ordem do usuário
                    # (1ª menção = origem) vale na reanálise do caso
                    text = f"{text} {session.pending_difal_text}"
                    print("[bot] DIFAL: resposta junta à pergunta pendente (2 estados)")
                else:
                    # resposta traz 0-1 estados (ou só a natureza): o estado
                    # da pergunta original mantém o papel (posição) dele
                    text = f"{session.pending_difal_text} {text}"
                    print("[bot] DIFAL: resposta junta à pergunta pendente (1 estado/natureza)")
            else:
                print("[bot] DIFAL: pendente descartado (mensagem não completa o caso)")
            session.pending_difal_text = None

        # pergunta fiscal (LC 116, regimes, impostos de uma nota, DIFAL, extrato) -> responde direto
        if self.advisor.is_tax_question(text, session.audits):
            answer, target = self.advisor.answer_with_target(
                text, session.audits, session.transactions,
                last_discussed=session.last_discussed)
            if target is not None:
                # a nota sobre a qual respondi (citada por número) vira a última
                # consultada. Follow-ups SEM número não apontam para ela: o bot
                # responde objetivamente e não supõe/adivinha a nota.
                session.last_discussed = target
            session.pending_difal_text = self._pending_difal(text)
            if session.pending_difal_text:
                print("[bot] DIFAL: caso incompleto — aguardando estados, "
                      "caso do destinatário ou alíquota interna")
            session.history.append({"role": "user", "content": text})
            session.history.append({"role": "assistant", "content": answer[:200]})
            session.history = session.history[-self.history_turns * 2:]
            return BotResponse(texts=[answer])

        # usuário pede a conciliação de um mês/data diretamente (ex.: "agosto",
        # "relatório de julho"): a conciliação correta daquele período pede o
        # extrato do período — peço de novo mesmo se já tiver pedido antes
        # (sem "não vou reenviar o relatório"). "buscar no email de julho"
        # NÃO é isso: é busca no e-mail (o cérebro decide abaixo)
        if period is not None and self._is_period_request(text) \
                and not TemplateBrain.is_email_fetch(text):
            resp = BotResponse()
            resp.texts.append(
                f"📅 {period_label(period)} — para a conciliação correta, me manda o "
                "extrato bancário desse período (PDF, CSV, Excel ou OFX)? Assim que "
                "ele chegar, eu concilio as faturas com ele e envio o relatório.")
            return resp

        decision = self.brain.decide(session, text)
        if decision.period and not session.period:
            session.period = decision.period

        session.history.append({"role": "user", "content": text})
        marker = {
            "run": "[rodei a conciliação]",
            "list_files": "[liste os arquivos]",
            "reset": "[recomeçamos do zero]",
            "send_report": "[enviei o relatório]",
            "fetch_email": "[busquei as notas no e-mail]",
        }.get(decision.action, decision.reply)
        session.history.append({"role": "assistant", "content": marker or "…"})
        session.history = session.history[-self.history_turns * 2:]

        resp = BotResponse()
        if decision.action == "reset":
            self.reset(session)
            resp.texts.append("🧹 Recomeçamos do zero. Envie o extrato e as faturas.")
        elif decision.action == "list_files":
            resp.texts.append(self._list_files(session))
        elif decision.action == "fetch_email":
            # período vem SÓ da mensagem atual ("...de julho", "...de 13/08/2026"):
            # sem mês aqui, o bot pergunta — o período de mensagens anteriores
            # não vale para a busca no e-mail
            self.handle_fetch_email(session, resp,
                                    period=parse_day_period(text) or period)
        elif decision.action == "send_report":
            if session.last_report is not None:
                resp.texts.append("📄 Segue o resumo da última conciliação:")
                resp.report = session.last_report  # _send_response envia resumo + anexos
                if session.last_report_paths:
                    resp.csv_path, resp.md_path = session.last_report_paths
            elif session.has_extrato and session.has_faturas:
                resp.texts.append("⏳ Rodando a conciliação para gerar o relatório…")
                self._run(session, resp)
            else:
                missing = []
                if not session.has_extrato:
                    missing.append("extrato bancário")
                if not session.has_faturas:
                    missing.append("faturas")
                resp.texts.append(f"Para gerar o relatório, preciso de: {', '.join(missing)}. "
                                  "Pode enviar os arquivos?")
        elif decision.action == "run":
            if session.has_extrato and session.has_faturas:
                # "rodar" sempre recalcula e reenvia o relatório: se o cliente
                # pediu de novo, ele quer o arquivo de novo (mesmo sem arquivos novos)
                resp.texts.append("⏳ Rodando a conciliação… pode levar alguns segundos.")
                self._run(session, resp)
            else:
                resp.texts.append("Para rodar, preciso de um extrato e de faturas. "
                                  "Pode enviar os arquivos?")
        else:  # ask
            if decision.reply:
                resp.texts.append(decision.reply)
        return resp

    # -- conciliação ------------------------------------------------------- #
    def _run(self, session: ChatSession, resp: BotResponse):
        results = parse_directory(session.workdir)
        txns_all: list = []
        invs_all: list = []
        for r in results:
            txns_all.extend(r.transactions)
            invs_all.extend(r.invoices)
        # faturas extraídas de fotos (não estão em arquivo reconhecível)
        invs_all.extend(session.invoices_from_photos)
        # rede de segurança: ignora faturas duplicadas (nº + valor + emitente)
        invs_all = self._dedup(invs_all, lambda i: (i.number, str(i.amount),
                                                    (i.issuer or "").lower()))

        txns, invs = txns_all, invs_all
        # arquivos que NÃO são extrato bancário de verdade:
        #  (a) "combinados" — transações E faturas no mesmo arquivo (ex.: reprint
        #      de fatura com o pagamento embutido); as "transações" deles são a
        #      data/valor do pagamento da própria fatura, não do extrato;
        #  (b) arquivos com 1-2 "transações" — um extrato bancário tem várias
        #      linhas; 1-2 linhas com mesmo valor/data é o pagamento da fatura.
        # As faturas desses arquivos podem não ter vencimento — mesmo assim são
        # as faturas do período que o cliente enviou.
        combined = {f["name"] for f in session.files
                    if f["n_txns"] > 0 and f["n_invoices"] > 0}
        non_extrato = combined | {f["name"] for f in session.files
                                  if 0 < f["n_txns"] <= 2 and f["n_invoices"] == 0}
        if session.period:
            # período pode ser mês (2026-08) ou dia (2026-08-15)
            y, mo = (int(x) for x in session.period.split("-")[:2])

            def _in_month(d):
                return d is not None and d.year == y and d.month == mo

            f_txns = [t for t in txns_all
                      if t.date and t.date.year == y and t.date.month == mo
                      and t.source_file not in non_extrato]
            # NFS-e costuma vir sem vencimento: cai na emissão (issue_date);
            # sem data nenhuma, só conta se o arquivo é "combinado" (fatura +
            # pagamento no mesmo PDF, ex.: reprint de operadora)
            f_invs = [i for i in invs_all
                      if _in_month(i.due_date)
                      or (i.due_date is None
                          and (_in_month(i.issue_date) or i.source_file in combined))]
            if f_txns or f_invs:
                txns, invs = f_txns, f_invs
            else:
                dates = [t.date for t in txns_all if t.date] + \
                        [i.due_date for i in invs_all if i.due_date]
                if dates:
                    resp.texts.append(
                        f"⚠️ Não achei dados de {session.period}. Vou conciliar todos os "
                        f"dados encontrados ({min(dates)} a {max(dates)})."
                    )
                else:
                    resp.texts.append("⚠️ Não encontrei dados nos arquivos. Confira as datas.")
                    return

        # conciliação de um lado só não serve: se o período tem transações do
        # extrato mas nenhuma fatura (ou o contrário), peço o arquivo que falta
        # em vez de rodar um relatório cheio de "missing_invoice"
        if session.period:
            if txns and not invs:
                resp.texts.append(
                    f"📅 {period_label(session.period)} — achei as transações do extrato, "
                    "mas nenhuma fatura desse período. Me manda as faturas/notas do mês "
                    "(PDF, CSV ou Excel) que eu concilio e envio o relatório.")
                return
            if invs and not txns:
                # cálculo dos impostos das notas ANTES de pedir o extrato, igual
                # ao envio manual: casa cada fatura do período com o parecer
                # fiscal dela e mostra o relatório completo (base × alíquota =
                # valor, regime, retenções). O parecer é selecionado PELO
                # ARQUIVO da fatura (source_file): o nº do Invoice e o nº do
                # parecer nem sempre batem (NFC-e trunca o número), e casar por
                # número deixava vazio — daí a antiga sobra de "mostra tudo
                # que tem parecer", que mandava notas de MESES ANTIGOS que não
                # tinham nada a ver com o período.
                inv_sources = {i.source_file for i in invs}
                seen = set()
                for a in session.audits:
                    if a.source not in inv_sources:
                        continue
                    k = (a.number, str(a.amount), (a.issuer or "").lower())
                    if k in seen:
                        continue
                    seen.add(k)
                    resp.texts.append(self.advisor.full_report(a))
                resp.texts.append(
                    f"📅 {period_label(session.period)} — achei as faturas, mas nenhuma "
                    "transação do extrato nesse período. Me manda o extrato bancário do "
                    "mês (PDF, CSV, Excel ou OFX) que eu concilio e envio o relatório.")
                return

        if not txns and not invs:
            resp.texts.append("⚠️ Não encontrei dados nos arquivos. Confira o formato.")
            return

        reconciler = Reconciler(self.pipeline_llm, self._matcher, **self._recon_kwargs)
        report = reconciler.reconcile(txns, invs)
        session.last_report = report  # p/ responder "o que significa o alerta?" depois
        reports_dir = session.workdir / "reports"
        # só a auditoria das faturas DESTA conciliação: session.audits guarda
        # todos os pareceres já vistos na conversa (meses anteriores) e mandar
        # todos eles junto do relatório enche o chat de notas antigas
        inv_sources = {i.source_file for i in invs}
        audits = self._dedup([a for a in session.audits if a.source in inv_sources],
                             lambda a: (a.number, str(a.amount), (a.issuer or "").lower()))
        csv_path, md_path = write_report(report, reports_dir, audits=audits)
        session.last_report_paths = (csv_path, md_path)
        session.executed_file_count = session.file_count
        self._save_state(session.workdir, session.executed_file_count,
                         pending_email_period=session.pending_email_period)
        resp.report = report
        resp.audits = audits
        resp.csv_path = csv_path
        resp.md_path = md_path

    def _run_period(self, session: ChatSession, resp: BotResponse, extrato_file: str):
        """Concilia um EXTRATO novo apenas com as faturas do período dele
        (vencimento dentro do range das transações, c/ margem) + faturas
        pendentes aguardando extrato. Evita misturar períodos e gerar
        alertas 'aleatórios' de faturas de outros meses."""
        results = parse_directory(session.workdir)
        txns: list = []
        invs_all: list = []
        for r in results:
            if Path(r.path).name == extrato_file:
                txns.extend(r.transactions)
            invs_all.extend(r.invoices)
        invs_all.extend(session.invoices_from_photos)
        invs_all = self._dedup(invs_all, lambda i: (i.number, str(i.amount),
                                                    (i.issuer or "").lower()))
        if not txns:
            resp.texts.append("⚠️ Não encontrei transações neste extrato. Confira o arquivo "
                              "e envie de novo.")
            return
        dates = [t.date for t in txns if t.date]
        if not dates:
            resp.texts.append("⚠️ As transações deste extrato não têm data legível — não "
                              "consigo definir o período. Confira o arquivo.")
            return
        dmin, dmax = min(dates), max(dates)
        # margem curta (7d) p/ pagamento antecipado/atrasado perto da borda do
        # extrato — margem maior puxaria faturas do período anterior e geraria
        # alertas falsos de "sem pagamento"
        window = timedelta(days=7)
        pending = session.pending_invoice_ids
        invs = [i for i in invs_all
                if i.id in pending
                or (i.due_date is not None and dmin - window <= i.due_date <= dmax + window)]
        if not invs:
            resp.texts.append(
                f"⚠️ Este extrato cobre {dmin} a {dmax}, mas não achei faturas com "
                "vencimento nesse período. Se a fatura que você quer conciliar não tem "
                "vencimento legível, me avisa que eu incluo manualmente.")
            return
        nums = [i.number for i in invs if i.number][:8]
        resp.texts.append(
            f"⏳ Conciliando o extrato ({dmin} a {dmax}) com {len(invs)} fatura(s) do "
            f"período: {', '.join(nums)}{'…' if len(invs) > 8 else ''}.")
        reconciler = Reconciler(self.pipeline_llm, self._matcher, **self._recon_kwargs)
        report = reconciler.reconcile(txns, invs)
        reports_dir = session.workdir / "reports"
        audits = self._dedup(list(session.audits),
                             lambda a: (a.number, str(a.amount), (a.issuer or "").lower()))
        inv_nums = {i.number for i in invs if i.number}
        inv_sources = {i.source_file for i in invs}
        # por ARQUIVO (o nº do parecer nem sempre bate com o nº do Invoice)
        period_audits = [a for a in audits
                         if a.source in inv_sources or a.number in inv_nums]
        csv_path, md_path = write_report(report, reports_dir, audits=period_audits)
        session.last_report = report
        session.last_report_paths = (csv_path, md_path)
        session.executed_file_count = session.file_count
        self._save_state(session.workdir, session.executed_file_count,
                         pending_email_period=session.pending_email_period)
        session.pending_invoice_ids.clear()
        resp.report = report
        resp.audits = period_audits
        resp.csv_path = csv_path
        resp.md_path = md_path

    @staticmethod
    def _dedup(items: list, key_fn) -> list:
        """Mantém apenas a primeira ocorrência de cada chave (ordem preservada)."""
        seen: set = set()
        out: list = []
        for it in items:
            k = key_fn(it)
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out

    # -- utilidades -------------------------------------------------------- #
    def _list_files(self, session: ChatSession) -> str:
        if not session.files:
            return "📂 Ainda não recebi arquivos. Envie o extrato e as faturas."
        lines = ["📂 Arquivos desta conversa:"]
        for f in session.files:
            lines.append(f"• {f['name']} — {f['n_txns']} transações / {f['n_invoices']} faturas")
        if session.period:
            lines.append(f"🗓️ Período: {session.period}")
        return "\n".join(lines)

    def reset(self, session: ChatSession):
        for f in session.workdir.iterdir():
            if f.is_file():
                f.unlink()
        reports = session.workdir / "reports"
        if reports.exists():
            shutil.rmtree(reports, ignore_errors=True)
        session.files.clear()
        session.invoices_from_photos.clear()
        session.audits.clear()
        session.transactions.clear()
        session.period = None
        session.history.clear()
        session.executed_file_count = 0
        session.last_report = None
        session.last_report_paths = None
        session.pending_invoice_ids.clear()
        session.last_discussed = None
        session.pending_difal_text = None
        session.pending_email_period = False


# --------------------------------------------------------------------------- #
# Loop principal
# --------------------------------------------------------------------------- #
def _dispatch(agent: ConciliationAgent, api: TelegramAPI,
              session: ChatSession, msg: dict) -> BotResponse:
    base = BotResponse()
    if not session.welcomed:
        # se a 1ª mensagem já é um cumprimento, handle_text envia o welcome
        # caloroso; se já é "voltar ao início", handle_text envia o welcome
        # (sem duplicar)
        txt = msg.get("text", "")
        first_is_greeting = "text" in msg and (is_greeting(txt) or is_home(txt))
        if not first_is_greeting:
            base.texts.append(WELCOME)
        session.welcomed = True

    if "document" in msg:
        doc = msg["document"]
        data = api.download_file(doc["file_id"])
        r = agent.handle_document(session, doc.get("file_name", "arquivo"), data,
                                  caption=msg.get("caption"))
    elif "photo" in msg:
        data = api.download_file(msg["photo"][-1]["file_id"])
        r = agent.handle_photo(session, "foto.jpg", data,
                               caption=msg.get("caption"))
    elif "text" in msg:
        r = agent.handle_text(session, msg["text"])
    else:
        r = BotResponse(texts=["Entendo texto, arquivos (PDF, CSV, Excel, OFX) e "
                               "FOTOS de faturas. Pode enviar o extrato e as faturas?"])
    base.texts.extend(r.texts)
    base.report = r.report
    base.audits = r.audits
    base.csv_path = r.csv_path
    base.md_path = r.md_path
    return base


def _send_response(api: TelegramAPI, chat_id, resp: BotResponse):
    """Envia a resposta item a item. Um item que falha NÃO derruba os outros
    (e o erro fica no log — sem isso um '400/403' silenciava a conversa toda)."""
    if not resp.texts and resp.report is None:
        print(f"   ⚠️ resposta VAZIA para chat={chat_id} (nada a enviar)")
    for t in resp.texts:
        try:
            api.send_text(chat_id, t)
        except Exception as e:  # noqa: BLE001
            print(f"   ❌ falha ao enviar texto ({type(e).__name__}): {e}")
    if resp.report is not None:
        try:
            api.send_text(chat_id, TelegramNotifier.build_summary(resp.report),
                          parse_mode="Markdown")
            if resp.audits:
                api.send_text(chat_id, _audits_summary(resp.audits))
            if resp.csv_path:
                api.send_document(chat_id, resp.csv_path, "📄 Relatório detalhado (CSV)")
            if resp.md_path:
                api.send_document(chat_id, resp.md_path, "📝 Relatório legível (Markdown)")
        except Exception as e:  # noqa: BLE001
            print(f"   ❌ falha ao enviar relatório ({type(e).__name__}): {e}")


def _audits_summary(audits: list[InvoiceAudit]) -> str:
    """Resumo da auditoria fiscal de todas as faturas analisadas."""
    lines = ["🧾 AUDITORIA FISCAL DAS FATURAS"]
    for a in audits:
        probs = a.problems
        head = (f"• Fatura {a.number} ({a.issuer}) — {_brl(a.amount)}"
                if a.amount is not None else f"• Fatura {a.number} ({a.issuer})")
        if not a.tax_lines:
            lines.append(head + " — sem detalhes de impostos.")
            continue
        lines.append(head + ":")
        for tl_line in format_tax_lines(a):
            lines.append("    " + tl_line)
        for c in probs:
            emoji = {"high": "🔴", "medium": "🟠", "low": "🟡"}[c.severity.value]
            lines.append(f"    {emoji} {c.message}")
    return "\n".join(lines)


def _typing_keepalive(api: TelegramAPI, chat_id, stop: threading.Event):
    """Mantém o 'digitando…' visível no Telegram enquanto o bot processa
    (a resposta do LLM pode levar alguns minutos; sem o indicador o chat
    parece travado)."""
    while not stop.wait(4):
        api.send_chat_action(chat_id)


def run_bot(cfg: dict):
    token = cfg["telegram"]["token"]
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN não definido. Exporte a variável e rode de novo.")
        return

    api = TelegramAPI(token)
    try:
        me = api.get_me()
    except Exception as e:  # noqa: BLE001
        print(f"❌ Falha ao falar com o Telegram: {e}")
        return
    print(f"🤖 Bot @{me.get('username', '?')} online.")

    # cérebro da conversa
    mode = cfg["bot"].get("mode", "auto")
    ollama = OllamaClient(
        base_url=cfg["llm"]["base_url"],
        model=cfg["llm"]["model"],
        timeout=float(cfg["llm"]["timeout"]),
        num_ctx=int(cfg["llm"].get("num_ctx", 2048)),
    )
    use_llm = (mode == "llm") or (mode == "auto" and ollama.available)
    if use_llm:
        history_turns = int(cfg["bot"].get("history_turns", 3))
        if mode == "llm":
            brain = LLMBrain(ollama, history_turns=history_turns)
            print(f"🧠 Cérebro: LLM (Ollama · {cfg['llm']['model']})")
        else:
            brain = HybridBrain(ollama, history_turns=history_turns)
            print(f"🧠 Cérebro: híbrido (comandos rápidos + LLM p/ perguntas · "
                  f"Ollama · {cfg['llm']['model']})")
    else:
        brain = TemplateBrain()
        print("🧠 Cérebro: template (sem LLM)")

    # pipeline de conciliação (determinístico por padrão = rápido)
    pipeline_llm = ollama if (use_llm and cfg["bot"].get("pipeline_llm", False)) else FallbackClient()
    if isinstance(pipeline_llm, FallbackClient):
        print("📐 Conciliação: determinística (rápida)")
    else:
        print("📐 Conciliação: com LLM (mais lenta)")

    base_dir = Path(cfg["bot"].get("sessions_dir", "data/sessions"))
    base_dir.mkdir(parents=True, exist_ok=True)
    extract_llm = ollama if cfg["bot"].get("extract_with_llm", False) else None
    # advisor usa LLM (fallback p/ dúvidas fiscais fora das regras) só quando o
    # modo do cérebro usa LLM — em modo template, só regras determinísticas
    # (evita resposta de 5-20 min pendurada no Ollama)
    advisor = TaxAdvisor(llm=ollama if (use_llm and ollama.available) else None)
    if advisor.llm is not None:
        print(f"📚 Assistente fiscal: com LLM de fallback (Ollama · {cfg['llm']['model']})")
    else:
        print("📚 Assistente fiscal: somente regras determinísticas")
    agent = ConciliationAgent(cfg, brain, pipeline_llm, base_dir,
                              extract_llm=extract_llm, advisor=advisor, api=api)

    allowed = set(cfg["bot"].get("allowed_chat_ids") or [])
    offset = 0
    print("👂 Aguardando mensagens… (Ctrl+C para sair)")
    try:
        while True:
            try:
                updates = api.get_updates(offset, timeout=30)
            except Exception as e:  # noqa: BLE001
                print(f"[bot] getUpdates: {e}; tentando de novo em 5s")
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id is None:
                    continue
                if allowed and chat_id not in allowed:
                    try:
                        api.send_text(chat_id, "⛔ Você não está autorizado a usar este bot.")
                    except Exception:  # noqa: BLE001 - chat bloqueado não pode derrubar o loop
                        pass
                    continue
                session = None
                try:
                    session = agent.get_session(chat_id)
                except Exception:  # noqa: BLE001 - sessão corrompida não mata o loop
                    print(f"[bot] erro ao carregar a sessão do chat {chat_id}:")
                    traceback.print_exc()
                    continue
                print(f"📩 chat={chat_id} tipo={next((k for k in ('document', 'photo', 'text') if k in msg), '?')}")
                if "text" in msg:
                    print(f"   📝 {msg['text'][:200]!r}")
                stop_typing = threading.Event()
                threading.Thread(target=_typing_keepalive, args=(api, chat_id, stop_typing),
                                 daemon=True).start()
                try:
                    resp = _dispatch(agent, api, session, msg)
                    _send_response(api, chat_id, resp)
                    for t in resp.texts:
                        print(f"   💬 {t.splitlines()[0][:200]!r}")
                except Exception as e:  # noqa: BLE001
                    print(f"[bot] erro ao processar: {e}")
                    traceback.print_exc()
                    try:
                        api.send_text(chat_id, "⚠️ Ocorreu um erro ao processar. Tente de novo.")
                    except Exception:  # noqa: BLE001 - chat bloqueado não pode derrubar o loop
                        pass
                finally:
                    stop_typing.set()
    except KeyboardInterrupt:
        print("\n👋 Bot encerrado.")
    except Exception:  # noqa: BLE001 - o loop NUNCA pode morrer em silêncio
        print("[bot] erro fatal no loop principal:")
        traceback.print_exc()
