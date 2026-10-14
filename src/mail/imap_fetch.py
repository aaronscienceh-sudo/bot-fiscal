"""Busca notas/faturas em anexo no e-mail do cliente (IMAP).

Caminho 2 do bot: o cliente digita "buscar no email" e o bot baixa os
anexos de documento (PDF, CSV, Excel, OFX) das mensagens da janela de
período; o pipeline concilia diretamente, sem o cliente enviar os
arquivos um a um.

A senha nunca vai para o config.yaml: vem da variável de ambiente
EMAIL_IMAP_PASSWORD (no Gmail, use uma "senha de app").
"""
from __future__ import annotations

import email
import email.header
import email.utils
import hashlib
import imaplib
import re
import unicodedata
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta

# extensões de anexo que o pipeline de parsing reconhece
DOC_EXTS = (".pdf", ".csv", ".xlsx", ".xls", ".ofx")

# sem timeout o socket IMAP pode bloquear para sempre em rede instável e
# travar o bot inteiro (a busca roda na thread principal)
IMAP_TIMEOUT_S = 60

# timeout do download de PDFs linkados no corpo do e-mail (mesmo motivo)
LINK_TIMEOUT_S = 20

# links de download de nota no corpo HTML (eNotas, Spedy, ...):
# <a href=".../pdf"> ou ".../arquivo.pdf"; links de portal/social não casam
_DOC_LINK_RE = re.compile(
    r'href\s*=\s*["\'](https?://[^"\']+?(?:/pdf|\.pdf))["\']',
    re.IGNORECASE,
)

_MONTHS_IMAP = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class EmailFetchError(RuntimeError):
    """Falha ao acessar/buscar no e-mail (rede, login, pasta, busca)."""


@dataclass
class EmailConfig:
    host: str
    port: int
    user: str
    password: str
    folder: str = "INBOX"
    days_back: int = 31
    subject_keywords: tuple[str, ...] = (
        "nota", "nf-", "nfe", "nfse", "fatura", "invoice",
    )
    max_files: int = 50


@dataclass
class EmailFile:
    filename: str
    data: bytes
    subject: str = ""
    sender: str = ""
    sent: date | None = None


def build_email_config(cfg: dict) -> EmailConfig | None:
    """Monta a config do caminho 2 a partir do cfg (+ env já sobrescrito).

    Retorna None quando o caminho está desligado (email.enabled: false) ou
    faltam credenciais — o bot segue funcionando só com o caminho 1.
    """
    e = (cfg or {}).get("email") or {}
    if not e.get("enabled"):
        return None
    user = str(e.get("user") or "").strip()
    password = str(e.get("password") or "").strip()
    if not user or not password:
        return None
    kws = tuple(_strip_accents(str(k)) for k in (e.get("subject_keywords") or ()))
    return EmailConfig(
        host=str(e.get("host") or "imap.gmail.com"),
        port=int(e.get("port") or 993),
        user=user,
        password=password,
        folder=str(e.get("folder") or "INBOX"),
        days_back=int(e.get("days_back") or 31),
        subject_keywords=kws,
        max_files=int(e.get("max_files") or 50),
    )


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                   if unicodedata.category(c) != "Mn")


def _imap_date(d: date) -> str:
    """Data no formato do IMAP (DD-Mon-YYYY)."""
    return f"{d.day:02d}-{_MONTHS_IMAP[d.month - 1]}-{d.year}"


def _period_range(period: str | None, days_back: int) -> tuple[date, date]:
    """Janela de busca: dia (AAAA-MM-DD), mês (AAAA-MM) ou últimos N dias."""
    today = date.today()
    if period:
        parts = [int(x) for x in period.split("-")]
        if len(parts) == 3:
            d = date(*parts)
            if d > today:
                return today, today
            return d, d
        y, mo = parts
        first = date(y, mo, 1)
        last = (date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)) - timedelta(days=1)
        if last > today:
            last = today
        if first > today:
            return today, today
        return first, last
    return today - timedelta(days=days_back), today


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    out = []
    for data, charset in email.header.decode_header(value):
        if isinstance(data, bytes):
            try:
                out.append(data.decode(charset or "utf-8", errors="replace"))
            except LookupError:  # charset inválido (ex.: unknown-8bit)
                out.append(data.decode("utf-8", errors="replace"))
        else:
            out.append(data)
    return "".join(out)


