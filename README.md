# Podcast Núcleo da Fé

Sistema semi-automatizado de geração e publicação de podcast para a igreja Núcleo da Fé. Transforma as transmissões ao vivo do YouTube em episódios de áudio no padrão Apple Podcasts e Spotify, hospedados no **Cloudflare R2** e distribuídos via **GitHub Pages**.

---

## ⚡ Estúdio Web (Recomendado - 1-Clique)

O jeito mais fácil e rápido de gerenciar o podcast é através do **Podcast Studio local**. Ele possui reprodutor visual com marcação de início e fim da pregação com 1 clique, integração direta com o Cloudflare R2, e envia automaticamente as atualizações para o GitHub Pages.

### Como iniciar o Estúdio:

```bash
./studio.sh
```

O comando verificará as dependências e abrirá automaticamente no navegador em:  
**`http://localhost:8000`**

### Funcionalidades do Estúdio:
1. **Transmissões do Canal:** Lista automaticamente os vídeos e cultos recentes do canal `@nucleodafelive` com indicação de status e duração.
2. **Reprodutor com Marcador de Tempo:** Assista ao vídeo e clique em `⏱️ Marcar Posição Atual` no início e fim do sermão para definir os pontos de corte exatos.
3. **Publicação 1-Clique:**
   - Baixa o áudio do YouTube sem bloqueio de bot (IP residencial/local).
   - Corta o sermão e normaliza o áudio para o padrão da indústria (-16 LUFS) com FFmpeg.
   - Faz upload direto do MP3 para o Cloudflare R2 (`podcast-nucleodafe`).
   - Atualiza `podcast.xml` e envia o commit/push para o GitHub Pages.
4. **Player de Episódios:** Ouça diretamente no navegador os episódios já publicados no Cloudflare R2.
5. **Diagnóstico R2:** Teste de conectividade com o bucket e link direto para cadastro no Spotify e Apple Podcasts.

---

## 🛠️ Preparação e Credenciais

As credenciais do Cloudflare R2 e configurações do canal ficam salvas no arquivo `.env` (ignorado no Git):

| Variável | Descrição |
| --- | --- |
| `PODCAST_EMAIL` | E-mail do responsável (`wfmoura@gmail.com`). |
| `PODCAST_FEED_URL` | `https://nfapplications.github.io/podcast-feed/podcast.xml` |
| `PODCAST_IMAGE_URL` | Capa do podcast no Cloudflare R2. |
| `R2_ACCOUNT_ID` | ID da conta Cloudflare. |
| `R2_BUCKET_NAME` | `podcast-nucleodafe` |
| `R2_PUBLIC_DOMAIN` | `https://pub-782ee0b9c17b40bcae3d6d60d29ee756.r2.dev` |
| `R2_ACCESS_KEY_ID` | Chave de acesso S3 do R2. |
| `R2_SECRET_ACCESS_KEY` | Chave secreta S3 do R2. |

---

## 📡 Links de Distribuição

- **Feed RSS de Áudio:** `https://nfapplications.github.io/podcast-feed/podcast.xml` (Spotify, Apple Podcasts, Amazon, etc.)
- **Feed RSS de Vídeo (Vodcast):** `https://nfapplications.github.io/podcast-feed/vodcast.xml` (Apple Podcasts & agregadores com suporte a vídeo)
- **Armazenamento:** Cloudflare R2 `podcast-nucleodafe` (`pub-782ee0b9c17b40bcae3d6d60d29ee756.r2.dev`)
- **Repositório:** `nfapplications/podcast-feed`

### Cadastro nas Plataformas:
- **Spotify:** [podcasters.spotify.com](https://podcasters.spotify.com) → Adicionar podcast existente → Cole a URL do Feed de Áudio (`podcast.xml`). *(Nota: o Spotify ingere áudio via RSS; vídeos no Spotify são gerenciados diretamente pelo painel do Spotify for Creators).*
- **Apple Podcasts:** [podcastsconnect.apple.com](https://podcastsconnect.apple.com) → Adicionar podcast via RSS:
  - Para áudio: Cole a URL de `podcast.xml`.
  - Para vídeo (Vodcast): Crie um programa adicional com a URL de `vodcast.xml`.
- **Amazon Music:** [podcasters.amazon.com](https://podcasters.amazon.com) → Submeter RSS.

---

## 💻 Uso via Linha de Comando (CLI)

```bash
# Ativar ambiente virtual
source .venv/bin/activate

# Listar transmissões do canal
python scan_videos.py --dry-run

# Publicar Áudio (padrão)
python processar_podcast.py --video-id 5CyVFrCs5Pk --start 00:35:00 --end 01:20:00

# Publicar Vídeo (MP4 para vodcast.xml)
python processar_podcast.py --video-id 5CyVFrCs5Pk --start 00:35:00 --end 01:20:00 --format video

# Publicar Ambos (Áudio em podcast.xml + Vídeo em vodcast.xml)
python processar_podcast.py --video-id 5CyVFrCs5Pk --start 00:35:00 --end 01:20:00 --format both
```

# Publicar com corte de pregação (ex: 35 min a 1h20)
python processar_podcast.py --video-id 5CyVFrCs5Pk --start 00:35:00 --end 01:20:00

# Publicar sem corte (vídeos já editados de até 75 min)
python processar_podcast.py --video-id 5CyVFrCs5Pk --force
```
