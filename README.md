# Podcast Núcleo da Fé

Este projeto acompanha as transmissões do [canal Núcleo da Fé](https://www.youtube.com/@nucleodafelive/streams). Quando um vídeo fica pelo menos 10 minutos mais curto e mantém a nova duração em duas verificações diárias, o workflow abre uma issue para revisão. Depois de conferir que o vídeo contém apenas a pregação, execute o workflow manualmente com o ID do vídeo. Ele gera um MP3, envia ao Cloudflare R2, atualiza `podcast.xml` e publica o feed pelo GitHub Pages.

O primeiro registro de um vídeo que já está curto aparece no relatório como **“Curto, sem duração anterior registrada”**. Ele também pode ser publicado manualmente após revisão. A publicação automática fica desativada para evitar que cultos completos entrem no podcast por engano.

## Preparação

1. Coloque estes arquivos no repositório `nfapplications/podcast-feed` e habilite **GitHub Pages → Build and deployment → GitHub Actions**. A publicação por branch não é acionada por commits feitos com `GITHUB_TOKEN`.
2. Crie um bucket Cloudflare R2 e associe um **domínio público próprio**. Use uma URL estável para os MP3; links assinados expiram e `r2.dev` é destinado a testes.
3. Prepare uma capa quadrada para o podcast, entre **1400 × 1400 e 3000 × 3000 pixels**, e hospede-a em uma URL HTTPS pública.
4. Configure as variáveis e os segredos abaixo em **Settings → Secrets and variables → Actions**.

| Tipo | Nome | Valor |
| --- | --- | --- |
| Variável | `PODCAST_EMAIL` | Caixa de e-mail acessível para verificar a propriedade do podcast. Fica pública no RSS. |
| Variável | `PODCAST_IMAGE_URL` | URL HTTPS da capa. |
| Variável | `PODCAST_FEED_URL` | URL final do RSS, por exemplo `https://nfapplications.github.io/podcast-feed/podcast.xml`. |
| Variável | `R2_ACCOUNT_ID` | ID da conta Cloudflare. |
| Variável | `R2_BUCKET_NAME` | Nome do bucket R2. |
| Variável | `R2_PUBLIC_DOMAIN` | Domínio público do bucket, por exemplo `https://audio.exemplo.com`. |
| Segredo | `R2_ACCESS_KEY_ID` | Chave de acesso R2 com permissão no bucket. |
| Segredo | `R2_SECRET_ACCESS_KEY` | Chave secreta R2. |

Não coloque as chaves do R2 em arquivos versionados. O workflow de varredura funciona sem R2 ou capa; esses itens só são necessários para publicar o primeiro episódio.

## Uso

1. Abra **Actions → Monitorar e publicar podcast → Run workflow** e deixe `publish_video_id` vazio. Isso registra as durações iniciais.
2. O workflow verifica o canal todos os dias às **10h17, horário de São Paulo**. Quando detecta um recorte estável ou um vídeo novo já curto, abre uma issue com o link do vídeo. A execução também mostra uma tabela com todos os estados.
3. Confira no YouTube se o vídeo contém apenas a pregação. Rode o mesmo workflow e preencha `publish_video_id` com os 11 caracteres do ID do vídeo.
   - **Corte de transmissões longas:** Se o culto não tiver sido recortado no YouTube Studio, você pode preencher `start_time` (ex: `00:35:00`) e `end_time` (ex: `01:25:00`) diretamente ao disparar o workflow. O FFmpeg extrairá apenas o trecho da pregação.
4. Após a primeira publicação e implantação do Pages, cadastre o RSS no Spotify for Creators e no Apple Podcasts Connect. As plataformas passarão a consultar o feed para novos episódios.

O script só publica IDs vistos na varredura, já curtos e cuja duração não mudou desde a última leitura (ou com cortes manuais definidos via parâmetros/`episodes.json`). O GUID `yt:ID` no próprio RSS impede duplicações, inclusive se um workflow for executado novamente. O áudio é verificado por `HEAD` e `Range` antes de atualizar o feed.

## Execução local

Requer Python 3.12+, FFmpeg e `yt-dlp`:

```bash
python -m pip install -r requirements.txt
python scan_videos.py --dry-run
python processar_podcast.py --video-id ID_DO_VIDEO --dry-run
# Ou com corte de tempo manual:
python processar_podcast.py --video-id ID_DO_VIDEO --start 00:35:00 --end 01:20:00 --dry-run
```

`episodes.json` permite cortes manuais de vídeos longos com `start` e `end` em `HH:MM:SS`, caso seja necessário. O modo padrão do script processa apenas essas entradas; o workflow de publicação usa o ID já revisado na varredura.

Como a única cópia atual está no YouTube, o processamento depende de `yt-dlp` e pode falhar se o YouTube alterar o acesso aos vídeos. Também é preciso confirmar os direitos de reutilização do conteúdo e as condições de uso do YouTube. Guardar os arquivos originais dos próximos cultos tornará o fluxo mais confiável.
