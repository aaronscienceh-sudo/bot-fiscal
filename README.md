# Bot Fiscal

Bot de Telegram que automatiza rotinas fiscais: busca notas fiscais no e-mail, calcula os impostos de cada nota, concilia com o extrato bancário e responde dúvidas fiscais.

## O que faz

- **Notas do e-mail:** busca as notas do mês escolhido (inclusive quando a nota vem em link) e já envia com os impostos calculados.
- **Cálculo de impostos:** calcula apenas os impostos aplicáveis à nota enviada (ex.: ISS, ICMS, DIFAL).
- **Conciliação:** cruza notas e faturas com o extrato e sinaliza divergências, faturas sem pagamento, pagamentos duplicados e atrasos.
- **Dúvidas fiscais:** primeiro responde com regras determinísticas da base própria (knowledge/); para perguntas livres, usa o LLM local com essas regras como contexto.


## Tecnologias

Python, Telegram Bot API, IMAP, parsing de PDF/CSV/OFX/XML. O cálculo de impostos segue regras determinísticas; um LLM local pequeno (Llama 3.2 1B, via Ollama) ajuda a classificar faturas, ler notas escaneadas e responder perguntas livres, com fallback caso o modelo esteja indisponível.

## Como rodar

1. Instale as dependências: `pip install -r requirements.txt`
2. Copie `.env.example` para `.env` e preencha o token do Telegram e os dados do e-mail
3. Ajuste o `config.yaml` se precisar
4. Rode: `python3 bot.py`

## Supervisor (governança)

Um segundo módulo audita o próprio bot: detecta e relata problemas, sem corrigir nada sozinho. Ele tem 6 verificadores:

- `log_health`: procura erros e vazamento de token no `bot.log`.
- `parsing_quality`: arquivos que não puderam ser lidos.
- `tax_recalc`: recalcula os impostos de cada nota e compara com o resultado do bot.
- `replay`: refaz a conciliação de cada sessão com o código atual e compara com o relatório salvo. Divergência nova pode ser erro que ninguém sinalizou, e divergência que sumiu indica relatório obsoleto.
- `session_sanity`: confere o estado salvo de cada sessão (`.estado_bot.json`), sessões vazias e relatórios órfãos, ou seja, sem arquivos de entrada.
- `kb_consistency`: valida a estrutura das regras fiscais, a tabela de alíquota interestadual de ICMS e a tabela de ISSQN de SP, que sustentam os pareceres do bot.

**Automático, por conversa:** quando o bot processa arquivos, uma auditoria roda em segundo plano (4 verificadores de sessão). O admin recebe o alerta técnico (achados de severidade média ou maior); o usuário recebe, no máximo, uma frase simples, como "Não consegui ler o arquivo X. Pode reenviar?". Para desligar: `supervisor.live: false` no `config.yaml`.

**Sob demanda:**

```bash
python3 auditoria.py                  # auditoria completa (logs + todas as sessões)
python3 auditoria.py --chat ID        # só uma conversa
python3 auditoria.py --only log_health
python3 auditoria.py --notify         # envia o alerta ao admin (TELEGRAM_CHAT_ID)
```

Os relatórios saem em CSV e Markdown.

## Segurança


Durante o desenvolvimento, o `bot.log` guardava o token do Telegram: a biblioteca HTTP inclui a URL da requisição nas mensagens de erro, e a URL da API do Telegram carrega o token. Contei as ocorrências no log e conferi o histórico do Git, e o token nunca foi commitado. A correção foi uma função `redact` aplicada a todo erro impresso e a todo traceback, e o supervisor também mascara tokens em qualquer texto que envia. Os logs antigos foram limpos.

`.env`, `data/` e logs ficam fora do Git (`.gitignore`).

## Limitações

- A auditoria global (logs e base de conhecimento) ainda precisa ser executada manualmente.
- O alerta ao admin foi validado pela linha de comando e por testes, mas não com duas contas reais de Telegram.
- Com `allowed_chat_ids: []`, qualquer conta pode usar o bot. Em produção, restrinja.
- O aviso ao usuário pode repetir arquivos antigos da mesma sessão.
- O supervisor só encontra o que os verificadores sabem procurar.

## Desenvolvimento

Desenvolvido com assistência de IA (OpenCode); o código foi revisado e testado por mim.

## Testes

`python3 -m pytest tests/ -q`

## Aviso

Projeto pessoal em evolução. Os cálculos são apoio à rotina e não substituem a conferência de um contador.
