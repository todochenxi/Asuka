# AgentOS 运行时镜像（M69）。
#
# ---------------------------------------------------------------------------
# 一个镜像，八个进程
#
# AgentOS 有八个进程（api / worker / outbox_publisher / cancellation_sweeper /
# run_cancellation_sweeper / wakeup_controller / recovery_controller /
# child_run_consumer）。它们的**代码完全一样**，不一样的只是入口模块：
#
#     python -m apps.api
#     python -m apps.worker
#     python -m apps.outbox_publisher
#     ...
#
# 所以这里没有八个 Dockerfile。八个镜像意味着八个构建、八份版本号、
# 以及"这八个镜像是不是同一份代码"这个每次上线都要重新回答的问题。
# 一个镜像 + 不同的 `command`，让"跑的是哪份代码"只有一个答案。
#
# ---------------------------------------------------------------------------
# 为什么是 slim 不是 alpine
#
# `psycopg[binary]` 只发了 manylinux wheel（glibc）。alpine 是 musl，
# 装它会退化成源码编译 —— 需要 gcc + libpq-dev，镜像反而更大，
# 而且"能不能装上"取决于编译环境。用 alpine 换来的是
# 一个**更容易失败的构建**和一份更小的数字。
#
# ---------------------------------------------------------------------------
# 为什么用非 root
#
# 容器里的进程能写的东西越少越好。这条不是合规表演：
# 八个进程都拿着数据库连接串，一旦被注入，root 意味着能改镜像里的代码。
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# 心跳文件（M68）：`apps.probe live` 与容器 HEALTHCHECK 都看它。
# 放在 /tmp 是因为它在容器里一定可写，而它本来就是一次性的观测痕迹，
# 不该被放进任何持久卷。
ENV AGENTOS_HEARTBEAT_FILE=/tmp/agentos.heartbeat

WORKDIR /app

# 依赖单独一层：改代码不触发重装依赖。
# 这一层是构建时间的大头，把它放在 COPY 代码之前，
# "改一行 Python 重新构建"才是几秒而不是几分钟。
COPY deploy/requirements-runtime.txt /app/deploy/requirements-runtime.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements-runtime.txt

# 迁移文件要进镜像：`apps.migrate apply` 是上线的一部分（M67），
# 没有它的容器建不出库。
COPY packages/    /app/packages/
COPY apps/        /app/apps/
COPY examples/    /app/examples/
COPY infrastructure/ /app/infrastructure/

RUN useradd --create-home --uid 10001 agentos \
    && chown -R agentos:agentos /app
USER agentos

# 默认起 api。后台进程由编排层用 `command:` 覆盖（见 deploy/k8s/）。
EXPOSE 8000

# 心跳超过 30 秒没跳 = 卡住了。`--start-period` 是必须的：
# 进程起来之后要连库、要装配，那段时间它还没跑到第一轮 tick，
# 没有它，每个容器都会在启动期被判死一次。
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "apps.probe", "live"]

CMD ["python", "-m", "apps.api"]