def _subject_matches(subject: str, keywords: tuple[str, ...]) -> bool:
    """Assunto casa com alguma palavra-chave (ignora acento/caixa).

    Compara também com o assunto SEM hífen ("NFS-e" -> "nfse") e sem
    espaço: a palavra-chave "nfse" nunca casaria em "NFS-e 1234" do jeito
    normal, e a nota ficaria de fora da busca."""
    if not keywords:
        return True
    s = _strip_accents(subject)
    variants = (s, s.replace("-", ""), re.sub(r"[\s\-_]+", " ", s))
    for k in keywords:
        kk = _strip_accents(k)
        if any(kk in v for v in variants):
            return True
    return False


def _iter_attachments(msg) -> list[tuple[str, bytes]]:
    """Anexos de documento (PDF/CSV/Excel/OFX), inclusive inline com nome."""
    out: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if not filename or not filename.lower().endswith(DOC_EXTS):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001 - anexo corrompido: pula
            continue
        if payload:
            out.append((filename, payload))
    return out


def _iter_linked_docs(msg) -> list[tuple[str, bytes]]:
    """PDFs baixados de links no corpo do e-mail (NFS-e de plataforma:
    eNotas, Spedy, ... chegam SEM anexo — só um <a href=".../pdf">).

    Só segue link de PDF direto; portal de prefeitura/redes sociais não
    casam o padrão. Falha de download não derruba a busca: aquele link é
    pulado e o resto dos anexos segue."""
    urls: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() not in ("text/html", "text/plain"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001 - corpo corrompido: pula
            continue
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        for url in _DOC_LINK_RE.findall(text):
            if url not in urls:
                urls.append(url)
        if len(urls) >= 5:
            break
    out: list[tuple[str, bytes]] = []
    for url in urls[:5]:
        data = _download(url)
        if data:
            out.append((_link_filename(url), data))
    return out


def _download(url: str) -> bytes | None:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (reconciliacao-bot)"})
    try:
        with urllib.request.urlopen(req, timeout=LINK_TIMEOUT_S) as resp:
            data = resp.read()
    except Exception:  # noqa: BLE001 - link morto/timeout: segue o resto
        return None
    # PDF de verdade (não uma página HTML de login/erro salva como .pdf)
    if not data:
        return None
    if data[:5] == b"%PDF-" or b"<Nfse" in data[:2048] or b"NFe" in data[:2048]:
        return data
    return None


def _link_filename(url: str) -> str:
    """Nome estável do arquivo baixado por link (dedupe por hash do conteúdo)."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", url.rsplit("/", 1)[-1].lower())
    if stem in ("pdf", "xml", ""):
        stem = "nota_" + hashlib.sha256(url.encode()).hexdigest()[:8]
    if not stem.endswith(".pdf"):
        stem = stem.rsplit(".", 1)[0] + ".pdf"
    return stem[:60]


def _header_meta(hdr: bytes, keywords: tuple[str, ...]):
    """Cabeçalho -> (assunto, remetente, data) se o assunto passa no filtro."""
    msg = email.message_from_bytes(hdr)
    subject = _decode_header(msg.get("Subject"))
    if not _subject_matches(subject, keywords):
        return None
    sender = email.utils.parseaddr(msg.get("From", ""))[1]
    sent = None
    if msg.get("Date"):
        try:
            dt = email.utils.parsedate_to_datetime(msg.get("Date"))
        except (TypeError, ValueError):
            dt = None
        if dt is not None:
            sent = dt.date()
    return subject, sender, sent


def _header_matches(conn, ids: list[bytes],
                    keywords: tuple[str, ...]) -> dict[bytes, tuple[str, str, date | None]]:
    """Assuntos/remetentes/datas das mensagens da janela, filtrados por
    palavra-chave no assunto.

    Busca os cabeçalhos de TODOS os UIDs em um único FETCH (round-trip
    único) — um FETCH por mensagem deixava a busca em minutos com ~400
    e-mails na janela. Se o servidor não devolver algo parseável, volta
    para o FETCH por UID (lento, mas certo)."""
    matches: dict[bytes, tuple[str, str, date | None]] = {}
    got_any = False
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        uid_set = ",".join(u.decode("ascii", "ignore") for u in chunk)
        try:
            status, msg_data = conn.uid(
                "FETCH", uid_set, "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
        except Exception:  # noqa: BLE001 - servidor não gostou: fallback abaixo
            status, msg_data = "NO", None
        if status != "OK" or not msg_data:
            continue
        for item in msg_data:
            if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
                continue
            m = re.search(rb"\bUID\s+(\d+)", item[0] or b"")
            if not m:
                continue
            got_any = True
            meta = _header_meta(item[1], keywords)
            if meta is not None:
                matches[m.group(1)] = meta
    if got_any:
        return matches
    # fallback: um FETCH por UID (compatível com servidor que não aceitou o lote)
    for uid in ids:
        try:
            status, msg_data = conn.uid(
                "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
        except Exception:  # noqa: BLE001 - mensagem problemática: pula
            continue
        if status != "OK" or not msg_data or msg_data[0] is None:
            continue
        meta = _header_meta(msg_data[0][1], keywords)
        if meta is not None:
            matches[uid] = meta
    return matches


def fetch_invoice_files(ecfg: EmailConfig, period: str | None = None) -> list[EmailFile]:
    """Conecta via IMAP e devolve os anexos de documento das mensagens da
    janela de período (filtro por palavras-chave no assunto)."""
    dmin, dmax = _period_range(period, ecfg.days_back)
    conn = None
    try:
        conn = imaplib.IMAP4_SSL(ecfg.host, ecfg.port, timeout=IMAP_TIMEOUT_S)
        conn.login(ecfg.user, ecfg.password)
        status, _ = conn.select(ecfg.folder, readonly=True)
        if status != "OK":
            raise EmailFetchError(f"não consegui abrir a pasta {ecfg.folder!r}")
        # Gmail não suporta SIN (responde BAD). Janela fechada [dmin, dmax]:
        # SINCE dmin + BEFORE (dmax+1); sem teto quando dmax é hoje (não há
        # mensagem futura) — com SINCE/BEFORE iguais a janela ficaria vazia.
        crit = f"SINCE {_imap_date(dmin)}"
        if dmax < date.today():
            crit += f" BEFORE {_imap_date(dmax + timedelta(days=1))}"
        status, data = conn.uid("SEARCH", None, f"({crit})")
        if status != "OK":
            raise EmailFetchError("falha na busca no e-mail")
        ids = (data[0] or b"").split()
        # etapa 1: só os cabeçalhos (barato) para filtrar por assunto sem
        # baixar corpo/anexos de todas as mensagens da janela — baixar o
        # RFC822 completo de cada uma era o que deixava a busca em minutos
        matches = _header_matches(conn, ids, ecfg.subject_keywords)
        # etapa 2: baixa completa só das mensagens que passaram no filtro
        out: list[EmailFile] = []
        for uid in ids:
            if len(out) >= ecfg.max_files:
                break
            meta = matches.get(uid)
            if meta is None:
                continue
            subject, sender, sent = meta
            status, msg_data = conn.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            docs = _iter_attachments(msg)
            if not docs:
                # NFS-e de plataforma: sem anexo, PDF só num link do corpo
                docs = _iter_linked_docs(msg)
                if not docs:
                    continue
                num = re.search(r"(?:no\.?|nº\.?|n°\.?)\s*(\d{2,})", subject,
                                re.IGNORECASE)
                if num:
                    docs = [(f"nfse_{num.group(1)}.pdf", d) for _, d in docs]
            for filename, payload in docs:
                out.append(EmailFile(filename=filename, data=payload,
                                     subject=subject, sender=sender, sent=sent))
                if len(out) >= ecfg.max_files:
                    break
        return out
    except EmailFetchError:
        raise
    except imaplib.IMAP4.error as e:
        raise EmailFetchError(str(e)) from e
    except OSError as e:
        raise EmailFetchError(f"falha de rede ao conectar em {ecfg.host}:{ecfg.port}") from e
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001 - desconexão: não importa
                pass
