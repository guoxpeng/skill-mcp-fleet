#!/usr/bin/env bash
# 云容器一键脚本：恢复 MCP 副端+隧道 → 装 ollama → 下 Qwen3.8-9B-Distill(Q4_K_M) → 建模型 → 起 OpenAI 兼容服务
# 目标机：Linux 云容器 / 云主机（Ubuntu/Debian 系为主），root 运行
# 日志：/mnt/workspace/qwen38/setup.log
# 幂等：重复执行只补齐缺失步骤，已完成的会跳过；模型下载可断点续传。
set -u

BASE=/mnt/workspace/qwen38
PORT=3100
MCPOPT="--name cloud --port $PORT --tunnel"

MODEL_REPO="empero-ai/Qwen3.8-9B-Distill-GGUF"
MODEL_FILE="Qwen3.8-9B-Q4_K_M.gguf"
MODEL_URL="https://modelscope.cn/models/$MODEL_REPO/resolve/master/$MODEL_FILE"
MODEL_SIZE=5780090176          # 魔搭标注的精确字节数
MODEL_NAME="qwen3.8-9b"

GH=("https://ghfast.top/https://github.com" "https://ghproxy.net/https://github.com" "https://gh-proxy.com/https://github.com" "https://github.com")
MCP_MIRROR=("https://ghfast.top/https://raw.githubusercontent.com" "https://ghproxy.net/https://raw.githubusercontent.com" "https://gh-proxy.com/https://raw.githubusercontent.com")

mkdir -p "$BASE"
BOOT="$BASE/bootstrap.log"
LOG="$BASE/setup.log"

say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$BOOT"; }

say "=========================================================="
say " 云容器一键：MCP 副端 + ollama + Qwen3.8-9B-Distill"
say "=========================================================="

# ---------- 0. 环境快照 ----------
say "--- 环境 ---"
say "  主机   : $(hostname)"
say "  架构   : $(uname -m)   核数: $(nproc 2>/dev/null || echo '?')"
say "  内核   : $(uname -r)"
if command -v free >/dev/null 2>&1; then say "  内存   : $(free -g 2>/dev/null | awk '/Mem:/{print $2" GB 总 / "$7" GB 可用"}')"; fi
if command -v df >/dev/null 2>&1; then say "  磁盘   : $(df -h /mnt/workspace 2>/dev/null | awk 'NR==2{print $4" 可用("$5" 已用)"}')"; fi
say "  python3: $(command -v python3 || echo '缺失！')"
say "  zstd   : $(command -v zstd || echo '缺失(稍后自动装)')"
say "  ollama : $(command -v ollama >/dev/null 2>&1 && (ollama --version 2>&1 | head -1) || echo '未安装')"
if command -v nvidia-smi >/dev/null 2>&1; then say "  GPU    : $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1)"; else say "  GPU    : 无（走 CPU 推理）"; fi

# ---------- 1. 恢复 MCP 副端 ----------
say "--- 1/4 恢复 MCP 副端 ---"
if [ -x /opt/cloud_mcp/mcp-ctl.sh ]; then
  say "  已安装，直接启动"
  bash /opt/cloud_mcp/mcp-ctl.sh start >/dev/null 2>&1 || true
  sleep 3
  curl -s -m 10 -o /dev/null -w "  本机自检 HTTP %{http_code}\n" "http://127.0.0.1:$PORT/" || true
else
  say "  未安装，走在线安装（国内镜像）"
  ok=0
  for M in "${MCP_MIRROR[@]}"; do
    say "   试 $M"
    if curl -fsSL --connect-timeout 15 -m 60 "$M/guoxpeng/mcp-fleet/main/install.sh" -o /tmp/mcp-install.sh \
       && [ "$(wc -c < /tmp/mcp-install.sh 2>/dev/null || echo 0)" -gt 40000 ]; then ok=1; break; fi
  done
  if [ "$ok" = 1 ]; then
    md5sum /tmp/mcp-install.sh | tee -a "$BOOT"
    bash -n /tmp/mcp-install.sh && bash /tmp/mcp-install.sh $MCPOPT 2>&1 | tail -25 | tee -a "$BOOT"
  else
    say "  !! 副端安装脚本拉取失败（网络受限），跳过；不影响后续 ollama/MCP 无关步骤"
  fi
