# 沙盒经受限隧道代理访问 tailnet 的限制（P2-12）

主控跑在托管沙盒里时，到 tailnet 的访问不是真 tailscaled，而是一条受限
隧道代理（见主控 `_opener_for` 的 tailnet 规则）。以下限制只影响这种部署，
在自己机器 / VPS 上跑主控时不存在。

## 逐地址审批

- 新 tailnet 地址（IP 或 `.ts.net` 主机名）首次访问要过一次审批；
  设备漫游换 IP 后，新地址要重新过审批。
- 表现：首次 probe 超时或被拦，批过一次之后就通；换了地址要再批一次。
- 对策：优先用 tailnet 域名（MagicDNS，如 `xxx.ts.net`）寻址 —— 域名不变
  就不用反复审批；IP 只作缓存。

## 主控优先用 tailnet 地址

- 主控解析目标时优先用 tailnet 地址；局域网地址（192.168.x.x / 172.16.x.x）
  沙盒够不着 —— agent 自报局域网地址时必须改走 tailnet 地址才通。
- agent 侧（v4.5 起）：`self_url()` 的优先级是
  配置 `self_url` > tailnet IP > 隧道地址 > 局域网 IP；
  对外自报地址不再是局域网地址。

## 地址漂移

- 没配地址目录时，tailnet IP 漂移仍需手工重登记（`fleet.py add` 改地址）
  或配地址目录（`fleet.py directory` + 副端安装时 `--directory`），
  让副端上报新地址、主端自动跟随。
- 主控探活失败时会重读一次登记（捡起用户刚手工改过的地址）再报失败，
  但跨进程的漂移仍要靠目录或手工重登记。
- 另见：地址目录机制（`fleet-directory.py` + `sync_from_directory` +
  `AUTO_SYNC`，可用 `--no-sync` / `FLEET_NO_SYNC=1` 关掉）。

## 不能设 exit node

- 沙盒的 Tailscale 是受限隧道代理，不是真 tailscaled：不能设 exit node，
  不能指望沙盒替你转发整个 tailnet 的流量；它只解决“主控够得到 tailnet
  上的 agent 端口”这一个问题。
