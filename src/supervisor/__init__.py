"""Supervisor do Bot Fiscal — audita o bot e relata possíveis erros."""
from .models import AuditoriaReport, CheckContext, Finding, Severity
from .notify import build_alert, send_alert
from .runner import (
    SESSION_CHECKS,
    discover_log_paths,
    run_audit,
    run_session_audit,
    write_audit_csv,
    write_audit_markdown,
    write_audit_report,
)

__all__ = [
    "AuditoriaReport",
    "CheckContext",
    "Finding",
    "Severity",
    "SESSION_CHECKS",
    "build_alert",
    "discover_log_paths",
    "run_audit",
    "run_session_audit",
    "send_alert",
    "write_audit_csv",
    "write_audit_markdown",
    "write_audit_report",
]
