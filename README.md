# Orquestrador Espelho Mágico 🧙‍♂️

Script de automação brute-force para raspar PDFs de newsletters, arrancar o lixo (anúncios, patrocínios, textos inúteis) e converter o puro suco da informação em Markdown limpo para injeção no NotebookLM. 

Utiliza extração híbrida (Marker / MarkItDown) e inteligência artificial para o processamento (Gemini / Groq / Perplexity).

## 🚀 Como Rodar

1. **Prepare o Ambiente**
   Crie um ambiente virtual e instale as dependências:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/Mac
   .venv\Scripts\activate     # Windows
   pip install -r requirements.txt
   ```

2. **Configuração de Chaves e Credenciais**
   * Crie um arquivo `.env` na raiz do projeto contendo as chaves necessárias:
     ```env
     GEMINI_API_KEY=sua_chave_1,sua_chave_2,sua_chave_3
     GROQ_API_KEY=sua_chave_opcional
     PERPLEXITY_API_KEY=sua_chave_opcional
     ```
   * Coloque o seu arquivo de credenciais do Google Workspace (`credentials.json`) na raiz do projeto. O script gerará um `token.json` automaticamente no primeiro acesso para envio dos markdowns para o Google Drive.

3. **Iniciando a Extração**
   Coloque os arquivos PDF das newsletters na mesma pasta e rode o orquestrador:
   ```bash
   python orquestrador.py
   ```

4. **Monitoramento de Cota (Opcional)**
   Caso os limites do Gemini se esgotem (Erro 429), você pode executar o monitor de cotas. Ele vai testar a chave silenciosamente e emitir um aviso sonoro quando o limite for restaurado:
   ```bash
   python monitora_cota.py
   ```
