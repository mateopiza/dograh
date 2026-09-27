# Dograh en el VPS de Agentrix (PoC de voz)

Despliegue de Dograh detrás del Traefik de Dokploy, junto a Agentrix y n8n, para validar voz self-hosted. Es de uso interno: una sola organización de Dograh.

Este directorio no modifica `api/` ni `ui/`. Solo añade:

| Archivo | Para qué |
|---|---|
| `docker-compose.override.yaml` | Se aplica encima de `docker-compose.yaml`. Añade Traefik, versión fijada, sin puertos en el host, sin telemetría y coturn. |
| `.env.example` | Variables del despliegue. Se copia a la raíz del repo como `.env`. |

La guía funcional (workflow, BYOK, Twilio, n8n y protocolo de comparación) está en `mateopiza/agentrix-app`, en `docs/VOZ_DOGRAH_POC.md`.

## Arquitectura

```
                    ┌──────── Traefik (Dokploy, 80/443) ────────┐
Navegador ─HTTPS──► │ voice.agentrixlabs.com.co      → ui:3010   │
                    │   └─ /api/v1 (mismo origen)    → api:8000  │
Twilio ──HTTPS/WSS► │ voice-api.agentrixlabs.com.co  → api:8000  │
n8n ───HTTPS──────► │   └─ /voice-audio/<objeto>     → minio:9000│ (solo GET/HEAD)
                    └────────────────────────────────────────────┘
Navegador ═UDP════► coturn (3478, 5349, 49152-49200) ── audio WebRTC
api ──HTTPS──► proveedores BYOK (Deepgram, LLM, ElevenLabs/Cartesia) y services.dograh.com (MPS)
```

- **Twilio Media Streams** usa WebSocket sobre TCP (`wss://voice-api…/api/v1/telephony/ws/...`). Pasa por Traefik sin configuración extra.
- **WebRTC desde el navegador** (probar el agente en la UI) envía el audio por UDP a coturn. Ni Traefik ni el túnel de Cloudflare transportan UDP, así que coturn publica puertos en la IP pública del VPS.
- El túnel `cloudflared` de Agentrix no se usa para Dograh. En este override los perfiles `tunnel` (cloudflared) y `remote` (nginx) de Dograh quedan desactivados aunque se pidan por error.

## 1. Requisitos del VPS

| Escenario | Mínimo | Recomendado |
|---|---|---|
| Solo Dograh (PoC, 1–3 llamadas simultáneas) | 2 vCPU, 8 GB RAM, 20 GB de disco | 4 vCPU, 8 GB |
| Dograh + Agentrix + n8n (+ Typebot) en el mismo VPS | 4 vCPU, 12 GB | **8 vCPU o más, 16 GB o más**, 60 GB SSD |

- La imagen `dograh-api` pesa unos 580 MB. Cada llamada ejecuta un pipeline de audio en el proceso del API. Mide con `docker stats` durante las 10 llamadas de prueba.
- Docker Engine 24 o superior y **Docker Compose 2.24.4 o superior** (el override usa `!reset` y `!override`). Compruébalo con `docker compose version`.
- Traefik v3 (el de Dokploy actual). Las reglas usan `PathRegexp` y `Method` con un solo argumento, que es sintaxis v3.
- IPv4 pública fija.

## 2. DNS y firewall

**DNS:** registros `A` hacia la IP del VPS:

- `voice.agentrixlabs.com.co`
- `voice-api.agentrixlabs.com.co`

Si usas Cloudflare, el proxy (nube naranja) sirve para HTTP y WebSocket. **`TURN_HOST` debe ser la IP pública del VPS**, nunca un nombre que pase por el proxy de Cloudflare. El proxy no transporta UDP.

**Firewall del VPS** (del proveedor y UFW). Además de 80/tcp y 443/tcp, que ya usa Traefik:

