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

## Testes

`python3 -m pytest tests/ -q`

## Aviso

Projeto pessoal em evolução. Os cálculos são apoio à rotina e não substituem a conferência de um contador.
