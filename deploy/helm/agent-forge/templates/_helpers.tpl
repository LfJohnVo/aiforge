{{/*
Helpers compartidos. Un solo sitio para los nombres y las etiquetas: dos plantillas que
calculan el mismo nombre de dos maneras acaban seleccionando pods distintos, y eso se
descubre cuando un Service deja de encontrar sus endpoints.
*/}}

{{- define "agent-forge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agent-forge.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "agent-forge.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "agent-forge.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 }}
{{ include "agent-forge.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: peak
agent-forge/instance: {{ .Values.env.AGENT_FORGE_INSTANCE | quote }}
{{- end -}}

{{- define "agent-forge.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agent-forge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
La referencia de la imagen. Prefiere el digest: un tag movil permite que dos replicas del
mismo Deployment corran codigo distinto, y ese fallo es indepurable.
*/}}
{{- define "agent-forge.image" -}}
{{- $repo := .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" $repo .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repo (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "agent-forge.configMapName" -}}
{{- default (printf "%s-profile" (include "agent-forge.fullname" .)) .Values.profile.existingConfigMap -}}
{{- end -}}

{{/*
Variables de entorno comunes al Deployment y a los Jobs. Compartidas para que una tarea de
mantenimiento no corra con una configuracion distinta de la celula que mantiene.
*/}}
{{- define "agent-forge.env" -}}
{{- range $key, $value := .Values.env }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
- name: AGENT_FORGE_PROFILE
  value: /app/configs/agent.profile.yaml
- name: LEDGER_PATH
  value: {{ .Values.ledger.mountPath | quote }}
{{- if .Values.observability.otelEndpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.observability.otelEndpoint | quote }}
{{- end }}
{{- range .Values.secrets.keys }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.existingSecret }}
      key: {{ . }}
{{- end }}
{{- end -}}