| Puerto | Protocolo | Servicio | Para qué |
|---|---|---|---|
| 3478 | UDP | coturn | STUN/TURN (principal) |
| 3478 | TCP | coturn | TURN sobre TCP (redes que bloquean UDP) |
| 5349 | UDP | coturn | TURN TLS/DTLS (reservado) |
| 5349 | TCP | coturn | TURN TLS (reservado) |
| 49152–49200 | UDP | coturn | Relé de medios (unos 24 flujos relayed simultáneos) |

```bash
sudo ufw allow 3478/udp && sudo ufw allow 3478/tcp
sudo ufw allow 5349/udp && sudo ufw allow 5349/tcp
sudo ufw allow 49152:49200/udp
```

> Docker publica puertos saltándose UFW. Por eso este override **quita** los puertos 5432 (Postgres), 6379 (Redis), 8000 (API), 3010 (UI) y 9000/9001 (MinIO) que el compose base publica. Solo coturn publica puertos.

Las llamadas de **Twilio no necesitan** ningún puerto UDP. Solo necesitan 443.

## 3. Variables de entorno

Copia `deploy/agentrix/.env.example` a la raíz del repo como `.env`. Genera los secretos con `openssl rand -hex 32`.

| Variable | Obligatoria | Valor / nota |
|---|---|---|
| `DOGRAH_VERSION` | sí | `1.47.0`. Es la misma etiqueta para `dograh-api` y `dograh-ui`; nunca `latest`. |
| `REGISTRY` | no | `dograhai` (Docker Hub) o `ghcr.io/dograh-hq`. |
| `DOGRAH_UI_HOST` | sí | `voice.agentrixlabs.com.co` |
| `DOGRAH_API_HOST` | sí | `voice-api.agentrixlabs.com.co`. De aquí salen `PUBLIC_BASE_URL`, `BACKEND_API_ENDPOINT` y `MINIO_PUBLIC_ENDPOINT`. |
| `TRAEFIK_NETWORK` | no | `dokploy-network` |
| `TRAEFIK_ENTRYPOINT` / `TRAEFIK_WEB_ENTRYPOINT` | no | `websecure` / `web` (los mismos que usa el `frontend` de Agentrix). |
| `TRAEFIK_CERTRESOLVER` | no | `letsencrypt` |
| `DOGRAH_UPLOAD_ALLOWED_IPS` | no | CIDRs que pueden subir archivos a MinIO desde el navegador. Ver §8. Por defecto `127.0.0.1/32`, es decir, nadie. |
| `TURN_HOST` | sí | IPv4 pública del VPS. También se usa como `SERVER_IP`. |
| `TURN_SECRET` | sí | Secreto compartido API↔coturn. |
| `OSS_JWT_SECRET` | sí | Firma las sesiones. Si lo cambias, se cierran todas las sesiones. |
| `POSTGRES_PASSWORD` | sí | **No cambiar tras el primer arranque**: queda grabado en el volumen. |
| `REDIS_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | sí | Credenciales internas. |
| `TELEPHONY_WS_TOKEN_SECRET` / `_ENFORCE` | recomendado | Firma la URL del WebSocket de medios. Primero pon el secreto con `ENFORCE=false`. Cuando los logs no muestren tokens inválidos, cambia a `true`. |
| `ENABLE_SIGNUP` | sí | `true` solo para crear el primer usuario; después `false`. |

Estos valores los fija el override y no se configuran: `ENVIRONMENT=production`, `ENABLE_TELEMETRY=false`, `POSTHOG_API_KEY=""`, `FASTAPI_WORKERS=1` y `ENABLE_COTURN=true`.

## 4. Arranque

Se ejecuta por SSH, fuera de la interfaz de Dokploy. Dokploy despliega un solo archivo de compose y aquí se combinan dos. Aun así, el Traefik de Dokploy descubre los contenedores por sus etiquetas en `dokploy-network`, igual que los de Agentrix.

```bash
# 1. Código (el compose del repo debe corresponder a la versión de las imágenes)
cd /opt && git clone https://github.com/mateopiza/dograh.git && cd dograh

