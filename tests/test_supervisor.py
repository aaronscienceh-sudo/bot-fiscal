"""Testes do supervisor (auditoria do bot).

Cobre: verificador de logs (token vazado, 409, tracebacks, rede, etc.),
qualidade de parsing das sessões, recálculo de impostos (drift), runner
(descoberta de logs, filtro --only) e escrita dos relatórios MD/CSV.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.models import TaxLine  # noqa: E402
from src.domain.tax import InvoiceAudit, verify_invoice_taxes  # noqa: E402
from src.parsing import ParsedFile  # noqa: E402
from src.supervisor import (  # noqa: E402
    discover_log_paths,
    run_audit,
    write_audit_csv,
    write_audit_markdown,
    write_audit_report,
)
from src.supervisor.checks import (  # noqa: E402
    kb_consistency,
    log_health,
    parsing_quality,
    replay,
    session_sanity,
    tax_recalc,
)
from src.supervisor.models import CheckContext, Severity  # noqa: E402
from src.supervisor.session_files import SessionFile  # noqa: E402

TOKEN = "123456789:AA" + "B" * 33  # padrão de token do Telegram (35 chars após os dois-pontos)


def _ctx(tmp_path: Path, content: str, name: str = "bot.log") -> CheckContext:
    log = tmp_path / name
    log.write_text(content, encoding="utf-8")
    return CheckContext(log_paths=[log])


def _run(ctx: CheckContext):
    findings, stats = log_health.run(ctx)
    return {f.id: f for f in findings}, stats


# --------------------------------------------------------------------------- #
# log_health: achados críticos e de alto impacto
# --------------------------------------------------------------------------- #
def test_token_leak_e_critical(tmp_path):
    ctx = _ctx(tmp_path, f"[bot] getUpdates: Client error '409 Conflict' "
                         f"for url 'https://api.telegram.org/bot{TOKEN}/getUpdates'\n")
    found, stats = _run(ctx)
    assert stats["token_leaks"] == 1
    assert found["log_health.token_leak"].severity == Severity.CRITICAL
    assert found["log_health.duplicate_instance"].severity == Severity.HIGH


def test_log_limpo_com_token_redigido_nao_vaza(tmp_path):
    ctx = _ctx(tmp_path, "[bot] getUpdates: Client error '409 Conflict' "
                         "for url 'https://api.telegram.org/botXXXX/getUpdates'\n")
    found, stats = _run(ctx)
    assert stats["token_leaks"] == 0
    assert "log_health.token_leak" not in found


def test_409_duplica_instancia_high(tmp_path):
    lines = ["[bot] getUpdates: Client error '409 Conflict'; tentando de novo em 5s\n"] * 3
    found, stats = _run(_ctx(tmp_path, "".join(lines)))
    assert stats["getupdates_409"] == 3
    assert found["log_health.duplicate_instance"].severity == Severity.HIGH
    assert found["log_health.duplicate_instance"].count == 3


def test_traceback_high(tmp_path):
    content = ("[bot] erro ao processar: Client error '403 Forbidden'\n"
               "Traceback (most recent call last):\n"
               '  File "src/bot/telegram_bot.py", line 1841, in loop\n'
               "httpx.HTTPStatusError: 403\n")
    found, stats = _run(_ctx(tmp_path, content))
    assert stats["tracebacks"] == 1
    assert stats["blocked_chats_403"] == 1
    assert found["log_health.traceback"].severity == Severity.HIGH
    assert found["log_health.blocked_chat"].severity == Severity.MEDIUM


# --------------------------------------------------------------------------- #
# log_health: rede e estabilidade
# --------------------------------------------------------------------------- #
def test_timeouts_poucos_sao_low_muitos_sao_medium(tmp_path):
    few = "[bot] getUpdates: The read operation timed out; tentando de novo em 5s\n" * 10
    found, _ = _run(_ctx(tmp_path, few))
    assert found["log_health.timeout"].severity == Severity.LOW

    many = few * 10  # 100 timeouts
    found, _ = _run(_ctx(tmp_path, many))
    assert found["log_health.timeout"].severity == Severity.MEDIUM


def test_dns_failure_contado(tmp_path):
    content = ("[bot] getUpdates: [Errno -3] Temporary failure in name resolution; "
               "tentando de novo em 5s\n") * 3
    found, stats = _run(_ctx(tmp_path, content))
    assert stats["dns_failure"] == 3
    assert "log_health.dns_failure" in found


def test_rajada_longa_de_erros_medium(tmp_path):
    content = ("[bot] getUpdates: The read operation timed out; tentando de novo em 5s\n"
               "📩 chat=1 tipo=text\n") * 15  # rajadas de 1 (quebradas)
    found, stats = _run(_ctx(tmp_path, content))
    assert stats["max_getupdates_streak"] == 1
    assert "log_health.error_streak" not in found

    streak = ("[bot] getUpdates: The read operation timed out; tentando de novo em 5s\n" * 25
              + "📩 chat=1 tipo=text\n")
    found, stats = _run(_ctx(tmp_path, streak))
    assert stats["max_getupdates_streak"] == 25
    assert found["log_health.error_streak"].severity == Severity.MEDIUM


def test_instabilidade_telegram_low(tmp_path):
    content = ("[bot] getUpdates: Server error '502 Bad Gateway'; tentando de novo em 5s\n"
               "[bot] getUpdates: Server disconnected without sending a response; "
               "tentando de novo em 5s\n")
    found, stats = _run(_ctx(tmp_path, content))
    assert stats["server_502"] == 1
    assert stats["server_disconnect"] == 1
    assert found["log_health.telegram_instability"].severity == Severity.LOW


# --------------------------------------------------------------------------- #
# log_health: respostas e parsing
# --------------------------------------------------------------------------- #
def test_resposta_vazia_e_falha_envio_medium(tmp_path):
    content = ("   ⚠️ resposta VAZIA para chat=1 (nada a enviar)\n"
               "   ❌ falha ao enviar texto (HTTPStatusError): 400\n")
    found, stats = _run(_ctx(tmp_path, content))
    assert stats["empty_responses"] == 1
    assert stats["send_failures"] == 1
    assert found["log_health.empty_response"].severity == Severity.MEDIUM
    assert found["log_health.send_failure"].severity == Severity.MEDIUM


def test_parse_failure_low(tmp_path):
    content = "⚠️ Não consegui extrair dados dos anexos encontrados no e-mail.\n"
    found, stats = _run(_ctx(tmp_path, content))
    assert stats["parse_failures"] == 1
    assert found["log_health.parse_failure"].severity == Severity.LOW


def test_log_sem_timestamp_low(tmp_path):
    found, stats = _run(_ctx(tmp_path, "📩 chat=1 tipo=text\n💬 'olá'\n"))
    assert stats["lines"] == 2
    assert stats["timestamped_lines"] == 0
    assert found["log_health.no_timestamps"].severity == Severity.LOW


def test_log_com_timestamp_nao_reclama(tmp_path):
    content = ("2026-10-14 19:33:01 📩 chat=1 tipo=text\n"
               "2026-10-14 19:33:02 💬 'olá'\n")
    found, stats = _run(_ctx(tmp_path, content))
    assert stats["timestamped_lines"] == 2
    assert "log_health.no_timestamps" not in found


def test_log_limpo_nao_gera_achados_altos(tmp_path):
    content = ("🤖 Bot @Invoice_travisbot online.\n"
               "🧠 Cérebro: template (sem LLM)\n"
               "📩 chat=111111 tipo=text\n"
               "   📝 'opa'\n"
               "   💬 'Olá! Tudo bem? 😊'\n")
    found, _ = _run(_ctx(tmp_path, content))
    assert not any(f.severity in (Severity.CRITICAL, Severity.HIGH,
                                  Severity.MEDIUM) for f in found.values())


def test_sem_logs_gera_finding_low(tmp_path):
    ctx = CheckContext(log_paths=[tmp_path / "nao_existe.log"])
    findings, stats = log_health.run(ctx)
    assert findings[0].id == "log_health.no_logs"


def test_varios_logs_agregam_stats(tmp_path):
    a = tmp_path / "bot.log"
    b = tmp_path / "logs" / "bot_live.log"
    b.parent.mkdir()
    a.write_text("[bot] getUpdates: Client error '409 Conflict'; x\n", encoding="utf-8")
    b.write_text("[bot] getUpdates: Client error '409 Conflict'; x\n", encoding="utf-8")
    found, stats = _run(CheckContext(log_paths=[a, b]))
    assert stats["getupdates_409"] == 2
    assert found["log_health.duplicate_instance"].count == 2


# --------------------------------------------------------------------------- #
# runner: descoberta, filtro e relatórios
# --------------------------------------------------------------------------- #
def test_discover_log_paths(tmp_path):
    (tmp_path / "bot.log").write_text("x\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "bot_live.log").write_text("x\n", encoding="utf-8")
    (logs / "outro.txt").write_text("x\n", encoding="utf-8")
    paths = discover_log_paths(tmp_path)
    assert [p.name for p in paths] == ["bot.log", "bot_live.log"]


def test_run_audit_com_only(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("[bot] getUpdates: Client error '409 Conflict'; x\n", encoding="utf-8")
    report = run_audit(log_paths=[log], only=["log_health"])
    assert report.checks_run == ["log_health"]
    assert any(f.id == "log_health.duplicate_instance" for f in report.findings)


def test_run_audit_ordenado_por_severidade(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "⚠️ resposta VAZIA para chat=1 (nada a enviar)\n"
        "[bot] getUpdates: Client error '409 Conflict'; x\n"
        f"https://api.telegram.org/bot{TOKEN}/getUpdates\n",
        encoding="utf-8",
    )
    report = run_audit(log_paths=[log], only=["log_health"])
    assert report.findings[0].severity == Severity.CRITICAL
    sevs = [f.severity for f in report.findings]
    order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
    assert sevs == sorted(sevs, key=lambda s: order[s])


def test_write_audit_report(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("[bot] getUpdates: Client error '409 Conflict'; x\n", encoding="utf-8")
    report = run_audit(log_paths=[log], only=["log_health"])
    out = tmp_path / "auditoria"
    csv_path, md_path = write_audit_report(report, out, stamp="20261014_000000")
    assert csv_path.exists() and md_path.exists()
    assert csv_path.name == "auditoria_20261014_000000.csv"
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "duplicate_instance" in csv_text
    md_text = md_path.read_text(encoding="utf-8")
    assert "Relatório de Auditoria" in md_text
    assert "duplicate_instance" in md_text


def test_write_audit_report_sem_achados(tmp_path):
    from src.supervisor.models import AuditoriaReport
    report = AuditoriaReport(checks_run=["log_health"])
    csv_path, md_path = write_audit_report(report, tmp_path, stamp="x")
    assert csv_path.read_text(encoding="utf-8-sig").count("\n") == 1  # só cabeçalho
    assert "Nenhum problema encontrado" in md_path.read_text(encoding="utf-8")


def test_max_severity():
    report = run_audit(log_paths=[], only=["log_health"])
    clean = report.max_severity  # sem logs -> só no_logs (LOW)
    assert clean == Severity.LOW


# --------------------------------------------------------------------------- #
# parsing_quality
# --------------------------------------------------------------------------- #
def _make_session(tmp_path: Path, chat: str = "1", files: dict[str, str] | None = None):
    sdir = tmp_path / "sessions" / chat
    sdir.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {}).items():
        (sdir / name).write_text(content, encoding="utf-8")
    return tmp_path / "sessions"


def test_parsing_quality_sem_sessoes(tmp_path):
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "nao_existe")
    findings, stats = parsing_quality.run(ctx)
    assert findings[0].id == "parsing_quality.no_sessions"
    assert stats["files_total"] == 0


def test_parsing_quality_flags_arquivos_problematicos(tmp_path):
    sessions = _make_session(tmp_path, files={
        "extrato.csv": "data,valor,historico\n01/08/2026,-150,PIX\n",
        "vazio.csv": "data,valor,historico\n",
        "nota.txt": "isto nao e uma nota fiscal\n",
        "foto.bin": "xxx",
        ".estado_bot.json": "{}",
    })
    ctx = CheckContext(cfg={}, sessions_dir=sessions)
    findings, stats = parsing_quality.run(ctx)
    by_id = {f.id: f for f in findings}
    assert stats["sessions"] == 1
    assert stats["files_total"] == 4  # .estado_bot.json é ignorado
    assert stats["files_ok"] == 1
    assert stats["files_error"] == 2  # vazio.csv (sem linhas) + nota.txt
    assert stats["files_unsupported"] == 1
    assert stats["txns"] == 1
    assert by_id["parsing_quality.errors"].severity == Severity.MEDIUM
    assert "vazio.csv" in by_id["parsing_quality.errors"].evidence
    assert by_id["parsing_quality.unsupported"].severity == Severity.LOW


def test_parsing_quality_arquivo_sem_dados_e_low(tmp_path):
    sessions = tmp_path / "sessions"
    (sessions / "1").mkdir(parents=True)
    ctx = CheckContext(cfg={}, sessions_dir=sessions)
    parsed = ParsedFile(path="x.pdf")  # parse "ok", mas nada extraído
    ctx.sessions_cache = [SessionFile(chat_id="1", path=Path("x.pdf"),
                                      kind="doc", parsed=parsed)]
    findings, stats = parsing_quality.run(ctx)
    assert stats["files_empty"] == 1
    assert findings[0].id == "parsing_quality.empty"
    assert findings[0].severity == Severity.LOW


def test_parsing_quality_arquivo_ok_nao_gera_finding(tmp_path):
    sessions = _make_session(tmp_path, files={
        "extrato.csv": "data,valor,historico\n01/08/2026,-150,PIX\n",
    })
    ctx = CheckContext(cfg={}, sessions_dir=sessions)
    findings, stats = parsing_quality.run(ctx)
    assert findings == []
    assert stats["files_ok"] == 1


def test_parsing_quality_cacheia_sessoes(tmp_path):
    sessions = _make_session(tmp_path, files={
        "extrato.csv": "data,valor,historico\n01/08/2026,-150,PIX\n",
    })
    ctx = CheckContext(cfg={}, sessions_dir=sessions)
    parsing_quality.run(ctx)
    assert ctx.sessions_cache is not None
    assert len(ctx.sessions_cache) == 1


# --------------------------------------------------------------------------- #
# tax_recalc
# --------------------------------------------------------------------------- #
def _audit_with_problem(number: str = "123") -> InvoiceAudit:
    audit = InvoiceAudit(
        number=number, issuer="DELTA MÓVEIS", nf_type="Fatura",
        amount=Decimal("100.00"),
        tax_lines=[TaxLine(name="ICMS", base=Decimal("100"),
                           rate=Decimal("0.18"), amount=Decimal("10.00"))],
    )
    verify_invoice_taxes(audit)
    assert audit.problems  # 18% de 100 = 18,00 ≠ 10,00
    return audit


def _audit_ok(number: str = "123") -> InvoiceAudit:
    audit = InvoiceAudit(
        number=number, issuer="DELTA MÓVEIS", nf_type="Fatura",
        amount=Decimal("100.00"),
        tax_lines=[TaxLine(name="ICMS", base=Decimal("100"),
                           rate=Decimal("0.18"), amount=Decimal("18.00"))],
    )
    verify_invoice_taxes(audit)
    assert not audit.problems
    return audit


def _report_md(head: str, problem_msgs: list[str] | None, ok: bool = False) -> str:
    lines = ["# Relatório de Conciliação", "",
             "## 🧾 Auditoria fiscal das faturas e notas", "", f"### {head}", ""]
    if problem_msgs:
        for m in problem_msgs:
            lines.append(f"- 🔴 {m}")
    if ok:
        lines.append("- ✅ Cálculo de impostos conferido.")
    lines.append("")
    return "\n".join(lines)


def _tax_ctx(tmp_path: Path, audit: InvoiceAudit, md_text: str | None) -> CheckContext:
    session = tmp_path / "sessions" / "1"
    if md_text is not None:
        reports = session / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "conciliacao_20260101_000000.md").write_text(md_text, encoding="utf-8")
    else:
        session.mkdir(parents=True, exist_ok=True)
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "sessions")
    parsed = ParsedFile(path="nota.pdf", audits=[audit])
    ctx.sessions_cache = [SessionFile(chat_id="1", path=Path("nota.pdf"),
                                      kind="doc", parsed=parsed)]
    return ctx


def test_parse_report_tax_sections(tmp_path):
    md = _report_md("Fatura 123 — DELTA MÓVEIS (R$ 100,00)",
                    ["ICMS: base 100 × 18.00% (nota) = 18.00, mas a fatura "
                     "informa 10.00 (diferença +8.00)."])
    f = tmp_path / "r.md"
    f.write_text(md, encoding="utf-8")
    sections = tax_recalc.parse_report_tax_sections(f)
    assert "123" in sections
    assert sections["123"]["problems"] == [
        "ICMS: base 100 × 18.00% (nota) = 18.00, mas a fatura informa 10.00 (diferença +8.00)."]
    assert sections["123"]["ok"] is False


def test_parse_report_tax_sections_ok_sem_problema(tmp_path):
    md = _report_md("NFS-e 2490 — SKYFIT (R$ 1.234,56)", None, ok=True)
    f = tmp_path / "r.md"
    f.write_text(md, encoding="utf-8")
    sections = tax_recalc.parse_report_tax_sections(f)
    assert sections["2490"]["ok"] is True
    assert sections["2490"]["problems"] == []


def test_parse_report_ignora_linhas_informativas(tmp_path):
    md = ("# Relatório\n\n## 🧾 Auditoria fiscal das faturas e notas\n\n"
          "### Fatura 123 — DELTA (R$ 100,00)\n\n"
          "- ✅ Cálculo de impostos conferido.\n"
          "- 🟡 ISSQN mascarado(s) na nota — valor(es) calculado(s) pela tabela "
          "de alíquotas.\n"
          "- 🟡 Impostos identificados pelo valor; base/alíquota não estavam "
          "legíveis, então o cálculo não pôde ser conferido.\n")
    f = tmp_path / "r.md"
    f.write_text(md, encoding="utf-8")
    sections = tax_recalc.parse_report_tax_sections(f)
    assert sections["123"]["problems"] == []
    assert sections["123"]["ok"] is True


def test_tax_recalc_problema_nao_sinalizado_e_high(tmp_path):
    audit = _audit_with_problem()
    msg = audit.problems[0].message
    md = _report_md("Fatura 123 — DELTA MÓVEIS (R$ 100,00)", None, ok=True)
    ctx = _tax_ctx(tmp_path, audit, md)
    findings, stats = tax_recalc.run(ctx)
    by_id = {f.id: f for f in findings}
    assert stats["missed"] == 1
    assert by_id["tax_recalc.missed_problem"].severity == Severity.HIGH
    assert msg in by_id["tax_recalc.missed_problem"].evidence


def test_tax_recalc_problema_obsoleto_e_medium(tmp_path):
    audit = _audit_ok()
    md = _report_md("Fatura 123 — DELTA MÓVEIS (R$ 100,00)",
                    ["ICMS: algo que o codigo antigo reportava."])
    ctx = _tax_ctx(tmp_path, audit, md)
    findings, stats = tax_recalc.run(ctx)
    by_id = {f.id: f for f in findings}
    assert stats["stale"] == 1
    assert "tax_recalc.missed_problem" not in by_id
    assert by_id["tax_recalc.stale_problem"].severity == Severity.MEDIUM


def test_tax_recalc_sem_drift_quando_mensagens_iguais(tmp_path):
    audit = _audit_with_problem()
    msg = audit.problems[0].message
    md = _report_md("Fatura 123 — DELTA MÓVEIS (R$ 100,00)", [msg])
    ctx = _tax_ctx(tmp_path, audit, md)
    findings, stats = tax_recalc.run(ctx)
    assert findings == []
    assert stats["invoices_compared"] == 1


def test_tax_recalc_sem_relatorio_nao_compara(tmp_path):
    audit = _audit_with_problem()
    ctx = _tax_ctx(tmp_path, audit, None)
    findings, stats = tax_recalc.run(ctx)
    assert findings == []
    assert stats["invoices_compared"] == 0


def test_tax_recalc_merge_relatorios_mais_novo_vence(tmp_path):
    audit = _audit_with_problem()
    msg = audit.problems[0].message
    session = tmp_path / "sessions" / "1"
    reports = session / "reports"
    reports.mkdir(parents=True)
    (reports / "conciliacao_20260101_000000.md").write_text(
        _report_md("Fatura 123 — DELTA (R$ 100,00)", ["problema velho."]),
        encoding="utf-8")
    (reports / "conciliacao_20260201_000000.md").write_text(
        _report_md("Fatura 123 — DELTA (R$ 100,00)", [msg]), encoding="utf-8")
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "sessions")
    parsed = ParsedFile(path="nota.pdf", audits=[audit])
    ctx.sessions_cache = [SessionFile(chat_id="1", path=Path("nota.pdf"),
                                      kind="doc", parsed=parsed)]
    findings, stats = tax_recalc.run(ctx)
    assert findings == []  # relatório mais novo já tem o problema atual


def test_run_audit_todos_os_checks(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("📩 chat=1 tipo=text\n", encoding="utf-8")
    report = run_audit(log_paths=[log], sessions_dir=tmp_path / "sem")
    assert set(report.checks_run) == {
        "log_health", "parsing_quality", "tax_recalc",
        "replay", "session_sanity", "kb_consistency",
    }


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
_EXTRATO_CSV = (
    "data,valor,historico\n"
    '10/08/2026,"-150,00",PGTO NF 100\n'
    '12/08/2026,"-200,00",PGTO NF 200\n'
    '13/08/2026,"-50,00",PIX QUALQUER\n'
)
_FATURAS_CSV = (
    "numero,emissor,vencimento,valor\n"
    "100,ACME,10/08/2026,\"150,00\"\n"
    "200,BETA,12/08/2026,\"200,00\"\n"
    "300,GAMA,15/08/2026,\"99,99\"\n"
)


def _replay_session(tmp_path: Path, report_md: str) -> CheckContext:
    session = tmp_path / "sessions" / "1"
    (session / "reports").mkdir(parents=True)
    (session / "extrato.csv").write_text(_EXTRATO_CSV, encoding="utf-8")
    (session / "faturas.csv").write_text(_FATURAS_CSV, encoding="utf-8")
    (session / "reports" / "conciliacao_20260820_120000.md").write_text(
        report_md, encoding="utf-8")
    return CheckContext(cfg={}, sessions_dir=tmp_path / "sessions")


_REPLAY_TABLE = (
    "## Detalhes da conciliação\n\n"
    "| Status | Método | Conf. | Transação | Data | Valor | Fatura | Emissor | Categoria |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
    "| matched | amount_date | 80% | TXN-a | 2026-08-10 | 150.00 | 100 | ACME | — |\n"
    "| matched | amount_date | 80% | TXN-b | 2026-08-12 | 200.00 | 200 | BETA | — |\n"
    "| unmatched_invoice | none | 0% | — | — | — | 300 | GAMA | — |\n"
    "| unmatched_transaction | none | 0% | TXN-c | 2026-08-13 | 50.00 | — | — | — |\n"
)


def test_parse_report_inconsistencies(tmp_path):
    md = ("# Relatório\n\n"
          "## ⚠️ Inconsistências detectadas\n\n"
          "### 1. 🔴 [HIGH] missing_payment\n"
          "- Fatura 300 (GAMA) de 99.99 sem pagamento identificado.\n"
          "- Detalhe: Vencimento: 2026-08-15\n\n"
          + _REPLAY_TABLE)
    f = tmp_path / "r.md"
    f.write_text(md, encoding="utf-8")
    rep = replay.parse_report_inconsistencies(f)
    assert rep["has_section"] is True
    assert rep["messages"] == ["Fatura 300 (GAMA) de 99.99 sem pagamento identificado."]
    assert rep["inv_numbers"] == {"100", "200", "300"}
    assert rep["txn_ids"] == {"TXN-a", "TXN-b", "TXN-c"}


def test_parse_report_nenhuma_inconsistencia(tmp_path):
    md = ("# Relatório\n\n## ✅ Nenhuma inconsistência detectada\n\n" + _REPLAY_TABLE)
    f = tmp_path / "r.md"
    f.write_text(md, encoding="utf-8")
    rep = replay.parse_report_inconsistencies(f)
    assert rep["has_section"] is True
    assert rep["messages"] == []
    assert "300" in rep["inv_numbers"]


def test_replay_divergencia_nao_sinalizada_e_high(tmp_path):
    md = "# Relatório\n\n## ✅ Nenhuma inconsistência detectada\n\n" + _REPLAY_TABLE
    ctx = _replay_session(tmp_path, md)
    findings, stats = replay.run(ctx)
    by_id = {f.id: f for f in findings}
    assert stats["sessions_replayed"] == 1
    assert stats["missed"] == 1
    assert by_id["replay.missed_inconsistency"].severity == Severity.HIGH
    assert "300" in by_id["replay.missed_inconsistency"].evidence


def test_replay_divergencia_obsoleta_e_medium(tmp_path):
    md = ("# Relatório\n\n## ⚠️ Inconsistências detectadas\n\n"
          "### 1. 🔴 [HIGH] missing_payment\n"
          "- Fatura 300 (GAMA) de 99.99 sem pagamento identificado.\n\n"
          + _REPLAY_TABLE.replace("| unmatched_invoice | none | 0% | — | — | — | 300 | GAMA | — |\n", ""))
    ctx = _replay_session(tmp_path, md)
    # paga a 300 em dia: agora não há missing_payment
    extrato = (_EXTRATO_CSV
               .replace('13/08/2026,"-50,00",PIX QUALQUER\n',
                        '15/08/2026,"-99,99",PGTO NF 300\n'))
    (tmp_path / "sessions" / "1" / "extrato.csv").write_text(extrato, encoding="utf-8")
    findings, stats = replay.run(ctx)
    by_id = {f.id: f for f in findings}
    assert stats["stale"] == 1
    assert "replay.missed_inconsistency" not in by_id
    assert by_id["replay.stale_inconsistency"].severity == Severity.MEDIUM


def test_replay_sem_relatorio_pula(tmp_path):
    session = tmp_path / "sessions" / "1"
    session.mkdir(parents=True)
    (session / "extrato.csv").write_text(_EXTRATO_CSV, encoding="utf-8")
    (session / "faturas.csv").write_text(_FATURAS_CSV, encoding="utf-8")
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "sessions")
    findings, stats = replay.run(ctx)
    assert findings == []
    assert stats["sessions_skipped"] == 1


def test_replay_um_lado_só_pula(tmp_path):
    session = tmp_path / "sessions" / "1"
    (session / "reports").mkdir(parents=True)
    (session / "extrato.csv").write_text(_EXTRATO_CSV, encoding="utf-8")
    (session / "reports" / "conciliacao_20260820_120000.md").write_text(
        "# r\n\n## ✅ Nenhuma inconsistência detectada\n", encoding="utf-8")
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "sessions")
    findings, stats = replay.run(ctx)
    assert findings == []
    assert stats["sessions_skipped"] == 1


# --------------------------------------------------------------------------- #
# session_sanity
# --------------------------------------------------------------------------- #
def test_session_sanity_estado_corrompido(tmp_path):
    session = tmp_path / "sessions" / "1"
    session.mkdir(parents=True)
    (session / "extrato.csv").write_text("data,valor\n", encoding="utf-8")
    (session / ".estado_bot.json").write_text("{invalid", encoding="utf-8")
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "sessions")
    findings, stats = session_sanity.run(ctx)
    by_id = {f.id: f for f in findings}
    assert stats["bad_state"] == 1
    assert by_id["session_sanity.bad_state"].severity == Severity.MEDIUM


def test_session_sanity_estado_a_frente_dos_arquivos(tmp_path):
    session = tmp_path / "sessions" / "1"
    session.mkdir(parents=True)
    (session / "extrato.csv").write_text("data,valor\n", encoding="utf-8")
    (session / ".estado_bot.json").write_text(
        '{"executed_file_count": 5}', encoding="utf-8")
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "sessions")
    findings, stats = session_sanity.run(ctx)
    by_id = {f.id: f for f in findings}
    assert stats["state_ahead"] == 1
    assert by_id["session_sanity.state_ahead"].severity == Severity.LOW


def test_session_sanity_sessao_vazia_e_orfa(tmp_path):
    (tmp_path / "sessions" / "vazia").mkdir(parents=True)
    orfa = tmp_path / "sessions" / "orfa"
    (orfa / "reports").mkdir(parents=True)
    (orfa / "reports" / "conciliacao_x.md").write_text("# r\n", encoding="utf-8")
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "sessions")
    findings, stats = session_sanity.run(ctx)
    by_id = {f.id: f for f in findings}
    assert stats["empty"] == 1
    assert stats["orphan_reports"] == 1
    assert by_id["session_sanity.empty_session"].severity == Severity.LOW
    assert by_id["session_sanity.orphan_reports"].severity == Severity.LOW


def test_session_sanity_sessao_ok(tmp_path):
    session = tmp_path / "sessions" / "1"
    session.mkdir(parents=True)
    (session / "extrato.csv").write_text("data,valor\n01/08/2026,-10\n", encoding="utf-8")
    (session / ".estado_bot.json").write_text('{"executed_file_count": 1}',
                                              encoding="utf-8")
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "sessions")
    findings, stats = session_sanity.run(ctx)
    assert findings == []
    assert stats["sessions"] == 1


def test_session_sanity_sem_diretorio(tmp_path):
    ctx = CheckContext(cfg={}, sessions_dir=tmp_path / "nao_existe")
    findings, stats = session_sanity.run(ctx)
    assert findings == []
    assert stats["sessions"] == 0


# --------------------------------------------------------------------------- #
# kb_consistency
# --------------------------------------------------------------------------- #
def test_kb_check_rules_estrutura():
    from src.domain.fiscal_kb import FiscalRule
    rules = [
        FiscalRule(title="Regra A", keywords=("a",), digest="d", text="t"),
        FiscalRule(title="Regra A", keywords=(), digest="d", text=""),
        FiscalRule(title="Regra B", keywords=("b",), digest="d", text="t",
                   topic="topico_inexistente"),
    ]
    errs = kb_consistency.check_rules(rules)
    assert any("duplicado" in e for e in errs)
    assert any("palavras-chave" in e for e in errs)
    assert any("sem texto" in e for e in errs)
    assert any("topic desconhecido" in e for e in errs)


def test_kb_check_rules_kb_real_sem_erro_estrutural():
    from src.domain.fiscal_kb import RULES
    assert kb_consistency.check_rules(RULES) == []


def test_kb_check_iss_table_limpa():
    assert kb_consistency.check_iss_table() == []


def test_kb_check_icms_regras_basicas():
    from src.domain.icms_rates import aliquota_interestadual
    from decimal import Decimal
    assert aliquota_interestadual("SP", "RJ") == Decimal("12")
    assert aliquota_interestadual("SP", "AM") == Decimal("7")
    assert aliquota_interestadual("SP", "SP") is None
    assert aliquota_interestadual("SP", "PA", importado=True) == Decimal("4")
    errs = kb_consistency.check_icms()
    assert isinstance(errs, list)


def test_kb_run_smoke():
    ctx = CheckContext(cfg={})
    findings, stats = kb_consistency.run(ctx)
    assert stats["kb_rules"] > 0
    assert stats["match_smoke_ok"] >= 2
    assert all(f.check == "kb_consistency" for f in findings)
