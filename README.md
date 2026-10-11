# Claude 分流规则（Loon / Clash）

每天核对 Claude / Anthropic 官方及上游域名，供 Loon 与 Clash (Meta / Verge / Premium) 将相关连接交给专属代理策略。只维护域名规则，不修改 DNS，不包含节点、代理订阅或证书。

[![更新状态](https://img.shields.io/badge/maintained%20by-Hermes%20Agent-blue)](https://github.com/J606y/claude-rules)

## 订阅方式

### 1. Loon 订阅
规则地址：
```text
https://raw.githubusercontent.com/J606y/claude-rules/main/Loon/Claude.list
```
在 Loon 的 `[Remote Rule]` 中添加下列一行（`AI` 必须是你已有的策略组名称）：
```ini
https://raw.githubusercontent.com/J606y/claude-rules/main/Loon/Claude.list, policy=AI, tag=Claude, enabled=true
```

### 2. Clash 订阅（Rule-set / Classical）
规则地址：
```text
https://raw.githubusercontent.com/J606y/claude-rules/main/Clash/Claude.yaml
```
在 Clash 配置文件中的 `rule-providers` 与 `rules` 添加（`AI` 必须是你已有的策略组名称）：
```yaml
rule-providers:
  Claude:
    type: http
    behavior: classical
    url: "https://raw.githubusercontent.com/J606y/claude-rules/main/Clash/Claude.yaml"
    path: ./ruleset/Claude.yaml
    interval: 21600

rules:
  - RULE-SET,Claude,AI
```

用对应条目替换旧的 Claude 远程规则即可，不需要替换整份配置。将 Claude 专用规则放在可能重叠的通用订阅规则之前，并开启 Loon 或 Clash 的远程规则自动更新。

## 能解决什么

覆盖 Claude 专属域名及指定的专用 CDN 主机，让命中的连接统一走 `AI`。`anthropic.com`、`claude.ai`、`claude.com`、`claudeusercontent.com` 等后缀规则也覆盖其子域名。

规则不会替你选择节点，也不会验证节点的出口国家。若目标是让 Claude 看到非中国 IP，必须给 `AI` 选择实际非中国出口，并通过客户端请求记录确认命中的最终节点；不能仅凭节点名称判断国家。

未列入清单的第三方登录、验证、遥测和自定义连接器仍按原配置分流。本仓库不承诺“绝对零侧漏”，也不会为了扩大覆盖而收录整个 Google、Cloudflare、AWS 或共享 CDN 根域。

## 维护机制

由部署在服务器的 Hermes Agent 守护进程每 6 小时自动从互联网上游（v2fly、MetaCubeX）抓取并严格交叉比对：
- 双源一致且验证通过后，才接纳新增条目；
- 失败时自动保留上一版有效规则，绝不丢弃已有规则；
- 有变动时自动提交推送至 GitHub 仓库。

## 数据来源与边界

人工基线：`data/seed.list`。

上游：

- [v2fly/domain-list-community: anthropic](https://github.com/v2fly/domain-list-community/blob/master/data/anthropic)
- [MetaCubeX/meta-rules-dat: anthropic](https://github.com/MetaCubeX/meta-rules-dat/blob/meta/geo/geosite/classical/anthropic.yaml)

这两份数据存在上游关联，一致性校验不代表两家独立权威认证，也不能自动发现上游尚未记录的新域名。域名用途可交叉参考 [Claude 官方网络文档](https://code.claude.com/docs/en/network-config)。本项目不是 Anthropic 官方规则库。

源数据遵循各自仓库许可证。仓库中的原创维护代码以 MIT 许可证提供。不会从私人配置或未经验证的随机域名清单自动吸收规则。

## 本地验证

仅使用 Python 标准库，建议 Python 3.13：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/update.py
```

`Loon/Claude.list` 与 `Clash/Claude.yaml` 是可订阅产物，`metadata.json` 保存生成摘要和来源证据。网络失败时更新命令返回非零状态；离线测试不依赖互联网。

## 回滚与人工清理

如果某次新增规则有问题，通过 GitHub 的提交记录回滚对应变更。发布文件都是普通文本，每次更新均有独立提交，可以对比和恢复。

保留规则是为了防止上游短暂缺项造成漏网，不代表域名永久可信。确认域名已失效或易主后，应人工检查并清理相应的基线与已发布规则，解决上游差异后再恢复自动维护。

紧急情况下可将订阅 URL 中的 `main` 换成已验证的历史提交 SHA，固定到该版本；恢复正常后再改回 `main`。
