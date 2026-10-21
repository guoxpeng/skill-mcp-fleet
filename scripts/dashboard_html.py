#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard_html -- v5.2 Web Dashboard 单文件页面（零依赖，无 CDN）

由 mcp_agent.py 的 /fleet/ui 路由原样返回；数据来自 /fleet/ui/data 轮询。
认证：节点配置了 auth_token 时，地址栏加 #<token>（fragment 不发送到服务器），
页面存 sessionStorage 后以 Bearer 头轮询数据。

v5.2 新增：身份指纹、安全状态、投递队列（重试/死信）、Agent 任务时间线、
对端指纹与“移除设备”操作（调用 /fleet/peer_remove）。
"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fleet Dashboard</title>
<style>
  :root{
    --bg:#0b1020; --card:#121a33; --card2:#0e1529; --line:#1f2b52;
    --txt:#e2e8f0; --dim:#8294bb; --cyan:#22d3ee; --green:#34d399;
    --red:#f87171; --amber:#fbbf24; --radius:14px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);
    font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    min-height:100vh;padding:22px 26px 40px}
  a{color:var(--cyan);text-decoration:none}
  .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:18px}
  .logo{font-size:20px;font-weight:700;letter-spacing:.5px}
  .logo b{color:var(--cyan)}
  .pill{font-size:12px;padding:3px 10px;border-radius:99px;
    border:1px solid var(--line);color:var(--dim);background:var(--card2)}
  .pill.mono{font-family:ui-monospace,Menlo,Consolas,monospace}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
    background:var(--green);margin-right:6px;animation:breath 2s infinite}
  @keyframes breath{0%,100%{opacity:1}50%{opacity:.35}}
  .spacer{flex:1}
  #refresh{color:var(--dim);font-size:12px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:16px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:13px 15px}
  .kpi .n{font-size:24px;font-weight:700;color:var(--cyan)}
  .kpi .l{font-size:12px;color:var(--dim);margin-top:2px}
  .kpi.warn .n{color:var(--amber)} .kpi.bad .n{color:var(--red)}
  .cols{display:grid;grid-template-columns:1.15fr .85fr;gap:14px}
  @media(max-width:900px){.cols{grid-template-columns:1fr}}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:16px 18px;margin-bottom:14px}
  .panel h3{font-size:13px;color:var(--dim);font-weight:600;margin-bottom:12px;
    letter-spacing:1px;display:flex;align-items:center;gap:8px}
  .panel h3 .tag{font-size:10px;color:var(--cyan);border:1px solid var(--line);
    border-radius:6px;padding:1px 6px;font-weight:400;letter-spacing:0}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{font-size:11px;color:var(--dim);text-align:left;font-weight:500;padding:6px 8px;
    border-bottom:1px solid var(--line)}
  td{padding:8px;border-bottom:1px solid rgba(31,43,82,.55);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  .st{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--red)}
  .st.on{background:var(--green);box-shadow:0 0 7px var(--green)}
  .mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:var(--dim)}
  .badge{font-size:10px;padding:1px 7px;border-radius:6px;border:1px solid var(--line);
    color:var(--dim);white-space:nowrap}
  .badge.ok{color:var(--green);border-color:rgba(52,211,153,.4)}
  .badge.bad{color:var(--red);border-color:rgba(248,113,113,.4)}
  .badge.amber{color:var(--amber);border-color:rgba(251,191,36,.4)}
  .btn{font-size:11px;padding:3px 8px;border-radius:7px;border:1px solid var(--line);
    background:var(--card2);color:var(--dim);cursor:pointer}
  .btn:hover{color:var(--red);border-color:rgba(248,113,113,.5)}
  .msg{padding:9px 12px;margin-bottom:7px;background:var(--card2);
    border:1px solid var(--line);border-radius:10px;font-size:13px}
  .msg .who{font-size:11px;color:var(--cyan);margin-bottom:3px}
  .msg .who span{color:var(--dim)}
  .msg .tx{word-break:break-all;color:var(--txt)}
  .msg .tx.empty{color:var(--dim);font-style:italic}
  .task{display:flex;gap:10px;align-items:flex-start;padding:8px 10px;margin-bottom:6px;
    background:var(--card2);border:1px solid var(--line);border-radius:10px;font-size:12px}
  .task .id{font-family:ui-monospace,Menlo,monospace;color:var(--dim);font-size:10px;flex:none}
  .task .g{flex:1;word-break:break-all}
  .task .meta{color:var(--dim);font-size:10px;flex:none}
  .kv{font-size:12px;color:var(--dim);line-height:1.9}
  .kv b{color:var(--txt);font-weight:600}
  svg text{fill:var(--txt);font-size:11px}
  .foot{margin-top:6px;font-size:11px;color:var(--dim);text-align:center}
  .err{color:var(--red);font-size:13px;padding:20px;text-align:center}
  .empty{color:var(--dim);font-size:12px;padding:8px 0}
