{{/* 通用 chart 名（可被 nameOverride 覆盖）。 */}}
{{- define "spineagent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* fullname：release 名 + chart 名拼装，作为所有资源名前缀（可被 fullnameOverride 覆盖）。 */}}
{{- define "spineagent.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* chart 标签值（name-version）。 */}}
{{- define "spineagent.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* 标准公共标签。 */}}
{{- define "spineagent.labels" -}}
helm.sh/chart: {{ include "spineagent.chart" . }}
{{ include "spineagent.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* selector 标签（Deployment/Service selector 须稳定，不含 version）。 */}}
{{- define "spineagent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "spineagent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* 非机密环境 ConfigMap 名。 */}}
{{- define "spineagent.envConfigMapName" -}}
{{- printf "%s-env" (include "spineagent.fullname" .) -}}
{{- end -}}

{{/* 机密 Secret 名。 */}}
{{- define "spineagent.secretName" -}}
{{- printf "%s-secrets" (include "spineagent.fullname" .) -}}
{{- end -}}

{{/* 是否需要渲染 Secret：任一 API key 非空时为 true。 */}}
{{- define "spineagent.hasSecrets" -}}
{{- if or .Values.secrets.anthropicApiKey .Values.secrets.openaiApiKey -}}true{{- end -}}
{{- end -}}

{{/* 是否需要渲染非机密 ConfigMap：仅当 extraEnv 非空才有内容（spineagent 无内建 SPINEAGENT_* 旋钮）。 */}}
{{- define "spineagent.hasConfigEnv" -}}
{{- if .Values.extraEnv -}}true{{- end -}}
{{- end -}}

{{/* 各工作负载共用的 envFrom：按需的非机密 ConfigMap +（按需）机密 Secret。两者都可能为空。 */}}
{{- define "spineagent.envFrom" -}}
{{- if include "spineagent.hasConfigEnv" . -}}
- configMapRef:
    name: {{ include "spineagent.envConfigMapName" . }}
{{ end -}}
{{- if include "spineagent.hasSecrets" . -}}
- secretRef:
    name: {{ include "spineagent.secretName" . }}
{{ end -}}
{{- end -}}
