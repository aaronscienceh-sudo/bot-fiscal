"""Cliente para LLM local via Ollama (http://localhost:11434).

Usa a API /api/chat com saída JSON para respostas estruturadas.
Se o Ollama não estiver disponível, available=False e o orquestrador
usa o fallback determinístico.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from ..domain.models import Inconsistency, Invoice, Transaction
from .base import CATEGORIES


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434",
                 model: str = "llama3.2:1b-instruct-q4_K_M", timeout: float = 120.0,
                 num_ctx: int = 2048):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = int(num_ctx)
        self.available = False
        self._client = httpx.Client(timeout=timeout)
        self._check()

    def _check(self) -> None:
        try:
            r = self._client.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            self.available = True
        except Exception:  # noqa: BLE001
            self.available = False

    def _chat(self, system: str, user: str, *, max_tokens: int = 512) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "keep_alive": 1800,  # mantém o modelo carregado 30min (evita reload de ~20s)
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": max_tokens,
                "temperature": 0.0,
                "reasoning_effort": "off",
            },
        }
        r = self._client.post(f"{self.base_url}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]["content"]

    @staticmethod
    def _loads(s: str) -> dict:
        s = s.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.startswith("json"):
                s = s[4:]
        return json.loads(s)

    # ------------------------------------------------------------------ #
    def chat(self, system: str, messages: list[dict[str, str]]) -> str:
        """Conversa multi-turno. messages: [{'role':'user'|'assistant','content':...}]"""
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [{"role": "system", "content": system}, *messages],
            "keep_alive": 1800,  # mantém o modelo carregado 30min (evita reload de ~20s)
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": 512,
                "temperature": 0.2,
                "reasoning_effort": "off",
            },
        }
        r = self._client.post(f"{self.base_url}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()["message"]["content"]

    def chat_json(self, system: str, messages: list[dict[str, str]]) -> dict:
        return self._loads(self.chat(system, messages))

    # ------------------------------------------------------------------ #
    def classify_invoice(self, invoice: Invoice) -> str:
        if not self.available:
            return "Outros"
        system = (
            "Você classifica faturas empresariais. Responda SOMENTE JSON: "
            '{"category": "<categoria>"} usando uma destas: '
            + ", ".join(CATEGORIES)
        )
        user = (
            f"Emissor: {invoice.issuer}\n"
            f"Descrição: {invoice.description or 'N/D'}\n"
            f"Valor: {invoice.amount}"
        )
        try:
            data = self._loads(self._chat(system, user))
            cat = str(data.get("category", "")).strip()
            # normaliza para a lista canônica
            for c in CATEGORIES:
                if c.lower() in cat.lower() or cat.lower() in c.lower():
                    return c
            return cat or "Outros"
        except Exception:  # noqa: BLE001
            return "Outros"

    # ------------------------------------------------------------------ #
    def propose_match(self, txn: Transaction, candidates: list[Invoice]) -> tuple[Invoice | None, float, str]:
        if not self.available or not candidates:
            return None, 0.0, ""
        system = (
            "Você concilia pagamentos de extrato bancário com faturas. "
            "Escolha a fatura que melhor corresponde ao pagamento. "
            "Responda SOMENTE JSON: {\"invoice_id\": \"<id>\" ou null, "
            "\"confidence\": <0..1>, \"reason\": \"<curto>\"}."
        )
        cand_lines = "\n".join(
            f"- id={c.id} número={c.number} emissor={c.issuer} valor={c.amount} venc={c.due_date}"
            for c in candidates
        )
        user = (
            f"PAGAMENTO: data={txn.date} valor={txn.amount} "
            f"descrição='{txn.description}' referência={txn.reference or 'N/D'} "
            f"contraparte={txn.counterparty or 'N/D'}\n\n"
            f"CANDIDATAS:\n{cand_lines}"
        )
        try:
            data = self._loads(self._chat(system, user))
            inv_id = data.get("invoice_id")
            conf = float(data.get("confidence", 0.0))
            reason = str(data.get("reason", ""))
            if not inv_id:
                return None, conf, reason
            inv = next((c for c in candidates if c.id == inv_id), None)
            return inv, max(0.0, min(1.0, conf)), reason
        except Exception:  # noqa: BLE001
            return None, 0.0, ""

    # ------------------------------------------------------------------ #
    def explain(self, inc: Inconsistency) -> str:
        if not self.available:
            return inc.message
        system = ("Explique a inconsistência financeira em 1 frase clara para um contador. "
                  "Responda SOMENTE JSON: {\"explanation\": \"<frase>\"}.")
        user = (
            f"Tipo: {inc.type.value}\nSeveridade: {inc.severity.value}\n"
            f"Mensagem: {inc.message}\nDetalhe: {inc.detail or 'N/D'}"
        )
        try:
            data = self._loads(self._chat(system, user))
            return str(data.get("explanation", inc.message))
        except Exception:  # noqa: BLE001
            return inc.message

    # ------------------------------------------------------------------ #
    EXTRACT_SYSTEM = (
        "Você é um assistente contábil brasileiro. Receberá o texto extraído por OCR "
        "de uma fatura ou nota fiscal (NFe/NFS-e/NFCe/fatura de serviço). Extraia os dados.\n"
        "Responda SOMENTE JSON válido, sem texto fora, neste formato exato:\n"
        '{"issuer": "<emissor/razão social>" ou null, '
        '"number": "<número da fatura/nota>" ou null, '
        '"issue_date": "AAAA-MM-DD" ou null, '
        '"due_date": "AAAA-MM-DD" ou null, '
        '"amount": <total a pagar, número> ou null, '
        '"subtotal": <subtotal antes dos impostos, número> ou null, '
        '"nf_type": "NFe" | "NFS-e" | "NFCe" | "Fatura" ou null, '
        '"tax_lines": [{"name": "<imposto>", "base": <número ou null>, '
        '"rate": <alíquota como fração, ex. 0.18>, "amount": <número ou null>}], '
        '"city": "<município da incidência do ISSQN, ex. São Paulo>" ou null, '
        '"service_code": "<código do serviço LC 116/2003, ex. 17.20>" ou null, '
        '"service_value": <valor do serviço bruto, NFS-e> ou null, '
        '"retentions": <TOTAL de retenções na fonte, NFS-e> ou null, '
        '"net_value": <valor líquido, NFS-e> ou null, '
        '"freight": <frete, NFe> ou null, '
        '"st": <valor do ICMS-ST, NFe> ou null, '
        '"discounts": <descontos> ou null, '
        '"notes": "<observação curta>" ou null}\n\n'
        "Regras:\n"
        "- rate SEMPRE como fração decimal (18% -> 0.18; 5% -> 0.05).\n"
        "- nf_type: NFe = mercadorias/transporte (ICMS); NFS-e = serviços (ISS); NFCe = varejo; 'Fatura' = fatura comum.\n"
        "- city e service_code: preencha somente quando houver ISSQN (NFS-e); null caso contrário.\n"
        "- tax_lines: inclua ICMS, IPI, PIS, COFINS, ISS, IR, CSLL, ICMS-ST etc. que aparecerem.\n"
        "- RETENÇÕES: se a nota mostrar retenções na fonte (ex.: 'IR retido', 'PIS retido', "
        "'COFINS retido', 'CSLL retido', 'ISS retido'), inclua cada uma em tax_lines com o nome "
        "terminando em RETIDO (ex.: \"IR RETIDO\") e o valor retido; e some tudo em 'retentions'.\n"
        "- service_value/retentions/net_value: só NFS-e. freight/st/discounts: só NFe.\n"
        "- notes: inclua o regime do emitente se aparecer ('Simples Nacional', 'Lucro Presumido', "
        "'Lucro Real', 'MEI') e outras observações fiscais relevantes.\n"
        "- Use null quando o campo não estiver no texto. NÃO invente valores.\n"
        "- Texto de OCR pode ter ruído/erro de leitura; use bom senso para interpretar números."
    )

    def extract_invoice_from_text(self, text: str) -> dict | None:
        """Extrai dados estruturados de uma fatura a partir do texto (OCR)."""
        if not self.available or not (text or "").strip():
            return None
        try:
            data = self._loads(self._chat(self.EXTRACT_SYSTEM, text[:6000]))
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            return None