</style>
</head>
<body>
  <div class="top">
    <div class="logo">MCP <b>Fleet</b> Dashboard</div>
    <span class="pill" id="node">…</span>
    <span class="pill mono" id="ver">…</span>
    <span class="pill mono" id="fp">…</span>
    <span class="pill" id="ro">…</span>
    <span class="pill"><span class="dot"></span><span id="alive">在线</span></span>
    <div class="spacer"></div>
    <span id="refresh">下次刷新 5s</span>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="n" id="k-peers">–</div><div class="l">已配对节点</div></div>
    <div class="kpi"><div class="n" id="k-online">–</div><div class="l">在线节点</div></div>
    <div class="kpi"><div class="n" id="k-inbox">–</div><div class="l">收件箱消息</div></div>
    <div class="kpi" id="kpi-out"><div class="n" id="k-outbox">–</div><div class="l">待投递</div></div>
    <div class="kpi" id="kpi-dead"><div class="n" id="k-dead">–</div><div class="l">死信</div></div>
    <div class="kpi"><div class="n" id="k-tools">–</div><div class="l">MCP 工具</div></div>
  </div>

  <div class="cols">
    <div>
      <div class="panel">
        <h3>节点 PEERS <span class="tag" id="peer-count">0</span></h3>
        <div id="peers" class="empty">加载中…</div>
      </div>
      <div class="panel">
        <h3>AGENT 任务时间线 <span class="tag">v5.2</span></h3>
        <div id="tasks" class="empty">暂无任务（agent 调用 fleet_plan 后出现）</div>
      </div>
      <div class="panel">
        <h3>MESH 拓扑</h3>
        <svg id="mesh" width="100%" height="230" viewBox="0 0 640 230"></svg>
      </div>
    </div>
    <div>
      <div class="panel">
        <h3>安全状态</h3>
        <div class="kv" id="security">…</div>
      </div>
      <div class="panel">
        <h3>最近消息 INBOX</h3>
        <div id="msgs" class="empty">暂无消息</div>
      </div>
      <div class="panel">
        <h3>本节点 SYSTEM</h3>
        <div class="kv" id="sys">…</div>
      </div>
    </div>
  </div>
  <div class="foot">mcp-fleet v5.2 · 自研协议 · UDP 发现 + 签名请求 + 离线信箱 + Agent 自对话</div>