fi

# ---------- 2. 隧道（拿新公网地址）----------
say "--- 2/4 启动公网隧道 ---"
if [ -x /opt/cloud_mcp/mcp-tunnel.sh ]; then
  CF_FORCE=1 bash /opt/cloud_mcp/mcp-tunnel.sh 2>&1 | tee -a "$BOOT" | grep -E "公网|MCP 端点|trycloudflare|HTTP|失败" || true
else
  say "  !! 隧道脚本不存在，跳过"
fi

# ---------- 3. 派发后台 worker（装 ollama + 下模型 + 建模型 + 起服务）----------
cat > "$BASE/worker.sh" <<'WORKER_EOF'
#!/usr/bin/env bash
set -u
BASE=/mnt/workspace/qwen38
MODEL_URL="__MODEL_URL__"
MODEL_FILE="__MODEL_FILE__"
MODEL_SIZE=__MODEL_SIZE__
MODEL_NAME="__MODEL_NAME__"
GH=(__GH_ARRAY__)
say(){ echo "[$(date +%H:%M:%S)] $*"; }

say "=== worker 启动（后台，日志 $BASE/setup.log）==="

# ---------- A. 准备 zstd ----------
have_zstd(){ command -v zstd >/dev/null 2>&1; }
if ! have_zstd; then
  say "[A] 安装 zstd ..."
  ( command -v apt-get >/dev/null && apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zstd ) >/dev/null 2>&1 \
  || ( command -v dnf >/dev/null && dnf install -y -q zstd ) >/dev/null 2>&1 \
  || ( command -v yum >/dev/null && yum install -y -q zstd ) >/dev/null 2>&1 \
  || ( command -v apk >/dev/null && apk add --no-cache zstd ) >/dev/null 2>&1 \
  || true
fi
if have_zstd; then say "[A] zstd 就绪: $(zstd --version 2>&1 | head -1)"; else say "[A] zstd 未装上，稍后走 python 解压兜底"; fi

# ---------- B. 通用下载器（显式 Range，先验 206 再追加，绝不删进度）----------
cat > "$BASE/dl.py" <<'PYEOF'
import os, sys, time, urllib.request
url, dst = sys.argv[1], sys.argv[2]
want = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] not in ("", "0") else 0
def size():
    try: return os.path.getsize(dst)
    except OSError: return 0
if want <= 0:
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=90) as r:
            cr = r.headers.get("Content-Range") or ""
            if "/" in cr: want = int(cr.rsplit("/", 1)[1])
            elif r.headers.get("Content-Length"): want = int(r.headers["Content-Length"])
    except Exception as e:
        print("[dl] 取总长失败: %r" % (e,), flush=True)
if want <= 0:
    print("[dl] 无法确定总长度，放弃", flush=True); sys.exit(5)