# 2. Variables
cp deploy/agentrix/.env.example .env && chmod 600 .env
nano .env          # TURN_HOST, secretos, ENABLE_SIGNUP=true (solo la primera vez)

# 3. Atajo para no repetir los -f (añádelo a ~/.bashrc si quieres)
alias dgc='docker compose -p dograh --env-file .env -f docker-compose.yaml -f deploy/agentrix/docker-compose.override.yaml --profile local-turn'

# 4. Validar y arrancar
dgc config -q && echo "compose OK"
dgc pull
dgc up -d
dgc ps             # api "healthy" tras ~60 s (aplica migraciones al arrancar)
```

5. Abre `https://voice.agentrixlabs.com.co`, crea el usuario administrador y confirma que entras.
6. Cierra el registro:

   ```bash
   sed -i 's/^ENABLE_SIGNUP=.*/ENABLE_SIGNUP=false/' .env
   dgc up -d api
   curl -s -X POST https://voice-api.agentrixlabs.com.co/api/v1/auth/signup \
     -H 'content-type: application/json' -d '{"email":"prueba@example.com","password":"no-importa-123","name":"x"}'
   # → {"detail":"Signup is disabled"}
   ```

Comprobaciones rápidas:

```bash
curl -s https://voice-api.agentrixlabs.com.co/api/v1/health            # JSON con backend_api_endpoint
curl -sI https://voice.agentrixlabs.com.co | head -1                     # HTTP/2 200
curl -s -o /dev/null -w '%{http_code}\n' https://voice-api.agentrixlabs.com.co/voice-audio/   # 404: el bucket no se lista
nc -zv <IP_VPS> 5432 6379 8000 9000      # deben fallar todos (no hay puertos publicados)
```

> **Sobre la versión:** el `docker-compose.yaml` del fork puede ir por delante de la última versión publicada. Entre 1.47.0 y `main` a 2026-09-27 solo cambió #788: variables opcionales de métricas, que las imágenes 1.47.0 ignoran. Antes de subir de versión, revisa `git log -- docker-compose.yaml`.

## 5. Telefonía: verificar el WebSocket de medios (Twilio)

Twilio llama por HTTPS a `POST https://voice-api…/api/v1/telephony/inbound/run`. Dograh responde con TwiML `<Connect><Stream url="wss://voice-api…/api/v1/telephony/ws/{workflow}/{org}/{run}">`. Twilio abre entonces ese WebSocket y envía audio μ-law de 8 kHz. Todo va por TCP 443 y Traefik.

1. **El upgrade llega al API (no a la UI, ni un 404 de Traefik):**

   ```bash
   curl -si --http1.1 -N --max-time 5 \
     -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
     -H 'Sec-WebSocket-Version: 13' -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
     https://voice-api.agentrixlabs.com.co/api/v1/telephony/ws/0/0/0 | head -1
   ```

   Esperado: `HTTP/1.1 101 Switching Protocols`. El endpoint acepta y luego cierra porque la llamada `0` no existe. Un `404`, `502` o `200` con HTML indica un problema de ruta.
2. **Durante una llamada real:**

   ```bash
   dgc logs -f api | grep -iE 'inbound|websocket|twilio|stream'
   ```

   Debe aparecer `Inbound /run dispatch received`, después la conexión WebSocket y, al colgar, `WebSocket disconnected`.
3. **En la consola de Twilio**, en Monitor → Logs → Errors, no debe haber errores 11200 (webhook inaccesible), 31920 ni 31921 (fallo del stream WebSocket).
4. **Llamadas largas:** haz una llamada de más de 3 minutos. Si se corta siempre a los ~60 s o ~180 s, el Traefik de Dokploy está cerrando la conexión por timeouts. En `/etc/dokploy/traefik/traefik.yml`, dentro de `entryPoints.websecure.transport.respondingTimeouts`, pon `readTimeout: 0s` e `idleTimeout: 3600s`, y reinicia Traefik.
5. **Firma de Twilio:** Dograh valida `X-Twilio-Signature` contra la URL pública `https://…`. Funciona porque el API confía en `X-Forwarded-Proto` (`FORWARDED_ALLOW_IPS=*`, y el API no tiene puerto publicado). Si ves "signature validation failed", revisa que el Auth Token de la configuración de Twilio en Dograh sea el vigente.

