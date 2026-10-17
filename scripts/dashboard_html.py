#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard_html -- v5.0 Web Dashboard 单文件页面（零依赖，无 CDN）

由 mcp_agent.py 的 /fleet/ui 路由原样返回；数据来自 /fleet/ui/data 轮询。
认证说明：节点配置了 auth_token 时，浏览器地址栏加 #<token>（fragment
不会发送到服务器），页面存 sessionStorage 后以 Bearer 头轮询数据。
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
  .top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:20px}
  .logo{font-size:20px;font-weight:700;letter-spacing:.5px}
  .logo b{color:var(--cyan)}
  .pill{font-size:12px;padding:3px 10px;border-radius:99px;
    border:1px solid var(--line);color:var(--dim);background:var(--card2)}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
    background:var(--green);margin-right:6px;animation:breath 2s infinite}
  @keyframes breath{0%,100%{opacity:1}50%{opacity:.35}}
  .spacer{flex:1}
  #refresh{color:var(--dim);font-size:12px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:18px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:14px 16px}
  .kpi .n{font-size:26px;font-weight:700;color:var(--cyan)}
  .kpi .l{font-size:12px;color:var(--dim);margin-top:2px}
  .cols{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}
  @media(max-width:860px){.cols{grid-template-columns:1fr}}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:16px 18px;margin-bottom:14px}
  .panel h3{font-size:14px;color:var(--dim);font-weight:600;margin-bottom:12px;letter-spacing:1px}
  .peer{display:flex;align-items:center;gap:10px;padding:10px 12px;margin-bottom:8px;
    background:var(--card2);border:1px solid var(--line);border-radius:10px}
  .peer .st{width:9px;height:9px;border-radius:50%;flex:none;background:var(--red)}
  .peer.on .st{background:var(--green);box-shadow:0 0 8px var(--green)}
  .peer .nm{font-weight:600;font-size:14px}
  .peer .meta{font-size:11px;color:var(--dim);margin-left:auto;text-align:right}
  .badge{font-size:10px;padding:1px 7px;border-radius:6px;border:1px solid var(--line);
    color:var(--dim);margin-left:6px}
  .badge.ok{color:var(--green);border-color:rgba(52,211,153,.4)}
  .msg{padding:9px 12px;margin-bottom:7px;background:var(--card2);
    border:1px solid var(--line);border-radius:10px;font-size:13px}
  .msg .who{font-size:11px;color:var(--cyan);margin-bottom:3px}
  .msg .who span{color:var(--dim)}
  .msg .tx{word-break:break-all;color:var(--txt)}
  .msg .tx.empty{color:var(--dim);font-style:italic}
  svg text{fill:var(--txt);font-size:11px}
  .foot{margin-top:6px;font-size:11px;color:var(--dim);text-align:center}
  .err{color:var(--red);font-size:13px;padding:20px;text-align:center}
