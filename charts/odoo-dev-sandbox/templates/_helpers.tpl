{{/*
Expand the name of the chart.
*/}}
{{- define "odoo-dev-sandbox.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "odoo-dev-sandbox.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 43 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 43 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 43 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "odoo-dev-sandbox.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "odoo-dev-sandbox.labels" -}}
helm.sh/chart: {{ include "odoo-dev-sandbox.chart" . }}
{{ include "odoo-dev-sandbox.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "odoo-dev-sandbox.selectorLabels" -}}
app.kubernetes.io/name: {{ include "odoo-dev-sandbox.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Compute the sandbox namespace name.
*/}}
{{- define "odoo-dev-sandbox.namespace" -}}
{{- if .Values.namespace.name }}
{{- .Values.namespace.name }}
{{- else }}
{{- printf "dev-sandbox-%s" .Values.sandbox.branch | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Compute the database name.
*/}}
{{- define "odoo-dev-sandbox.dbName" -}}
{{- if .Values.postgres.database }}
{{- .Values.postgres.database }}
{{- else }}
{{- printf "woow_%s" (.Values.sandbox.branch | replace "-" "_") | trunc 63 }}
{{- end }}
{{- end }}

{{/*
Secret name for credentials.
*/}}
{{- define "odoo-dev-sandbox.secretName" -}}
{{- if .Values.secrets.existingSecret }}
{{- .Values.secrets.existingSecret }}
{{- else }}
{{- include "odoo-dev-sandbox.fullname" . }}-secret
{{- end }}
{{- end }}

{{/*
Component labels for a specific service (odoo, postgres, nginx, pgadmin).
*/}}
{{- define "odoo-dev-sandbox.componentLabels" -}}
app.kubernetes.io/component: {{ .component }}
{{- end }}
