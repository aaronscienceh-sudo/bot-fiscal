"""Notificações via Telegram.

Envia um resumo das inconsistências no chat e anexa os arquivos de
relatório (CSV + Markdown). Usa a Bot API com httpx.
"""
from __future__ import annotations

from pathlib import Path

import httpx

from ..domain.models import ReconciliationReport, Severity

_SEV_EMOJI = {Severity.HIGH: "🔴", Severity.MEDIUM: "🟠", Severity.LOW: "🟡"}


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, enabled: bool = True,
                 timeout: float = 30.0):
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token) and bool(chat_id)
        self.timeout = timeout
        self._base = f"https://api.telegram.org/bot{token}"

    # ------------------------------------------------------------------ #
    def _post(self, method: str, **kwargs):
        if not self.enabled:
            return None
        url = f"{self._base}/{method}"
        try:
            if "files" in kwargs:
                r = httpx.post(url, data=kwargs.get("data", {}), files=kwargs["files"],
                               timeout=self.timeout)
            else:
                r = httpx.post(url, json=kwargs.get("json", {}), timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            print(f"[telegram] falha em {method}: {e}")
            return None

    def send_text(self, text: str):
        return self._post("sendMessage", json={"chat_id": self.chat_id, "text": text})

    def send_document(self, path: str | Path, caption: str = ""):
        path = Path(path)
        if not path.exists():
            return None
        with path.open("rb") as f:
            return self._post("sendDocument", data={"chat_id": self.chat_id, "caption": caption},
                              files={"document": (path.name, f)})

    # ------------------------------------------------------------------ #
    @staticmethod
    def build_summary(report: ReconciliationReport, max_items: int = 15) -> str:
        sev = report.by_severity
        lines = [
            "📊 *Conciliação de Contas e Faturas*",
            "",
            f"Transações: {report.total_transactions}   Faturas: {report.total_invoices}",
            f"Conciliadas: {len(report.matched)}",
            f"Inconsistências: {len(report.inconsistencies)} "
            f"(🔴 {sev['high']} · 🟠 {sev['medium']} · 🟡 {sev['low']})",
        ]
        if report.inconsistencies:
            lines.append("")
            lines.append("_Principais alertas:_")
            for inc in report.inconsistencies[:max_items]:
                emoji = _SEV_EMOJI.get(inc.severity, "•")
                lines.append(f"{emoji} {inc.message}")
            if len(report.inconsistencies) > max_items:
                lines.append(f"… e mais {len(report.inconsistencies) - max_items}.")
        else:
            lines.append("")
            lines.append("✅ Nenhuma inconsistência detectada.")
        lines.append("")
        lines.append("_Relatórios completos anexados._")
        return "\n".join(lines)

    def send_report(self, report: ReconciliationReport,
                    csv_path: str | Path, md_path: str | Path):
        """Envia resumo + anexa CSV e Markdown."""
        if not self.enabled:
            return
        self.send_text(self.build_summary(report))
        self.send_document(csv_path, caption="📄 Relatório detalhado (CSV)")
        self.send_document(md_path, caption="📝 Relatório legível (Markdown)")
