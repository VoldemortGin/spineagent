# 部署 —— spineagent 容器镜像

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
