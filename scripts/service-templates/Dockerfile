# MCP Fleet 节点镜像（纯标准库，无需 pip 依赖）
# 构建（仓库根目录）：docker build -f scripts/service-templates/Dockerfile -t mcp-fleet:{{VERSION}} .
# 运行：docker run -d --name mcp-fleet -p 3100:3100 --restart unless-stopped \
#         --security-opt no-new-privileges:true --cap-drop ALL mcp-fleet:{{VERSION}}
FROM python:3.12-alpine

# tini 做 PID 1：正确转发信号给 python（否则 docker stop 等 10s 才 SIGKILL）
# curl 供健康检查与人工排障；纯标准库运行不需要 pip
RUN apk add --no-cache tini curl

WORKDIR /app
{{COPY_NODE_LINE}}
COPY {{COPY_CONFIG_LINE}} ./mcp_agent_config.example.json

EXPOSE {{PORT}}
# 容器内以 root 运行（exec 工具需要），加固靠 no-new-privileges/cap-drop/read-only
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:{{PORT}}/health',timeout=3).status==200 else 1)"

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "-u", "/app/mcp_agent.py", "--host", "0.0.0.0", "--port", "{{PORT}}"]
