# 部署 —— spineagent 容器镜像 + Compose + Helm

> **诚实边界(先读)**:spineagent 是 Spine 家族的通用多 agent 协作【库】,不是常驻服务 ——
> 它【没有】HTTP server、【没有】worker / 队列、【没有】redis / 数据库、【不】落盘、其 `src/`
> 对 `os.environ`【零引用】(没有任何 `SPINEAGENT_*` 应用旋钮)。镜像默认 `CMD` 跑
> `examples/quickstart.py`:一个【一次性、零网络、跑完即退(exit 0=健康)】的离线 demo。
>
> 因此部署层与兄弟包 ragspine 【同源但不照搬】:**对齐**「一个镜像 + 离线默认(mock provider,
> 零 key)+ 真实后端经 env/values 旋钮选择性接入」这套元模式与 chart 结构;**差异**在于 spineagent
> 只有【一个一次性 demo 角色】(无 server/worker/redis 常驻栈),且【不】预置任何 `SPINEAGENT_*`
> 伪旋钮 —— 唯一有效的运行时变量是真实 LLM provider 的 `*_API_KEY`(由厂商 SDK 读取,默认 demo 不需要)。

## 组成

| 路径 | 作用 |
|---|---|
| `deploy/Dockerfile` | uv 驱动;build context = 家族根,先装 `corespine`、再装 `spineagent`。默认 `CMD` 跑离线 demo。 |
| `deploy/compose.yaml` | 单个 `demo` 服务(一次性 `restart: "no"`),离线默认可跑。无 redis/db/卷。 |
| `deploy/.env.example` | 真实 provider 的 `*_API_KEY` 占位符(拷为 `deploy/.env` 用);spineagent 无 `SPINEAGENT_*` 旋钮。 |
| `deploy/helm/spineagent/` | Helm chart:默认渲染一个一次性 demo **Job**;Deployment/Service/Ingress 为前瞻骨架,默认关闭。 |
| `deploy/.dockerignore` | 裁剪构建上下文(详见下「关于 `.dockerignore`」)。 |

## 镜像

镜像构建需要【两个】兄弟包:被依赖的薄核 `corespine` + `spineagent` 自身。所以镜像的
**build context 是家族根** `~/workspace/spine`(里面并排放着 `corespine/` 与 `spineagent/`),
而 `-f` 指向本目录的 `Dockerfile`。镜像内先装 `corespine`、再装 `spineagent`,默认 `CMD`
跑 spineagent 的一键离线 demo(零网络)。

## 构建

```bash
# context = 家族根;-f 指向 spineagent 包内的 Dockerfile
docker build -f spineagent/deploy/Dockerfile -t spineagent:latest ~/workspace/spine
```

## 运行

```bash
docker run --rm spineagent:latest   # 跑离线 demo,成功打印 "spineagent OK"
```

## Docker Compose 一键(默认离线,无需 key)

从 **spineagent 包根**运行(`build.context: ../..` 自动抬到家族根):

```bash
docker compose -f deploy/compose.yaml up --build
```

单个 `demo` 服务跑离线 demo,打印 `spineagent OK` 后 `exit 0`(`restart: "no"`,跑完即退,
不重启 —— 这是一次性 job 语义,不是常驻服务)。无 redis/db、无卷、无端口暴露。

接真实 LLM(可选):`cp deploy/.env.example deploy/.env` 填 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
(由厂商 SDK 读取);并把镜像内入口改成显式选用对应真实 provider(默认 demo 走离线 MockProvider,
这些 key 不需要)。注意 spineagent 自身【不读任何 `SPINEAGENT_*` 旋钮】,故 `.env` 里没有应用旋钮。

静态校验:`docker compose -f deploy/compose.yaml config`。

## 为什么 context 是家族根而不是包根

`spineagent` 在 `dependencies` 里写着 `corespine`,但 `corespine` 还没发到 PyPI(本地是 uv
path source 指向 `../corespine`)。Docker 的 `COPY` 只能取 **build context 内**的文件,而包根
`spineagent/` 里看不到兄弟目录 `corespine/`。把 context 抬到家族根,镜像才能同时 `COPY` 进两个
包,并按 `corespine → spineagent` 的顺序可编辑安装 —— 与本地 `make install` 完全一致。

