# Orquestrador Espelho Mágico 🧙‍♂️

Automação que lê newsletters do Gmail (marcador `Espelho_Magico`), arranca o lixo (anúncios, patrocínios, CTAs, rodapés e créditos fotográficos) e converte o puro suco da informação em Markdown rigorosamente estruturado e limpo para injeção no NotebookLM. Os arquivos são salvos localmente e sincronizados com o Google Drive.

## 🔄 Fluxo de Processamento

1. **Busca Resiliente no Gmail + Dupla Verificação Web**:
   - Busca e-mails **não lidos** no marcador configurado (`Espelho_Magico`), com pacing e backoff automático para respeitar limites do Google Cloud (Rate Limit 403).
   - Para as newsletters externas configuradas em `fontes_web` (`the news`, `tns money`, `tns health`, `tns sports`, `tns business`, `tns better work`, `aidrops` e `peak`), se algum e-mail não chegar no Gmail, verifica automaticamente nos arquivos públicos dos sites oficiais se saiu edição com data posterior ao último `.md` salvo em `resultados_markdown/`.
   - Também permite extração avulsa diretamente via `--url <link>`.
2. **Agrupamento Inteligente**: Agrupa mensagens por newsletter (mapeamento em `config.json`), detecta o histórico e calcula a numeração da próxima edição automaticamente.
3. **Pré-limpeza HTML → Markdown**: Converte tabelas de layout para blocos enxutos, preserva linhas de tabelas de dados (cotações/mercados), remove menus/rodapés web (`<nav>`, `<footer>`, `<aside>`), blocos de "Leia mais sobre..." e créditos fotográficos puros (`Manoella Mello | Globo`, `Fortune | Getty Images`), economizando até 90% dos tokens antes do envio à IA.
4. **Extração Literal com IA (Fatiamento Anti-504 e Anti-413)**:
   - Extrai o conteúdo sem resumir ou parafrasear usando o Gemini (`gemini-3.8-flash` como prioridade, com rotação de chaves e fallback para provedor alternativo `ALT_*` ou Perplexity com busca web desativada).
   - Entradas extensas (como unificações de múltiplos e-mails) são fatiadas preventivamente por fronteiras de seção (`MAX_CHARS_POR_CHAMADA`) e contam com auto-divisão binária reativa em caso de `504` (timeout) ou `413` (limite de TPM), mantendo o modelo principal sem downgrade.
5. **Placares Esportivos**: No *Gol Contra*, injeta automaticamente os placares do dia anterior via API pública da ESPN logo abaixo do cabeçalho `# Data`, filtrando por ligas de elite e clubes de destaque.
6. **Sanitização para NotebookLM (Áudio/Podcast)**:
   - Garante **rigorosamente 1 único cabeçalho `#`** na linha 1 de cada arquivo (com a data da edição, em português ou 100% em inglês para programas em `prefixos_ingles`);
   - Remove cercas ```` ```markdown ```` externas e pontuações terminais (`?`, `!`, `.`, `:`) dos títulos `##` para capítulos perfeitos;
   - Elimina resíduos visuais como `(Aprofunde)` e links quebrados de redes sociais;
   - Deduplica seções repetidas por escopo (subtítulos `###` só deduplicam dentro do mesmo `##` pai).
7. **Publicação**: Salva em `resultados_markdown/`, envia/atualiza no Google Drive e marca os e-mails como lidos no Gmail.

## 🚀 Como rodar

1. **Ambiente**
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. **Chaves e credenciais**
   * Copie `.env.example` para `.env` e preencha ao menos `GEMINI_API_KEY` (uma ou mais chaves separadas por vírgula para rotação).
   * Coloque o `credentials.json` (OAuth do Google Cloud com APIs Gmail e Drive habilitadas) na raiz ou na pasta indicada por `ESPELHO_SECRETS_DIR`. O `token.json` é gerado automaticamente no primeiro login via navegador.

3. **Execução**
   ```powershell
   python orquestrador.py                       # modo normal (Gmail + verificação automática nos sites oficiais)
   python orquestrador.py --dry-run             # simulação: salva em _dry_run sem alterar Gmail/Drive
   python orquestrador.py --grupo "the news"    # filtra apenas um grupo específico
   python orquestrador.py --unificar sim        # mescla 2+ e-mails do mesmo grupo sem perguntar
   python orquestrador.py --url "https://..."   # extrai diretamente a partir do link de uma edição online
   python orquestrador.py --sem-web             # consulta apenas o Gmail, desativando a verificação nos sites
   python orquestrador.py --ia alt              # usa o provedor alternativo configurado no .env nesta execução
   python orquestrador.py --sem-interacao       # para execução automatizada (Agendador de Tarefas)
   python orquestrador.py --help                # opções disponíveis
   ```

## ⚙️ Configuração

| Onde | O quê |
| --- | --- |
| `config.json` | Mapeamento de prefixos → títulos, arquivos públicos para dupla verificação (`fontes_web`), diretrizes específicas por fonte (`dicas_por_prefixo`), idioma (`prefixos_ingles`), ordem de modelos Gemini e filtros de ligas/clubes |
| `.env` | Chaves de API (`GEMINI_API_KEY`, `ALT_*`, `PERPLEXITY_API_KEY`), `IA_PADRAO`, retentativas (`MAX_RODADAS`, `BACKOFF_*`), tamanho máximo por chamada (`MAX_CHARS_POR_CHAMADA`), verificação web (`VERIFICAR_FONTES_WEB`), workers paralelos (`MAX_WORKERS`), IDs de pasta do Drive, label do Gmail |
| `espelho_magico/prompts/` | Diretrizes de extração literal, tratamento de imagens/tabelas/quizzes para áudio e modo unificação |
| `prompts_notebooklm/` | Prompts universais prontos para copiar e colar no Google NotebookLM (Português e Inglês) |

## 📁 Estrutura

```
orquestrador.py            # ponto de entrada da CLI
config.json                # mapeamentos e configurações de modelos/ligas
espelho_magico/
├── config.py              # caminhos absolutos e variáveis de ambiente
├── log.py                 # formatação de console (Rich) e rotação de logs
├── gmail_client.py        # autenticação OAuth, busca resiliente e parsing MIME
├── drive_client.py        # upload e atualização sem duplicatas no Drive
├── html_cleaner.py        # pré-limpeza HTML → Markdown e filtro de créditos
├── llm.py                 # rotação de chaves e fallback Gemini / Perplexity
├── futebol.py             # placares ESPN com validação e controle de datas
├── pipeline.py            # agrupamento, extração, deduplicação e sanitização final
└── prompts/               # templates de prompts para o Gemini
prompts_notebooklm/        # prompts otimizados para geração de áudio no NotebookLM
tests/                     # suíte completa de testes unitários (pytest)
```

## 🧪 Testes

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

## 🛠️ Utilitários

* `python validar_chaves.py`: verifica o status e cota das chaves Gemini configuradas no `.env`.
* `python monitora_cota.py`: monitora e avisa com som quando uma cota 429 for restabelecida.
* `python teste_futebol.py`: testa e exibe os placares esportivos de ontem via API da ESPN.