cur = size()
print("[dl] 目标 %s" % dst, flush=True)
print("[dl] 起始 %d / %d (%.1f%%)" % (cur, want, cur * 100.0 / want), flush=True)
attempt = 0
while cur < want:
    attempt += 1
    if attempt > 500:
        print("[dl] 尝试次数过多，放弃", flush=True); sys.exit(3)
    req = urllib.request.Request(url, headers={"Range": "bytes=%d-" % cur, "User-Agent": "curl/8", "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            if r.getcode() != 206:
                print("[dl] 警告: 期望 206 实得 %d，本轮跳过（保留 %d 字节）" % (r.getcode(), cur), flush=True)
                time.sleep(10); continue
            got = 0; t0 = time.time(); last = 0
            with open(dst, "ab") as f:
                while True:
                    try: b = r.read(1 << 20)
                    except Exception as e:
                        print("[dl] 读中断(%r)，本轮已收 %d 字节" % (e, got), flush=True); break
                    if not b: break
                    f.write(b); got += len(b)
                    now = time.time()
                    if now - last >= 20:
                        last = now
                        tot = size()
                        print("[dl] %d / %d (%.1f%%)  %.2f MB/s" % (tot, want, tot * 100.0 / want, got / max(now - t0, .001) / 1048576.0), flush=True)
            nxt = size()
            if nxt <= cur:
                print("[dl] 无进展(%d)，10s 后重试" % nxt, flush=True); time.sleep(10)
            cur = nxt
    except Exception as e:
        print("[dl] 请求异常 %r，10s 后重试" % (e,), flush=True); time.sleep(10); cur = size()
ok = (size() == want)
print("[dl] 结束 %d / %d  完成=%s" % (size(), want, ok), flush=True)
sys.exit(0 if ok else 4)
PYEOF

# ---------- C. 安装 ollama ----------
say "[C] 检查 ollama ..."
if command -v ollama >/dev/null 2>&1; then
  say "[C] 已安装: $(ollama --version 2>&1 | head -1)"
else
  TARBALL="$BASE/ollama-linux-amd64.tar.zst"
  say "[C] 下载 ollama 运行包（约 1.36 GB，断点续传）"
  for P in "${GH[@]}"; do
    U="$P/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst"
    say "[C]   源: ${P%%//*}"
    python3 "$BASE/dl.py" "$U" "$TARBALL" 0 && break
    say "[C]   该源未完成，换下一个（已有进度保留）"
  done
  SZ=$(stat -c%s "$TARBALL" 2>/dev/null || echo 0)
  say "[C] 已下载 $SZ 字节"
  if [ "$SZ" -gt 100000000 ]; then
    say "[C] 解包到 /usr/local ..."
    if command -v zstd >/dev/null 2>&1; then
      zstd -dc "$TARBALL" | tar -xf - -C /usr/local && say "[C] 解包完成"
    else
      say "[C] 无 zstd，改用 python 解压"
      python3 -m pip install -q --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple zstandard >/dev/null 2>&1 || true
      python3 - <<'PYX'
import sys, tarfile, io
try:
    import zstandard as zstd
except Exception as e:
    print("[C] python 也没有 zstandard，解压失败:", e); sys.exit(1)
p = "/mnt/workspace/qwen38/ollama-linux-amd64.tar.zst"
d = zstd.ZstdDecompressor()
with open(p, "rb") as f, d.stream_reader(f) as r:
    tf = tarfile.open(fileobj=io.BufferedReader(r), mode="r|")
    tf.extractall("/usr/local")
print("[C] python 解压完成")
PYX
    fi
    chmod +x /usr/local/bin/ollama 2>/dev/null || true
  fi
  if command -v ollama >/dev/null 2>&1; then say "[C] ollama 就绪: $(ollama --version 2>&1 | head -1)"; else say "[C] !! ollama 安装未成功，后续步骤会跳过"; fi
fi

# ---------- D. 下载模型 ----------
export OLLAMA_MODELS="$BASE/ollama-models"
mkdir -p "$OLLAMA_MODELS"
GGUF="$BASE/$MODEL_FILE"
say "[D] 下载模型 $MODEL_FILE（5.38 GiB / 魔搭，断点续传）"
CUR=$(stat -c%s "$GGUF" 2>/dev/null || echo 0)
if [ "$CUR" = "$MODEL_SIZE" ]; then
  say "[D] 已下载完整，跳过"
else
  for i in $(seq 1 40); do
    C=$(stat -c%s "$GGUF" 2>/dev/null || echo 0)
    [ "$C" = "$MODEL_SIZE" ] && break
    say "[D] 第 $i 轮，当前 $C ($(( C * 100 / MODEL_SIZE ))%)"
    python3 "$BASE/dl.py" "$MODEL_URL" "$GGUF" "$MODEL_SIZE"
    N=$(stat -c%s "$GGUF" 2>/dev/null || echo 0)
    [ "$N" = "$MODEL_SIZE" ] && break
    if [ "$N" -lt "$C" ]; then say "[D] !! 文件变小($C -> $N)，保留并重试"; fi
    sleep 5
  done
fi
FINAL=$(stat -c%s "$GGUF" 2>/dev/null || echo 0)
if [ "$FINAL" != "$MODEL_SIZE" ]; then
  say "[D] !! 模型未下完（$FINAL / $MODEL_SIZE），退出；重跑本 worker 会接着下"
  exit 2
fi
say "[D] 模型完整: $FINAL 字节"

# ---------- E. 起 ollama serve（先于 create，确保 daemon 在）----------
say "[E] 启动 ollama serve (0.0.0.0:11434) ..."
if ! pgrep -f "ollama serve" >/dev/null 2>&1; then
  OLLAMA_MODELS="$OLLAMA_MODELS" OLLAMA_HOST=0.0.0.0:11434 OLLAMA_KEEP_ALIVE=24h OLLAMA_NUM_PARALLEL=1 \
    setsid nohup ollama serve > "$BASE/ollama.log" 2>&1 < /dev/null &
  sleep 8
fi
curl -s -m 10 -o /dev/null -w "[E] /api/tags HTTP %{http_code}\n" http://127.0.0.1:11434/api/tags || true

# ---------- F. 建模型 ----------
say "[F] ollama create $MODEL_NAME ..."
cat > "$BASE/Modelfile" <<MF
FROM $GGUF
PARAMETER temperature 0.6
PARAMETER top_p 0.95
PARAMETER top_k 20
PARAMETER num_ctx 16384
MF
if OLLAMA_MODELS="$OLLAMA_MODELS" ollama list 2>/dev/null | grep -q "$MODEL_NAME"; then
  say "[F] 已存在，先删除重建"
  OLLAMA_MODELS="$OLLAMA_MODELS" ollama rm "$MODEL_NAME" >/dev/null 2>&1 || true
fi
OLLAMA_MODELS="$OLLAMA_MODELS" ollama create "$MODEL_NAME" -f "$BASE/Modelfile" 2>&1 | tail -20
say "[F] ollama list:"
OLLAMA_MODELS="$OLLAMA_MODELS" ollama list 2>&1 | head -6

# ---------- G. 自检 ----------
say "[G] 真实推理自检（CPU，可能要等一会）"
python3 - <<'PYG'
import json, urllib.request, time
body = {"model": "qwen3.8-9b",
        "messages": [{"role": "user", "content": "只回复两个字：能通"}],
        "stream": False, "options": {"num_predict": 64}}
req = urllib.request.Request("http://127.0.0.1:11434/v1/chat/completions",
                             data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read().decode())
    print("[G] 用时 %.1fs" % (time.time() - t0))
    print("[G] 回复:", json.dumps(d.get("choices", [{}])[0].get("message", {}), ensure_ascii=False)[:500])
    print("[G] 统计:", d.get("usage"))
except Exception as e:
    print("[G] 自检失败: %r" % (e,))
PYG
say "=== worker 完成 ==="
WORKER_EOF

# 占位符替换
python3 - "$BASE/worker.sh" <<'PYR'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
gh = '"https://ghfast.top/https://github.com" "https://ghproxy.net/https://github.com" "https://gh-proxy.com/https://github.com" "https://github.com"'
s = (s.replace("__MODEL_URL__", "https://modelscope.cn/models/empero-ai/Qwen3.8-9B-Distill-GGUF/resolve/master/Qwen3.8-9B-Q4_K_M.gguf")
      .replace("__MODEL_FILE__", "Qwen3.8-9B-Q4_K_M.gguf")
      .replace("__MODEL_SIZE__", "5780090176")
      .replace("__MODEL_NAME__", "qwen3.8-9b")
      .replace("__GH_ARRAY__", gh))
open(p, "w", encoding="utf-8").write(s)
print("worker 已生成，占位符残留:", "__" in s.replace("__pycache__", ""))
PYR
chmod +x "$BASE/worker.sh" 2>/dev/null || true

say "--- 3/4 派发后台任务 ---"
if pgrep -f "$BASE/worker.sh" >/dev/null 2>&1; then
  say "  worker 已在运行，不重复派发"
else
  : > "$LOG"
  setsid nohup bash "$BASE/worker.sh" >> "$LOG" 2>&1 < /dev/null &
  sleep 2
  say "  已派发（pid $(pgrep -f "$BASE/worker.sh" | head -1)）"
fi

say "--- 4/4 完成 ---"
say ""
say "=========================================================="
say " 后台任务已启动，它在做：装 ollama → 下 5.38GiB 模型 → 建模 → 起服务"
say " 预计耗时取决于带宽（容器出网约 1.2 MB/s 时约 1.5 小时）"
say ""
say "   看进度： tail -f $LOG"
say "   看摘要： grep -E '^\\[|ollama|完成' $LOG | tail -30"
say "=========================================================="
say ""
say "★ 把上面【公网隧道地址】那一行发我，我接管后续验证与配置。"