</style>
</head>
<body>
  <div class="top">
    <div class="logo">MCP <b>Fleet</b> Dashboard</div>
    <span class="pill" id="node">…</span>
    <span class="pill" id="ver">…</span>
    <span class="pill" id="enc">…</span>
    <span class="pill"><span class="dot"></span><span id="alive">在线</span></span>
    <div class="spacer"></div>
    <span id="refresh">下次刷新 5s</span>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="n" id="k-peers">–</div><div class="l">已连接节点</div></div>
    <div class="kpi"><div class="n" id="k-online">–</div><div class="l">在线节点</div></div>
    <div class="kpi"><div class="n" id="k-inbox">–</div><div class="l">收件箱消息</div></div>
    <div class="kpi"><div class="n" id="k-outbox">–</div><div class="l">待投递(离线暂存)</div></div>
    <div class="kpi"><div class="n" id="k-tools">–</div><div class="l">MCP 工具</div></div>
  </div>

  <div class="cols">
    <div>
      <div class="panel"><h3>节点 PEERS</h3><div id="peers">加载中…</div></div>
      <div class="panel"><h3>MESH 拓扑</h3>
        <svg id="mesh" width="100%" height="240" viewBox="0 0 640 240"></svg>
      </div>
    </div>
    <div>
      <div class="panel"><h3>最近消息 INBOX</h3><div id="msgs">加载中…</div></div>
      <div class="panel"><h3>本节点 SYSTEM</h3><div id="sys" style="font-size:13px;color:var(--dim)">…</div></div>
    </div>
  </div>
  <div class="foot">mcp-fleet · 零依赖自研协议 · UDP 广播发现 + store-and-forward + gossip</div>

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

  function drawMesh(node, peers){
    var svg = el("mesh"); var cx=320, cy=120, R=86;
    var s = "";
    peers.forEach(function(p,i){
      var a = -Math.PI/2 + i*2*Math.PI/peers.length;
      var x = cx+R*Math.cos(a), y = cy+R*Math.sin(a);
      var col = p.online ? "#34d399" : "#47547a";
      s += '<line x1="'+cx+'" y1="'+cy+'" x2="'+x+'" y2="'+y+'" stroke="'+col+'" stroke-width="1.5" opacity="0.7"/>';
      s += '<circle cx="'+x+'" cy="'+y+'" r="16" fill="#0e1529" stroke="'+col+'" stroke-width="2">';
      s += '<title>'+esc(p.name)+'</title></circle>';
      s += '<text x="'+x+'" y="'+(y+4)+'" text-anchor="middle">'+esc(p.name.slice(0,6))+'</text>';
    });
    s += '<line x1="'+cx+'" y1="'+cy+'" x2="'+cx+'" y2="'+(cy-2)+'" stroke="#22d3ee"/>';
    s += '<circle cx="'+cx+'" cy="'+cy+'" r="24" fill="#121a33" stroke="#22d3ee" stroke-width="2.5">';
    s += '<title>'+esc(node)+'</title></circle>';
    s += '<text x="'+cx+'" y="'+(cy+5)+'" text-anchor="middle" style="font-weight:700;fill:#22d3ee">'+esc(node.slice(0,7))+'</text>';
    svg.innerHTML = s;
  }

  function render(d){
    el("node").textContent = d.node;
    el("ver").textContent = "v" + d.version;
    el("enc").textContent = d.encryption ? "邮箱加密 ON" : "邮箱加密 OFF";
    el("k-peers").textContent = d.peers.length;
    el("k-online").textContent = d.peers.filter(function(p){return p.online}).length;
    el("k-inbox").textContent = (d.mailbox && d.mailbox.inbox_total) || 0;
    el("k-outbox").textContent = (d.mailbox && d.mailbox.outbox_pending) || 0;
    el("k-tools").textContent = d.tools;

    var ph = "";
    if(!d.peers.length){ ph = '<div style="color:var(--dim);font-size:13px">暂无节点——等待 UDP 自动发现（30s 广播一次）</div>'; }
    d.peers.forEach(function(p){
      ph += '<div class="peer'+(p.online?" on":"")+'">'
        + '<span class="st"></span><span class="nm">'+esc(p.name)+'</span>'
        + (p.token?'<span class="badge ok">已配对</span>':'<span class="badge">未配对</span>')
        + (p.cooldown?'<span class="badge">冷却 '+p.cooldown+'s</span>':'')
        + '<div class="meta">'+esc(p.url)
        + '<br>'+esc(p.added_by||"")+(p.last_seen_ago!=null?(' · '+p.last_seen_ago+'s 前发现'):'')
        + '</div></div>';
    });
    el("peers").innerHTML = ph;

    var mh = "";
    if(!d.recent.length){ mh = '<div style="color:var(--dim);font-size:13px">暂无消息</div>'; }
    d.recent.forEach(function(m){
      mh += '<div class="msg"><div class="who">'+esc(m.from)+' <span>→</span> '+esc(m.to)
        + ' <span>· '+esc(m.ts)+'</span></div><div class="tx'+(m.text?"":" empty")+'">'
        + esc(m.text || "(空)") + '</div></div>';
    });
    el("msgs").innerHTML = mh;

    var sy = d.sys || {};
    el("sys").innerHTML = "平台 "+esc(sy.platform||"-")+" · Python "+esc(sy.python||"-")
      + "<br>负载 "+esc(sy.load||"-")
      + (sy.disk_free_gb!=null ? (" · 磁盘剩余 "+sy.disk_free_gb+" GB") : "")
      + "<br>刷新于 "+esc(d.ts);

    drawMesh(d.node, d.peers.length ? d.peers : []);
  }

  var left = 5;
  setInterval(function(){
    left--; el("refresh").textContent = "下次刷新 " + left + "s";
    if(left <= 0){ left = 5; tick(); }
  }, 1000);

  function tick(){
    var x = new XMLHttpRequest();
    x.open("GET", "/fleet/ui/data", true);
    hdr(x);
    x.onload = function(){
      if(x.status == 200){ try{ render(JSON.parse(x.responseText)); }catch(e){} }
      else { el("peers").innerHTML = '<div class="err">数据加载失败 HTTP '+x.status
        + '（节点已启用鉴权时，地址栏加 #你的auth_token）</div>'; }
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
