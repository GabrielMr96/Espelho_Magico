# Orquestrador Espelho Mágico 🧙‍♂️

Automação que lê newsletters do Gmail (marcador `Espelho_Magico`), arranca o lixo (anúncios, patrocínios, CTAs, rodapés e créditos fotográficos) e converte o puro suco da informação em Markdown rigorosamente estruturado e limpo para injeção no NotebookLM. Os arquivos são salvos localmente e sincronizados com o Google Drive.

## 🔄 Fluxo de Processamento

1. **Busca Resiliente no Gmail**: Busca e-mails **não lidos** no marcador configurado, com pacing e backoff automático para respeitar limites de taxa por minuto do Google Cloud (Rate Limit 403).
2. **Agrupamento Inteligente**: Agrupa mensagens por newsletter (mapeamento em `config.json`), detecta o histórico e calcula a numeração da próxima edição automaticamente.
3. **Pré-limpeza HTML → Markdown**: Converte tabelas de layout para blocos enxutos, remove scripts/estilos e expurga créditos fotográficos puros (`Manoella Mello | Globo`, `Fortune | Getty Images`), economizando até 90% dos tokens antes do envio à IA.
4. **Extração Literal com IA**: Extrai o conteúdo sem resumir ou parafrasear usando o Gemini (com rotação de chaves e fallback entre modelos Flash ativos), com Perplexity opcional.
5. **Placares Esportivos**: No *Gol Contra*, injeta automaticamente os placares do dia anterior via API pública da ESPN, filtrando por ligas de elite e clubes de destaque.
6. **Sanitização para NotebookLM (Áudio/Podcast)**:
   - Garante **rigorosamente 1 único cabeçalho `#`** por arquivo (com a data da edição);
   - Remove pontuações terminais (`?`, `!`, `.`) dos títulos `##` para capítulos perfeitos;
   - Elimina resíduos visuais como `(Aprofunde)` e links quebrados de redes sociais;
   - Deduplica seções repetidas mantendo novos desdobramentos.
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
   python orquestrador.py                       # modo normal (interativo)
   python orquestrador.py --dry-run             # simulação: salva em _dry_run sem alterar Gmail/Drive
   python orquestrador.py --grupo "the news"    # filtra apenas um grupo específico
   python orquestrador.py --unificar sim        # mescla 2+ e-mails do mesmo grupo sem perguntar
   python orquestrador.py --sem-interacao       # para execução automatizada (Agendador de Tarefas)
   python orquestrador.py --help                # opções disponíveis
   ```

## ⚙️ Configuração

| Onde | O quê |
| --- | --- |
| `config.json` | Mapeamento de prefixos → títulos, ordem de modelos Gemini (`gemini-3.8-flash`, `gemini-3.7-flash`, etc.), filtros de ligas e clubes |
| `.env` | Chaves de API, `IA_PADRAO`, retentativas (`MAX_RODADAS`, `BACKOFF_*`), workers paralelos (`MAX_WORKERS`), IDs de pasta do Drive, label do Gmail |
| `espelho_magico/prompts/` | Diretrizes de extração literal, tratamento de imagens para áudio e modo unificação |

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
