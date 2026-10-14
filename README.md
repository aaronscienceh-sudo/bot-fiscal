# Assistente de Conciliação de Contas e Faturas

IA assistente virtual que faz a **conciliação** de extratos bancários com
faturas/notas, **classifica** cada fatura por categoria e **sinaliza
inconsistências** (valores divergentes, faturas sem pagamento, pagamentos sem
fatura, duplicidades, atrasos).

Usa um **LLM local (Ollama)** para classificação e matching fuzzy, com um
**fallback determinístico** que faz o pipeline funcionar mesmo sem modelo.
Alertas chegam no **Telegram** + relatório em **CSV/Markdown**.

## Arquitetura

```
data/inbox/  (PDF, CSV, XLSX, OFX, XML)
      │
      ▼
┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
│   PARSING   │──▶│   DOMÍNIO    │──▶│   ENGINE         │
│ multi-format│   │ Transaction  │   │ matching determi-│
│ PDF/CSV/OFX │   │ Invoice      │   │ nístico + LLM    │
└─────────────┘   │ Match/Incons.│   │ classificação    │
                  └──────────────┘   │ inconsistências  │
                                     └────────┬─────────┘
                                              ▼
                  ┌──────────────┐   ┌──────────────────┐
                  │   RELATÓRIO  │◀──│   LLM (Ollama)   │
                  │ CSV + MD     │   │ ou fallback      │
                  └──────┬───────┘   └──────────────────┘
                         ▼
                  ┌──────────────┐
                  │  TELEGRAM    │  resumo + anexos
                  └──────────────┘
```

**Camadas** (`src/`):
- `parsing/` — detecta o formato e extrai `Transaction`/`Invoice` (PDF, CSV/TSV, Excel, OFX/QFX, XML).
- `domain/` — modelos (pydantic) + regras determinísticas (normalização, matching, detecção de inconsistências).
- `llm/` — interface plugável: `OllamaClient` (local) e `FallbackClient` (sem LLM).
- `engine/` — orquestra classificação → matching determinístico → matching fuzzy (LLM) → inconsistências.
- `reporting/` — gera CSV detalhado + Markdown legível.
- `notify/` — envia resumo e anexos no Telegram.

## Como a conciliação funciona

1. **Classificação** — cada fatura recebe uma categoria (Energia, Aluguel,
   Software, Tributos, …) via LLM (ou palavras-chave no fallback).
2. **Matching determinístico** (rápido e confiável), em ordem:
   - referência exata (nº da fatura na descrição do movimento);
   - nome + valor exato;
   - valor exato + proximidade de data (vencimento);
   - nome forte + valor próximo.
3. **Matching fuzzy (LLM)** — para o que sobrou, o modelo sugere a fatura mais
   provável com confiança e motivo.
4. **Inconsistências** — valor divergente, fatura sem pagamento, pagamento sem
   fatura, pagamento duplicado, pagamento atrasado, match de baixa confiança.

## Instalação

```bash
cd ~/reconciliacao
python3 -m pip install --user -r requirements.txt   # ou num venv
```

> Este ambiente não tem `python3-venv`; usei `pip --user --break-system-packages`.

### Ollama (opcional, recomendado)

```bash
# instalar o Ollama (se ainda não tiver)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2        # ou outro modelo (qwen2.5, mistral, etc.)
```

Sem Ollama o sistema roda em **modo determinístico** (fallback) — útil para
testar o pipeline.

## Configuração

1. Copie `.env.example` para `.env` e preencha o token do Telegram e o chat id.
2. Ajuste `config.yaml` (modelo, tolerâncias, caminhos).

```bash
set -a; source .env; set +a     # carrega TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
```

## Uso

Coloque os arquivos (extratos e faturas) em `data/inbox/` e rode:

```bash
python3 main.py                  # concilia, gera relatório e envia no Telegram
python3 main.py --no-telegram    # só gera o relatório local
python3 main.py --inbox /caminho --reports /outro
```

Os relatórios saem em `data/reports/` (CSV + Markdown) e os alertas vão para o
Telegram.

## Formatos de entrada suportados

| Formato | Tipo | Observações |
|---|---|---|
| `.csv` / `.tsv` | extrato **ou** faturas | detecta pelo cabeçalho (PT/EN) |
| `.xlsx` / `.xls` | extrato **ou** faturas | todas as abas |
| `.ofx` / `.qfx` / `.xml` | extrato bancário | blocos `<STMTTRN>` |
| `.pdf` | faturas (e extrato) | heurísticas; layouts específicos pedem template |

### Cabeçalhos reconhecidos (CSV/Excel)

- **Extrato:** `data`, `valor`, `descrição`, `referência`, `favorecido`, `conta`
- **Faturas:** `número`, `emissor`, `vencimento`, `valor`, `descrição`

## Testes

```bash
python3 -m pytest tests/ -q
```

## Próximos passos sugeridos

- Templates de PDF por emissor/banco (parsing mais fiel).
- Persistência (SQLite/Postgres) para histórico e re-conciliação incremental.
- Agendamento (cron) para rodar periodicamente.
- Dashboard web para revisão interativa.
