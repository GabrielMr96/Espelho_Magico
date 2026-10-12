# Orquestrador Espelho Mágico 🧙‍♂️

Automação que lê newsletters do Gmail (marcador `Espelho_Magico`), arranca o lixo (anúncios, patrocínios, CTAs, rodapés) e converte o puro suco da informação em Markdown limpo para injeção no NotebookLM. Os arquivos são salvos localmente e enviados ao Google Drive.

## Fluxo

1. Busca os e-mails **não lidos** do marcador no Gmail.
2. Agrupa por newsletter (prefixos em `config.json`) e define o número da edição automaticamente.
3. Pré-limpa o HTML para Markdown (remove estilos, rastreadores e URLs, o que economiza muitos tokens).
4. Extrai o conteúdo com o Gemini (rotação de chaves + fallback de modelos), com Perplexity opcional.
5. No *Gol Contra*, inclui os placares do dia anterior via API da ESPN.
6. Salva em `resultados_markdown/`, sobe para o Drive e marca os e-mails como lidos.

## 🚀 Como rodar

1. **Ambiente**
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. **Chaves e credenciais**
   * Copie `.env.example` para `.env` e preencha ao menos `GEMINI_API_KEY` (várias chaves separadas por vírgula).
   * Coloque o `credentials.json` (OAuth do Google Cloud, com APIs Gmail e Drive habilitadas) na raiz, ou na pasta indicada em `ESPELHO_SECRETS_DIR`. O `token.json` é gerado no primeiro login.

3. **Execução**
   ```powershell
   python orquestrador.py                       # modo normal (interativo)
   python orquestrador.py --dry-run             # testa sem Drive, sem marcar e-mails
   python orquestrador.py --grupo "tns sports"  # só um grupo
   python orquestrador.py --unificar sim        # unifica grupos com 2+ e-mails sem perguntar
   python orquestrador.py --sem-interacao       # para o Agendador de Tarefas
   python orquestrador.py --help
   ```

## ⚙️ Configuração

| Onde | O quê |
| --- | --- |
| `config.json` | Prefixos → nomes das newsletters, lista de modelos Gemini (ordem de prioridade), filtros de ligas e clubes |
| `.env` | Chaves, `IA_PADRAO`, retentativas (`MAX_RODADAS`, `BACKOFF_*`), `MAX_WORKERS`, `DRIVE_FOLDER_ID`, `GMAIL_LABEL`, `HTML_PRECLEAN`... (veja `.env.example`) |
| `espelho_magico/prompts/` | Prompt principal e complemento do modo unificação |

## 📁 Estrutura

```
orquestrador.py            # ponto de entrada (CLI)
config.json                # configuração editável
espelho_magico/
├── config.py              # caminhos absolutos + variáveis de ambiente
├── log.py                 # console (Rich) + arquivo rotativo
├── gmail_client.py        # autenticação, busca e parsing MIME
├── drive_client.py        # upload/atualização no Drive
├── html_cleaner.py        # pré-limpeza HTML → Markdown
├── llm.py                 # Gemini (retentativas limitadas) + Perplexity
├── futebol.py             # placares ESPN + controle de datas
├── pipeline.py            # agrupamento, extração, deduplicação, publicação
└── prompts/
tests/                     # pytest
```

## 🧪 Testes

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

## 🛠️ Utilitários

* `python validar_chaves.py`: verifica se as chaves Gemini são válidas.
* `python monitora_cota.py`: avisa com som quando a cota do Gemini (429) for restaurada.
* `python teste_futebol.py`: mostra os placares filtrados de ontem.
