# Claude 分流规则（Loon / Clash）

每天核对 Claude / Anthropic 官方及上游域名，供 Loon 与 Clash (Meta / Verge / Premium) 将相关连接交给专属代理策略。只维护域名规则，不修改 DNS，不包含节点、代理订阅或证书。

[![每日维护](https://github.com/J606y/claude-rules/actions/workflows/update.yml/badge.svg)](https://github.com/J606y/claude-rules/actions/workflows/update.yml)

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
    interval: 86400

rules:
  - RULE-SET,Claude,AI
```

用本条替换旧的 Claude 远程规则即可，不需要替换整份配置。将 Claude 专用规则放在可能重叠的通用订阅规则之前，并开启 Loon 或 Clash 的远程规则自动更新。仓库更新不等于设备立即刷新，具体刷新间隔以你的客户端设置为准。

## 能解决什么

覆盖 Claude 专属域名及指定的专用 CDN 主机，让命中的连接统一走 `AI`。`anthropic.com`、`claude.ai`、`claude.com`、`claudeusercontent.com` 等后缀规则也覆盖其子域名。

规则不会替你选择节点，也不会验证节点的出口国家。若目标是让 Claude 看到非中国 IP，必须给 `AI` 选择实际非中国出口，并通过客户端请求记录确认命中的最终节点；不能仅凭节点名称判断国家。

未列入清单的第三方登录、验证、遥测和自定义连接器仍按原配置分流。本仓库不承诺“绝对零侧漏”，也不会为了扩大覆盖而收录整个 Google、Cloudflare、AWS 或共享 CDN 根域。需要补充第三方主机时，应先依据实际请求记录确认归属。

## 每日维护

GitHub Actions 每天北京时间 **08:17**（UTC 00:17）计划运行，也支持在 Actions 页面手动执行 `Run workflow`。GitHub 的定时调度可能延迟，不是精确计时服务。

维护流程：

1. 先运行离线边界测试。
2. 拉取两个来源，验证格式、大小、域名和数量；两份集合一致才接纳新增条目。
3. 合并人工基线、已发布规则及有效上游，排序去重；不自动删除既有条目。
4. 校验通过后提交规则和来源摘要；每天另写 `maintenance.json`，记录真实检查时间、规则摘要及 Actions 运行链接。

任一来源下载失败、内容为空、返回 HTML、格式异常、超出限制或双方不一致，本次任务失败，不覆盖仓库里上一版规则。失败详情在 Actions 日志中查看；通知是否送达取决于你的 GitHub 通知设置。

规则文件不带每日变动的时间戳。即使域名未变，每次成功检查仍会更新独立的维护记录，提供可观察的运行证据，也避免仓库长期无活动。`maintenance.json` 记录的是成功校验时间；是否发布成功还应以其链接对应的 Actions 运行结果为准。

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

如果某次新增规则有问题，先在 Actions 页面暂停该工作流，再通过 GitHub 的提交记录回滚对应变更。发布文件都是普通文本，每次更新均有独立提交，可以对比和恢复。

保留规则是为了防止上游短暂缺项造成漏网，不代表域名永久可信。确认域名已失效或易主后，应人工检查并清理相应的基线与已发布规则，解决上游差异后再恢复自动维护。不要仅回滚生成文件后立刻恢复计划任务，否则仍在上游的条目可能再次被加入。

紧急情况下可将订阅 URL 中的 `main` 换成已验证的历史提交 SHA，固定到该版本；恢复正常后再改回 `main`。