## 关于 `.dockerignore`

忽略规则在 `deploy/.dockerignore`,其路径相对 **context 根(家族根)**。BuildKit 解析忽略文件的
顺序是:先找 `<-f 指向的 Dockerfile>.dockerignore`,再找 context 根的 `.dockerignore` —— 因此
放在 `deploy/` 下的这份默认不会被自动应用。要让它生效,二选一:

```bash
# 1) 链到 context 根(推荐,保持单一来源)
ln -sf spineagent/deploy/.dockerignore ~/workspace/spine/.dockerignore

# 2) 或改名让 BuildKit 按 Dockerfile 旁路自动识别
cp spineagent/deploy/.dockerignore spineagent/deploy/Dockerfile.dockerignore
```

即便不应用忽略文件,构建结果依然正确:镜像内做的是一次干净的 `uv pip install`,COPY 进来的
`.venv/`、缓存、构建产物都不会被使用,只是徒增上下文体积、拖慢构建。

## Kubernetes:Helm chart

chart 在 [`deploy/helm/spineagent/`](helm/spineagent/),与 Compose 同源同设计,并诚实贴合「库」形态:
**默认渲染一个一次性 demo `Job`**(`examples/quickstart.py`,run-to-completion,`exit 0`=健康),
而非常驻的 Deployment+Service —— 因为 spineagent 当前没有 HTTP server 可服务。Deployment / Service /
Ingress 作为【前瞻骨架】保留(结构对齐 ragspine、备将来 spineagent 真长出 server 入口),由
`server.enabled=true` 显式开启,默认全部关闭。

```bash
# 1) 构建镜像(与 compose 同一个 Dockerfile / 同一个 tag;context = 家族根)
docker build -f spineagent/deploy/Dockerfile -t spineagent:local ~/workspace/spine

# 2) 灌进 kind 节点(否则 pullPolicy=IfNotPresent 在节点上找不到镜像)
kind load docker-image spineagent:local

# 3) 安装(默认离线:跑一次性 demo Job,无需任何 key)
helm install spineagent deploy/helm/spineagent

# 4) 看 demo Job 日志(应见结尾 "spineagent OK")
kubectl logs -l app.kubernetes.io/instance=spineagent,app.kubernetes.io/component=demo
kubectl wait --for=condition=complete job/spineagent-demo --timeout=120s
```

卸载:`helm uninstall spineagent`。

### 旋钮速查(values.yaml)

| values 键 | 作用 | 默认 |
|---|---|---|
| `image.repository` / `image.tag` | 镜像坐标(本地构建 `spineagent:local`) | `spineagent` / `local` |
| `job.enabled` / `job.command` / `job.backoffLimit` | 一次性 demo Job(默认入口=镜像 CMD) | `true` / `[]` / `0` |
| `server.enabled` / `server.command` | 前瞻 HTTP server 骨架(spineagent 当前【无】server) | `false` / `[]` |
| `secrets.anthropicApiKey` / `secrets.openaiApiKey` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`(仅填值才渲染 Secret,厂商 SDK 读取) | 空 |
| `extraEnv` | 自定义非机密 env 逃生舱(spineagent 无内建 `SPINEAGENT_*` 旋钮,故不预置) | `{}` |
| `ingress.*` | 前瞻 Ingress(依赖 `server.enabled`) | `enabled: false` |

### 验证状态(本机实测)

本机有 docker(29.x)/helm(v4.x):

- `docker compose -f deploy/compose.yaml config` 通过(context 正确解析为家族根)。
- `helm lint deploy/helm/spineagent` 通过(0 失败)。
- `helm template` 在【默认】(仅渲染 Job)与【`--set server.enabled=true` + ingress + secrets + extraEnv】
  (渲染全 6 种 kind:Job/Deployment/Service/Ingress/ConfigMap/Secret)两种取值下均正确渲染,
  所有 manifest 经 PyYAML 解析通过。
- 活体 `helm install` / `docker compose up` 的端到端 boot-test 留待远端集群/docker 主机执行。
