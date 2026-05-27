# CustomGreeting

Video greeting generator — Streamlit app that creates personalized video greetings using ElevenLabs TTS and MoviePy.

## Deployment

- **Server:** `wbg-apps` (SSH alias) — `app.whiteboardgeeks.com` / `87.99.151.184`
- **Live URL:** `app.whiteboardgeeks.com/customgreeting/`
- **Install path:** `/opt/customgreeting/app/`
- **Systemd service:** `customgreeting.service` (runs as `deploy` user)
- **Port:** 8002, reverse-proxied by Caddy via `/etc/caddy/apps/customgreeting.caddy`
- **Env file:** `/etc/customgreeting/env`
- **Venv:** `/opt/customgreeting/app/.venv/`

Previously hosted on Render (deactivated). GitHub repo: `whiteboard-geeks/CustomGreeting`.