## 6. WebRTC (probar desde el navegador): coturn

- Usa el perfil **`local-turn`**, que ya incluye el alias `dgc`. `dograh-init` renderiza `turnserver.conf` con `external-ip=$TURN_HOST` y `static-auth-secret=$TURN_SECRET`, y coturn lo carga. No uses el perfil `remote`: arranca un nginx en 80/443 que choca con Traefik (este override lo desactiva).
- Verifica que coturn escucha:

  ```bash
  dgc logs coturn | grep -i listen
  sudo ss -lunp | grep -E ':3478|:4915'
  ```
- Prueba desde fuera: en la UI, abre el workflow y usa **Test / Web call**. Si conecta pero no hay audio, casi siempre faltan los puertos UDP en el firewall del proveedor.
- Diagnóstico: `FORCE_TURN_RELAY=true` en `.env` y luego `dgc up -d api`. Así todo el audio pasa por coturn, lo que prueba el relé de extremo a extremo. Vuelve a `false` al terminar.

## 7. Telemetría

- `ENABLE_TELEMETRY=false` desactiva Sentry y el PostHog del navegador (UI).
- **No basta para el API.** `api/services/posthog_client.py` envía eventos siempre que `POSTHOG_API_KEY` tenga valor, y el compose base la trae fija. El override la vacía (`POSTHOG_API_KEY: ""`, `POSTHOG_KEY: ""`).
- Compruébalo:

  ```bash
  dgc exec api env | grep -E 'POSTHOG_API_KEY|ENABLE_TELEMETRY'   # vacío y false
  dgc exec ui env | grep -E 'POSTHOG_KEY|ENABLE_TELEMETRY'
  ```
- Reo.dev, GTM y Meta Pixel solo se cargan si la imagen de la UI se construyó con `NEXT_PUBLIC_*`. Las imágenes oficiales no las traen.

## 8. Tráfico que sale hacia Dograh (MPS) y archivos en MinIO

> ⚠️ **Advertencia:** el procesamiento de documentos de conocimiento **sale a `services.dograh.com`** (Model Proxy Service, `MPS_API_URL`). `api/tasks/knowledge_base_processing.py` envía el archivo a MPS para convertirlo y trocearlo. **En la PoC no subas documentos sensibles** (datos de pacientes, contratos, precios internos). Reemplazar MPS está fuera del alcance de esta fase.

También contacta a MPS:

- **Al crear el primer usuario**, Dograh inicializa la organización: emite una *service key* en MPS, crea una configuración de modelos "Dograh managed" (LLM/STT/TTS de Dograh a través de MPS) e intenta aprovisionar SIP gestionado (Cloudonix). Para que el audio y el texto de las llamadas **no** pasen por Dograh, cambia **LLM, STT y TTS a tus propias claves (BYOK)** en `/model-configurations` antes de la primera llamada. Los pasos están en `docs/VOZ_DOGRAH_POC.md` de agentrix-app.
- No apuntes `MPS_API_URL` a un host inválido: se rompen la inicialización y la base de conocimiento.

**MinIO:** el bucket `voice-audio` se crea con una política anónima de lectura, escritura, borrado y listado (así lo hace Dograh). Por eso Traefik solo expone:

- `GET`/`HEAD` de `/voice-audio/<clave>`: reproducción de grabaciones en la UI y descarga de la transcripción desde n8n. Las claves llevan UUID.
- `PUT`/`OPTIONS` (subidas directas del navegador: documentos, CSV, audios de saludo), solo desde las IPs de `DOGRAH_UPLOAD_ALLOWED_IPS`. Para subir un documento, pon tu IP (`curl -s ifconfig.me`) y ejecuta `dgc up -d minio`; al terminar, vuelve a `127.0.0.1/32`. Si el host pasa por el proxy de Cloudflare, Traefik ve la IP de Cloudflare: desactiva el proxy mientras subes.
- Nunca el listado del bucket ni `DELETE`.

