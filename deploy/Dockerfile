# syntax=docker/dockerfile:1
#
# spineagent 容器镜像 —— 构建需要【两个】兄弟包:被依赖的薄核 corespine + spineagent 自身。
# 因此 build context 必须是【家族根】 ~/workspace/spine(而非 spineagent 包根),这样才能
# 同时把 corespine 与 spineagent COPY 进来,先装薄核、再装 spineagent。
#
# 构建(context = 家族根,-f 指向本文件):
#   docker build -f spineagent/deploy/Dockerfile -t spineagent:latest ~/workspace/spine
#
# 运行(默认 CMD 跑离线 demo,零网络;退出码即健康信号):
#   docker run --rm spineagent:latest
#
# 忽略规则见同目录 .dockerignore(详见 deploy/README.md 的 .dockerignore 说明)。

# uv 官方镜像:自带 Python 3.11 + uv,贴合家族「uv 管环境」的约定。
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# 先放被依赖的薄核 corespine,再放 spineagent(顺序与下面的安装顺序一致)。
COPY corespine/ /app/corespine/
COPY spineagent/ /app/spineagent/

# 先装 corespine —— 这样装 spineagent 时它的 `corespine` 依赖已被满足(离线、无需 PyPI);
# 再装 spineagent[dev]。--system 直接装进容器系统 Python,容器内无需再建 venv。
RUN uv pip install --system -e /app/corespine \
 && uv pip install --system -e "/app/spineagent[dev]"

# 默认跑 spineagent 的一键离线 demo(零网络),成功打印 "spineagent OK"。
CMD ["python", "/app/spineagent/examples/quickstart.py"]
