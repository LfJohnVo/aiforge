# Canales (downstream)

> Estado: implementado. API OpenAI-compatible en F1; WebSocket, Teams, Slack y el pipe
> de OpenWebUI en F5.

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

Registrar la célula requiere **sólo la URL base y la API key**, en *Settings →
Connections → OpenAI API*: no hace falta ningún código, porque el canal ya es
OpenAI-compatible. OpenWebUI sondea `{base}/models` al añadir la conexión y luego publica
en `{base}/chat/completions`.

El pipe de `channels/openwebui/pipe.py` es **opcional** y resuelve dos cosas que esa
conexión no puede: propagar la identidad del usuario de OpenWebUI —sin ella la célula ve
un solicitante anónimo y responde sólo con material público (C0)— y renderizar citas y
`awaiting_approval` como estados propios en vez de texto dentro de la respuesta. Se copia
en *Workspace → Functions → New Function*; se ejecuta dentro de OpenWebUI, no dentro de la
célula.

## 4. Copilot Studio

Manifiesto **OpenAPI 3.1** saneado, servido en `GET /openapi/copilot-studio.json` y
generado por `upstream/openapi/` (sin `$ref` sin resolver, sin `anyOf` de nulabilidad, sin
`additionalProperties` libres: los tres constructos que Copilot Studio rechaza). Se
registra como acción personalizada. Ver también `ORCHESTRATORS.md`: Copilot Studio puede consumir la
célula como canal *o* como agente A2A.

## 5. Teams y Slack

Webhooks entrantes con **verificación de firma obligatoria**:

* Teams: validación del JWT del Bot Framework contra el JWKS de Microsoft
  (`https://login.botframework.com/v1/.well-known/keys`), con emisor `https://api.botframework.com`
  y **audiencia fijada a `TEAMS_APP_ID`**. Sin ese app id el webhook rechaza todo: una
  audiencia sin fijar aceptaría el token de cualquier bot del Bot Framework, que es el
  ataque entero.
* Slack: `v0=` HMAC-SHA256 sobre `v0:{timestamp}:{body}` con ventana de 5 minutos. Las dos
  mitades cuentan: la firma prueba quién envía, la ventana impide reproducir una petición
  capturada. Slack reintenta lo que no ve confirmado en 3 s, así que la respuesta se
  produce en segundo plano y el webhook contesta de inmediato.

Una petición con firma inválida o antigua se descarta sin procesar y se registra.

La identidad de la plataforma se mapea a grupos del tenant con `TEAMS_GROUP_MAP` y
`SLACK_GROUP_MAP` (formato `usuario:grupo1|grupo2,usuario2:grupo3`). Un usuario ausente
del mapa queda **autenticado pero sin grupos**, y por tanto sin acceso a nada con ACL: es
lo correcto para quien el directorio no sitúa, y no se adivina.

## 6. WebSocket

`/ws/chat` para UIs propias. Mismo contrato, mensajes JSON delimitados por evento
(`ready`, `token`, `citation`, `awaiting_approval`, `done`, `error`). Autenticación en el
handshake, **antes** de aceptar la conexión: una conexión sin credencial válida se cierra
con 4401 y nunca llega a ser sesión.

La credencial va en la cabecera `Authorization` o, para navegadores —que no pueden fijar
cabeceras en un handshake WebSocket—, en el parámetro `api_key`. Es la razón por la que la
célula debe ir detrás de TLS: un token en un query string es un token en el log de algún
proxy.

Una trama malformada produce un evento `error` y la conversación sigue; no se cierra el
socket.

## 7. Añadir un canal

1. Módulo bajo `channels/<nombre>/` con un `APIRouter`.
2. Implementa `to_channel_request()` y `from_agent_events()`.
3. Actívalo por perfil (`channels.<nombre>.enabled`); si está desactivado, el router no
   se monta.
4. Test que verifique: identidad propagada, firma verificada si aplica, y que el canal
   **no** puede elevar el techo de clasificación.
