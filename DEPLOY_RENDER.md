# Deploy the Meta AI image editor on Render

This fork adds prompt-based image editing to `mir-ashiq/metaai-api`:

- `POST /upload` — stage a PNG/JPEG/WebP
- `POST /edit` — direct image + prompt or staged `upload_id`
- `POST /v1/images/edits` — OpenAI-shaped multipart endpoint

> Unofficial integration: it automates the Meta AI website with Playwright. Meta may change its UI, limit an account, or disallow this use under its terms. Use only your own account and do not expose session cookies.

## 1. Push this folder to your own private GitHub repository

Do not commit `.env` or any cookie values.

```bash
git remote remove origin
git remote add origin https://github.com/YOUR_NAME/YOUR_PRIVATE_REPO.git
git add .
git commit -m "Add Meta AI image editing and Render deployment"
git push -u origin main
```

## 2. Create the Render service

1. In Render, choose **New → Blueprint**.
2. Connect the private repository.
3. Render detects `render.yaml` and builds the Docker image.
4. Add the secret environment values below.

| Variable | Required | Description |
|---|---:|---|
| `META_AI_COOKIE` | yes | Colle la valeur complète de l’en-tête `Cookie` de `meta.ai`. Le serveur extrait automatiquement `datr`, `ecto_1_sess`, `abra_sess` et les autres cookies. Les exports JSON de Cookie-Editor sont également acceptés. |
| `META_AI_API_KEY` | yes | Secret protégeant ton API Render ; généré automatiquement par le Blueprint. |

Tu n’as donc qu’un seul cookie/session à coller dans Render. Ne place jamais cette valeur dans le frontend, dans GitHub ou dans un message. Traite-la comme un mot de passe et renouvelle ta session immédiatement si elle est exposée.

## 3. Check the deployment

```bash
curl https://YOUR-SERVICE.onrender.com/healthz
```

Expected response:

```json
{"status":"ok","version":"5.3.0","browser_ready":false}
```

`browser_ready` becomes true after the first generation/edit request. The first request can be slow because the free Render service may be sleeping.

## 4. Edit an image directly

```bash
curl -X POST https://YOUR-SERVICE.onrender.com/edit \
  -H "Authorization: Bearer YOUR_META_AI_API_KEY" \
  -F 'prompt=Replace the sky with a sunset. Preserve the person exactly.' \
  -F 'image=@photo.jpg'
```

Response:

```json
{
  "success": true,
  "image_urls": ["https://...fbcdn.net/..."],
  "conversation_id": "...",
  "prompt": "Replace the sky with a sunset. Preserve the person exactly.",
  "elapsed_seconds": 42.1
}
```

Download the result promptly because Meta CDN URLs may expire.

## 5. Optional two-step upload

```bash
curl -X POST https://YOUR-SERVICE.onrender.com/upload \
  -H "Authorization: Bearer YOUR_META_AI_API_KEY" \
  -F 'image=@photo.jpg'
```

Then:

```bash
curl -X POST https://YOUR-SERVICE.onrender.com/edit \
  -H "Authorization: Bearer YOUR_META_AI_API_KEY" \
  -F 'prompt=Turn this into a watercolor illustration' \
  -F 'upload_id=UUID_FROM_UPLOAD_RESPONSE'
```

Uploads use Render's ephemeral `/tmp` storage and disappear when the service restarts.

## 6. OpenAI-shaped endpoint

```bash
curl -X POST https://YOUR-SERVICE.onrender.com/v1/images/edits \
  -H "Authorization: Bearer YOUR_META_AI_API_KEY" \
  -F 'prompt=Add sunglasses without changing the face' \
  -F 'image=@portrait.png'
```

Response:

```json
{
  "created": 1780000000,
  "data": [{"url":"https://..."}]
}
```

## Operational limits

- Keep **one Uvicorn worker**. A single Playwright page is stateful and not safe for parallel edits.
- The default upload limit is 15 MiB.
- Supported source formats: PNG, JPEG and WebP.
- Free Render instances sleep and have constrained RAM. Chromium may be slow or be terminated under memory pressure.
- If Meta changes the upload button or composer selectors, update `src/metaai_api/playwright_backend.py`.
- Protect the endpoint with `META_AI_API_KEY`; otherwise strangers could consume your Meta account allowance.
