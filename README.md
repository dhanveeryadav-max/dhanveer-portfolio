# Portfolio backend (Flask)

## Run locally
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then edit .env
python app.py                   # open http://127.0.0.1:5000
```

## API
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/contact` | JSON `{name, email, message}` – validates, saves, emails you |
| GET | `/api/messages` | Your saved messages. Header: `X-Admin-Token: <ADMIN_TOKEN>` |
| GET | `/api/health` | Health check |

## Deploy (Render / Railway / any VPS)
- Start command: `gunicorn app:app`
- Environment variables: copy from `.env.example` and set `TRUST_PROXY=1`
- SQLite file is lost on free hosts that reset disk – attach a persistent disk and set `DB_PATH`.

## If the frontend is hosted separately (Netlify, GitHub Pages)
1. Set `ALLOWED_ORIGIN=https://your-site-url` on the backend.
2. In `index.html`, add before the main script: `<script>window.API_BASE='https://your-backend-url'</script>`