## 9. Backups

Los datos viven en los volúmenes `dograh_postgres_data` (agentes, llamadas, credenciales cifradas) y `dograh_minio-data` (grabaciones, transcripciones y documentos). Redis solo guarda colas y caché, así que no necesita backup. **Guarda también `.env`** en un lugar seguro: sin `OSS_JWT_SECRET` y las contraseñas no se puede restaurar.

```bash
mkdir -p ~/backups/dograh && cd /opt/dograh
set -a; . ./.env; set +a

# Postgres (en caliente, formato custom)
dgc exec -T postgres pg_dump -U postgres -Fc postgres > ~/backups/dograh/pg-$(date +%F).dump

# MinIO (en caliente, espejo del bucket)
docker run --rm --network dograh_app-network \
  -e MC_HOST_local="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000" \
  -v ~/backups/dograh/minio:/backup minio/mc mirror --overwrite local/voice-audio /backup/voice-audio

# Retención simple: 14 días
find ~/backups/dograh -name 'pg-*.dump' -mtime +14 -delete
```

Programa esos comandos en `cron` (por ejemplo, a las 03:15) y copia `~/backups/dograh` fuera del VPS.

**Restaurar:**

```bash
dgc stop api ui
dgc exec -T postgres pg_restore -U postgres -d postgres --clean --if-exists < ~/backups/dograh/pg-AAAA-MM-DD.dump
docker run --rm --network dograh_app-network \
  -e MC_HOST_local="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000" \
  -v ~/backups/dograh/minio:/backup minio/mc mirror --overwrite /backup/voice-audio local/voice-audio
dgc up -d
```

## 10. Actualizar de versión

1. Lee el `CHANGELOG.md` de la versión destino. Busca migraciones y cambios de variables.
2. Haz un backup (§9). **Sin backup no hay vuelta atrás**: el API aplica las migraciones (`alembic upgrade head`) al arrancar, y bajar de versión después de una migración no es seguro.
3. Sincroniza el fork con upstream hasta el commit de esa versión (etiqueta upstream `dograh-vX.Y.Z`) y revisa `git diff` de `docker-compose.yaml`.
4. Cambia `DOGRAH_VERSION=X.Y.Z` en `.env`. Siempre la misma para API y UI; la etiqueta de Docker es semver sin `v`.
5. Aplica:

   ```bash
   dgc config -q && dgc pull api ui && dgc up -d
   dgc ps && curl -s https://voice-api.agentrixlabs.com.co/api/v1/health
   ```
6. Repite una llamada de prueba entrante (§5).

**Volver atrás:** restaura el backup de Postgres (§9), vuelve a poner la `DOGRAH_VERSION` anterior y ejecuta `dgc up -d`.

## 11. Apagar o desinstalar

```bash
dgc down            # detiene y conserva los datos
dgc down -v         # ⚠️ borra también volúmenes (Postgres y MinIO)
```

## Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| `service "nginx" ... disabled-agentrix` o nginx no arranca | Correcto: el perfil `remote` no se usa aquí. |
| `yaml: unknown tag !reset` | Docker Compose anterior a 2.24.4. Actualízalo. |
| La UI carga pero dice "backend unreachable" | El contenedor `api` no está `healthy` o no está en `dokploy-network`. Revisa `dgc logs api`. |
| El certificado no se emite | El DNS aún no apunta al VPS, o Cloudflare está en modo "Flexible". Usa "Full (strict)". |
| La llamada de Twilio suena y cuelga | El número no tiene *Inbound workflow* asignado en `/telephony-configurations`, o el Auth Token está mal. Revisa los logs del API. |
| La prueba web conecta sin audio | Firewall UDP (§2) o `TURN_HOST` no es la IP pública. |
