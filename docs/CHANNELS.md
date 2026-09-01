# Canales (downstream)

> Estado: API OpenAI-compatible en F1; el resto en F5.

Un canal traduce un transporte concreto al contrato interno de la célula. La regla que
los ordena: **el canal no decide nada**. No filtra conocimiento, no elige modelo, no
autoriza acciones. Sólo normaliza entrada, propaga identidad y serializa la salida.

## 1. Contrato común

Todo canal produce un `ChannelRequest` con:

| Campo | Origen | Si falta |
|---|---|---|
| `messages` | Cuerpo de la petición | Error 400 |
| `tenant_id` | JWT, API key o header firmado | Error 401 |
| `user_id` | JWT `sub` | Techo de clasificación baja a **C0** |
| `groups` | JWT `groups`/`roles` | Lista vacía: sólo contenido sin ACL |
| `thread_id` | Cabecera o campo; si falta se genera | Conversación nueva |
| `stream` | Petición | `false` |

La identidad **nunca** viene del cuerpo del mensaje. Un usuario que escribe "soy
administrador" no es administrador.

## 2. API OpenAI-compatible

`POST /v1/chat/completions` y `GET /v1/models`, con streaming SSE en el formato
`chat.completion.chunk`. Es el canal primario porque habilita OpenWebUI, LibreChat y
cualquier cliente estándar sin adaptador.

Extensiones propias, todas opcionales y en campos con prefijo `x_`: `x_thread_id`,
`x_citations` (citas de la respuesta), `x_task_id`, `x_classification`. Un cliente que
las ignora sigue funcionando.

Autenticación: `Authorization: Bearer <api-key-por-tenant>` o JWT OIDC. La clave por
tenant identifica al tenant, no al usuario: sin JWT el techo es C0.

## 3. OpenWebUI

Pipe empaquetado en `channels/openwebui/`. Registrar la célula requiere sólo la URL base
más la API key, porque el canal ya es OpenAI-compatible. El pipe añade el paso de la
identidad del usuario de OpenWebUI a `user_id`/`groups`.

## 4. Copilot Studio

Manifiesto **OpenAPI 3.1** limpio en `channels/copilot_studio/` (sin `oneOf`
anidados ni `additionalProperties` libres, que Copilot Studio rechaza). Se registra como
acción personalizada. Ver también `ORCHESTRATORS.md`: Copilot Studio puede consumir la
célula como canal *o* como agente A2A.

## 5. Teams y Slack

Webhooks entrantes con **verificación de firma obligatoria**:

* Teams: validación del JWT del Bot Framework contra el JWKS de Microsoft.
* Slack: `v0=` HMAC-SHA256 sobre `timestamp:body` con ventana de 5 minutos.

Una petición con firma inválida o antigua se descarta sin procesar y se registra. La
identidad del usuario de la plataforma se mapea a `user_id`/`groups` mediante el
directorio configurado en el perfil.

## 6. WebSocket

`/ws/chat` para UIs propias. Mismo contrato, mensajes JSON delimitados por evento
(`token`, `citation`, `awaiting_approval`, `done`, `error`). Autenticación en el
handshake; una conexión sin token válido se cierra con 4401.

## 7. Añadir un canal

1. Módulo bajo `channels/<nombre>/` con un `APIRouter`.
2. Implementa `to_channel_request()` y `from_agent_events()`.
3. Actívalo por perfil (`channels.<nombre>.enabled`); si está desactivado, el router no
   se monta.
4. Test que verifique: identidad propagada, firma verificada si aplica, y que el canal
   **no** puede elevar el techo de clasificación.