<script>
(function(){
  var T = "";
  try{
    if(location.hash && location.hash.length > 1){ T = location.hash.slice(1); sessionStorage.setItem("fleet_t", T); }
    else { T = sessionStorage.getItem("fleet_t") || ""; }
  }catch(e){}
  function hdr(req){ if(T) req.setRequestHeader("Authorization","Bearer "+T); }
  function el(id){ return document.getElementById(id); }
  function esc(s){ var d=document.createElement("div"); d.textContent=s==null?"":String(s); return d.innerHTML; }
  function ago(ts){ if(!ts) return ""; var s=Math.round((Date.now()-ts)/1000);
    return s<60?(s+"s 前"):(s<3600?(Math.round(s/60)+"m 前"):(Math.round(s/3600)+"h 前")); }

  function drawMesh(node, peers){
    var svg = el("mesh"); var cx=320, cy=115, R=82;
    var s = "";
    peers.forEach(function(p,i){
      var a = -Math.PI/2 + i*2*Math.PI/peers.length;
      var x = cx+R*Math.cos(a), y = cy+R*Math.sin(a);
      var col = p.online ? "#34d399" : "#47547a";
      s += '<line x1="'+cx+'" y1="'+cy+'" x2="'+x+'" y2="'+y+'" stroke="'+col+'" stroke-width="1.5" opacity="0.7"/>';
      s += '<circle cx="'+x+'" cy="'+y+'" r="16" fill="#0e1529" stroke="'+col+'" stroke-width="2"><title>'+esc(p.name)+'</title></circle>';
      s += '<text x="'+x+'" y="'+(y+4)+'" text-anchor="middle">'+esc(p.name.slice(0,6))+'</text>';
    });
    s += '<line x1="'+cx+'" y1="'+cy+'" x2="'+cx+'" y2="'+(cy-2)+'" stroke="#22d3ee"/>';
    s += '<circle cx="'+cx+'" cy="'+cy+'" r="24" fill="#121a33" stroke="#22d3ee" stroke-width="2.5"><title>'+esc(node)+'</title></circle>';
    s += '<text x="'+cx+'" y="'+(cy+5)+'" text-anchor="middle" style="font-weight:700;fill:#22d3ee">'+esc(node.slice(0,7))+'</text>';
    svg.innerHTML = s;
  }

  function renderPeers(peers){
    if(!peers.length){ el("peers").className="empty";
      el("peers").innerHTML="暂无节点——等待 UDP 自动发现，或用邀请链接配对"; return; }
    el("peers").className="";
    var h = '<table><thead><tr><th>节点</th><th>状态</th><th>指纹 / 信任</th><th>最近</th><th></th></tr></thead><tbody>';
    peers.forEach(function(p){
      h += '<tr>'
        + '<td><b>'+esc(p.name)+'</b><div class="mono">'+esc(p.url)+'</div></td>'
        + '<td><span class="st'+(p.online?" on":"")+'"></span> '
        +   (p.online?'在线':'离线')
        +   (p.cooldown?'<div class="badge amber">冷却 '+p.cooldown+'s</div>':'')+'</td>'
        + '<td class="mono">'+esc(p.fingerprint||"—")+'<div>'
        +   (p.has_pubkey?'<span class="badge ok">签名已配对</span>':'<span class="badge amber">仅 token</span>')
        +   (p.token?'<span class="badge ok">token</span>':'')+'</div></td>'
        + '<td class="mono">'+(p.last_seen_ago!=null?(p.last_seen_ago+'s 前'):"—")+'</td>'
        + '<td><button class="btn" onclick="removePeer(\''+esc(p.name)+'\')">移除</button></td>'
        + '</tr>';
    });
    el("peers").innerHTML = h + '</tbody></table>';
    el("peer-count").textContent = peers.length;
  }

  function renderTasks(tasks){
    if(!tasks || !tasks.length){ el("tasks").className="empty";
      el("tasks").innerHTML="暂无任务（agent 调用 fleet_plan 后出现）"; return; }
    el("tasks").className="";
    var h="";
    tasks.slice().reverse().forEach(function(t){
      h += '<div class="task"><span class="id">'+esc((t.task_id||"").slice(-8))+'</span>'
        + '<span class="g">'+esc(t.goal||"(未命名任务)")+'</span>'
        + '<span class="badge">'+esc(t.status||"?")+' · '+esc(t.steps||0)+' 步</span>'
        + '<span class="meta">'+ago(t.updated)+'</span></div>';
    });
    el("tasks").innerHTML = h;
  }

  function renderSecurity(d){
    var s = d.security || {};
    var a = (s.audit_ok===true)?'<span class="badge ok">审计链正常</span>'
          : (s.audit_ok===false?'<span class="badge bad">审计链异常</span>'
          : '<span class="badge">审计未启用</span>');
    el("security").innerHTML =
      '节点身份 <b class="mono">'+esc((d.identity||{}).fingerprint||"—")+'</b><br>'
      + '邮箱加密 '+(d.encryption?'<span class="badge ok">ON</span>':'<span class="badge bad">OFF</span>')
      + ' · 签名 '+(s.require_signature?'<span class="badge ok">必须</span>':'<span class="badge">可选</span>')
      + ' · TLS '+(s.tls?'<span class="badge ok">ON</span>':'<span class="badge">OFF</span>')+'<br>'
      + '审计 '+a+' · 模式 '+(s.read_only?'<span class="badge amber">只读</span>':'<span class="badge">可写</span>')
      + ' · Agent '+(s.agents?'<span class="badge ok">已启用</span>':'<span class="badge">未启用</span>');
  }

  function render(d){
    el("node").textContent = d.node;
    el("ver").textContent = "v" + d.version;
    el("fp").textContent = (d.identity && d.identity.fingerprint) || "";
    el("ro").textContent = (d.security && d.security.read_only) ? "只读模式" : "可写模式";
    el("k-peers").textContent = d.peers.length;
    el("k-online").textContent = d.peers.filter(function(p){return p.online}).length;
    el("k-inbox").textContent = (d.mailbox && d.mailbox.inbox_total) || 0;
    var pending = (d.delivery && d.delivery.pending) || 0;
    var dead = (d.delivery && d.delivery.dead) || 0;
    el("k-outbox").textContent = pending;
    el("k-dead").textContent = dead;
    el("kpi-out").className = "kpi" + (pending ? " warn" : "");
    el("kpi-dead").className = "kpi" + (dead ? " bad" : "");
    el("k-tools").textContent = d.tools;
    renderPeers(d.peers);
    renderTasks(d.tasks);
    renderSecurity(d);
    var mh = "";
    (d.recent||[]).forEach(function(m){
      mh += '<div class="msg"><div class="who">'+esc(m.from)+' <span>→</span> '+esc(m.to)
        + (m.topic?' <span>#'+esc(m.topic)+'</span>':'')
        + ' <span>· '+esc(m.ts)+'</span></div><div class="tx'+(m.text?"":" empty")+'">'
        + esc(m.text || "(空)") + '</div></div>';
    });
    el("msgs").className = (d.recent||[]).length ? "" : "empty";
    el("msgs").innerHTML = mh || "暂无消息";
    var sy = d.sys || {};
    el("sys").innerHTML = "平台 <b>"+esc(sy.platform||"-")+"</b> · Python <b>"+esc(sy.python||"-")+"</b><br>"
      + "负载 <b>"+esc(sy.load||"-")+"</b>"
      + (sy.disk_free_gb!=null ? (" · 磁盘剩余 <b>"+sy.disk_free_gb+" GB</b>") : "")
      + "<br>刷新于 "+esc(d.ts);
    drawMesh(d.node, d.peers.length ? d.peers : []);
  }

  window.removePeer = function(name){
    if(!confirm("确定移除节点「"+name+"」？\n移除后需重新配对才能互通。")) return;
    var x = new XMLHttpRequest();
    x.open("POST", "/fleet/peer_remove", true);
    x.setRequestHeader("Content-Type","application/json"); hdr(x);
    x.onload = function(){
      if(x.status==200){ tick(); }
      else { alert("移除失败 HTTP "+x.status+"（需要 token 鉴权）"); }
    };
    x.send(JSON.stringify({name:name}));
  };

  var left = 5;
  setInterval(function(){
    left--; el("refresh").textContent = "下次刷新 " + left + "s";
    if(left <= 0){ left = 5; tick(); }
  }, 1000);

  function tick(){
    var x = new XMLHttpRequest();
    x.open("GET", "/fleet/ui/data", true); hdr(x);
    x.onload = function(){
      if(x.status == 200){ try{ render(JSON.parse(x.responseText)); el("alive").textContent="在线"; }catch(e){} }
      else { el("peers").innerHTML = '<div class="err">数据加载失败 HTTP '+x.status
        + '（启用鉴权时请在地址栏加 #你的auth_token）</div>'; }
    };
    x.onerror = function(){ el("alive").textContent = "连接中断"; };
    x.send();
  }
  tick();
})();
</script>
</body>
</html>
"""
