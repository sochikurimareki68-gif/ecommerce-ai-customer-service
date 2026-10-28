# 上线检查清单 + 应急 Runbook

> 每次改动对话流、提示词、知识库或后端代码后，发布前照着走一遍。
> 目的：避免「改了提示词但没发布」「工作流发布了、智能体没发布」这类问题在演示当天才暴露。
>
> 第一节到第四节是**发布流程**；第五到第七节是**出问题时的应急手册**（面试被问「线上挂了怎么办」，答这部分）。

---

## 一、10 项清单（发布前逐条勾选）

| # | 检查项 | 怎么验证 | 不通过会怎样 |
|---|--------|---------|-------------|
| 1 | **对话流已发布** | 扣子 → 资源库 → `customer_service_workflow` → 版本记录里出现新版本号（如 v0.0.12），且状态为已发布 | 画布上改了，线上仍跑旧逻辑 |
| 2 | **智能体已发布** | 智能体编排页右上角「发布」→ 版本记录有新版本；否则线上仍调用上一版对话流 | 最常见的坑：工作流发了，智能体没发，改动完全不生效 |
| 3 | **API 渠道已勾选** | 发布弹窗里 `API` 渠道勾选，且权限含 `Chat`（对话） | 后端调 `/v3/chat` 返回 4xx，页面报「无法连接」 |
| 4 | **令牌有效** | `GET /api/health` 返回 `token_ready: true` 且 `version` 为本次版本；再在页面上真发一句「你好」拿到真实回复 | 只报 `token_ready` 不够——令牌过期时它仍是 true；`version` 没变说明部署没生效 |
| 5 | **单轮评测通过** | `python3 eval/run_eval.py --base <线上地址>` → 关键信息覆盖率 100%，规范性 100% | 回答质量回退没被发现 |
| 6 | **多轮评测通过** | `python3 eval/run_eval.py --multi --base <线上地址>` → 上下文正确率 100% | 会话历史开关被关掉、或节点改动导致上下文断裂 |
| 7 | **额度充足** | 当日已用请求数 < 单 IP 60 / 全站 500；评测前先看 `eval/report.md` 的生成时间，避免当天重复全量跑 | 评测跑到一半被 429 截断，指标失真 |
| 8 | **密钥与配置未进仓库** | `git status` 无 `.env`；`git log -S "pat_" --oneline` 无命中；`.env.example` 只有占位符 | 令牌泄漏，等于把扣子账号交出去 |
| 9 | **版本号与文档已更新** | `server.py` 的 `server_version` 递增；README 的评测表、`项目一-后续规划表` 状态同步 | 面试被问「这版做了什么」时对不上 |
| 10 | **发布说明与回滚点已写** | 一句话写清「改了什么 / 验证结果 / 怎么回滚」；扣子侧记下上一版版本号 | 出问题只能凭记忆回退，容易越修越乱 |

---

## 二、发布后 3 分钟自检

```bash
# 1. 服务活着、令牌就绪
curl -s https://youmian-customer-service.onrender.com/api/health

# 2. 首页可访问（应含「清空对话」按钮文案）
curl -s https://youmian-customer-service.onrender.com/ | grep -c "清空对话"

# 3. 会话重置可用（应返回 ok + epoch）
curl -s -X POST https://youmian-customer-service.onrender.com/api/reset

# 4. 单条真实对话（SSE 应出现 delta 事件）
curl -sN -X POST https://youmian-customer-service.onrender.com/api/chat \
  -H "Content-Type: application/json" -d '{"message":"你好"}'
```

四条都正常，再打开页面手动问一句业务问题（如「1.8 米的四件套多少钱」），确认回答与知识库口径一致。

> 免费实例休眠时，第一条请求会等 30–60 秒才返回，这是正常的，不要当成故障。

---

## 三、常见事故对照表

| 现象 | 最可能的原因 | 处理 |
|------|-------------|------|
| 改了提示词，线上回答没变 | 对话流或智能体没发布（第 1、2 项） | 两侧都发布后重试 |
| 回答开始「记不住上文」 | 某个节点「会话历史」开关被关 | 打开开关 → 重新发布工作流与智能体 |
| 页面提示「系统暂时无法连接」 | 后端休眠、令牌失效、或扣子接口异常 | 先看 `GET /api/health`；`token_ready: false` 查环境变量 |
| 回答突然开始编造认证 / 参数 | 提示词被改回旧版，或知识库里没有该条 | 对照 `docs/product-catalog.md` 与商品咨询节点口径 |
| 评测大面积 429 | 当天额度用完 | 次日再跑，或用 `--limit N` 只验证改动相关的几条 |
| 改动只在 API 生效、页面没变 | 前端缓存旧 `index.html` | 强刷（Cmd+Shift+R）确认 |
| 首字很快但整段一次性到齐 | 结束节点被改回「返回变量」 | 改回「返回文本 + `{{reply}}`」并开流式，见第七节 |
| 待接管队列是空的 | 服务重启过（内存队列） | 无需处理，属于当前轻量版的已知限制 |

---

## 四、发布记录

每次发布在下方追加一行，保留最近若干次即可：

| 日期 | 版本 | 改了什么 | 验证结果 | 回滚到 |
|------|------|---------|---------|--------|
| 2026-10-20 | v1.8 / 工作流 v0.0.11 | 清空会话、断线重连、健康接口限流 | 单测 39 条通过；线上 `/api/reset` 200 | v1.7.1 |
| 2026-10-25 | v1.13 / 工作流 v0.0.24 | v2.0 真流式：结束节点改返回文本、后端关键词推断分支 | 线上探针首字后收尾 0.51s | v1.12 |
| 2026-10-25 | v2.0.3 文档 | 全量回归 + 指标口径修正（分支归属降级为参考） | 覆盖 100%、规范 100%、事实 12/12、安全 6/6 | 上一版文档 |
| 2026-10-27 | 意图抽检 | 20 条人工抽检回填报告 | 意图正确率 20/20 = 100% | — |
| 2026-10-27 | I-3/I-4/I-5 | 挂件化、人工接管轻量版、二维码扫码 | 本地三页面联调通过 | 0544d12 |
| 2026-10-28 | I-2 探活 | GitHub Actions 每 5 分钟 curl /api/health 防免费实例休眠；/api/health 新增 uptime 字段；server_version 1.13→1.14 | 本地单测 45 条全绿；首次 workflow 触发后看 Actions 页确认 | 删除 `.github/workflows/keepalive.yml` 即可关闭 |

---

## 五、故障分级与响应

先定级，再动手。**不要在没定级的情况下乱改代码**——多数「越修越乱」都是跳过了定级。

| 级别 | 判定标准 | 用户感受 | 响应动作 | 目标恢复 |
|------|---------|---------|---------|---------|
| **P0** | 页面完全打不开 / 每次提问都报错 / 回答泄漏密钥 | 完全不可用 | 立即回滚（第七节），再做定位 | 5 分钟内恢复可用 |
| **P1** | 能对话但质量明显回退（编造参数、答非所问、记不住上文） | 可用但不可信 | 先回滚提示词/工作流版本，再复现定位 | 30 分钟内 |
| **P2** | 局部体验问题（某类问题答不好、首字偏慢、样式错位） | 基本可用 | 记录到 Badcase 台账，排期修 | 下个迭代 |
| **P3** | 内部可见问题（日志缺字段、报告数字格式） | 用户无感 | 顺手修 | 随时 |

**三条铁律**：

1. **先止血再定位** —— 能用回滚解决的，不要现场改代码。
2. **一次只改一个变量** —— 同时改提示词和 server.py，出了问题无法归因。
3. **每个动作留证据** —— 改了什么、验证结果贴进第四节发布记录。

---

## 六、场景处置手册

### 场景 1：页面打不开 / 一直转圈（P0 优先）

**判定顺序**（从外到内，逐层排除）：

```bash
# ① 服务进程活着吗
curl -s -o /dev/null -w "%{http_code}\n" https://youmian-customer-service.onrender.com/api/health

# ② 活着但令牌挂了？
curl -s https://youmian-customer-service.onrender.com/api/health
#    token_ready: false  → Render 环境变量 COZE_API_TOKEN 缺失或填错
#    version 不是最新        → 部署没生效，看 Render 的 deploy 日志

# ③ 令牌在但扣子侧不可用？
curl -sN -X POST https://youmian-customer-service.onrender.com/api/chat \
  -H "Content-Type: application/json" -d '{"message":"你好"}'
#    返回 error 事件 → 扣子令牌过期 / 智能体没发布 / API 渠道没勾
```

| 判定结果 | 处理 |
|---------|------|
| 返回 000 或超时 > 90s | 免费实例休眠，再等一次；仍不行 → Render 控制台看 Events 是否有崩溃 |
| `token_ready: false` | Render → Environment 重填 `COZE_API_TOKEN`，保存后自动重启 |
| `version` 是旧版 | 部署失败或 CI 没跑，看 GitHub Actions 的 `deploy` 任务日志 |
| 返回 429 | 不是故障，是限流（见场景 4） |
| 返回「系统暂时无法连接」 | 说明代理活着、扣子接口调用失败 → 查扣子侧（令牌/发布/额度） |

**升级处理**：以上都不成立，直接回滚到上一个 deploy（第七节 A）。

---

### 场景 2：回答质量明显回退（P1）

**典型症状**：编造认证参数、物流口径说错、多轮「那纯棉的呢」答成兜底、把 JSON 吐给用户。

**第一步：确认是不是「没发布」而不是「改坏了」**

- 扣子 → 资源库 → 工作流版本记录：最新版本号与画布内容是否一致？
- 智能体发布记录：是否发布了对应版本？
- 最常见的坑是**工作流发了、智能体没发**，线上仍跑旧逻辑。

**第二步：如果是改坏了，按改动类型回滚**

| 改了什么 | 回滚方式 |
|---------|---------|
| 提示词（商品咨询回复节点） | 版本记录里对比上一版提示词，粘回 → 发布工作流 → 发布智能体 |
| 知识库 | 扣子知识库重新上传上一版文档并重建索引 |
| server.py | `git revert <commit>` 或 `git checkout <旧commit> -- server.py` → push 触发部署 |

**第三步：回归验证（不跑全量，防止打满额度）**

```bash
# 每个分支抽 1 条，共 6 条
python3 eval/run_eval.py --limit 6 --base https://youmian-customer-service.onrender.com
# 事实口径单独验
python3 eval/run_eval.py --facts --base https://youmian-customer-service.onrender.com
```

只有用户明确说「全测」才跑 31 条全量。

---

### 场景 3：核心口径被改坏（P1，最容易出丑）

「事实口径速查」块是 v2.0 合并节点时唯一的防退化机制，**改提示词时最容易整块丢掉**。

必查的 8 条口径：

| 问题 | 正确口径 |
|------|---------|
| 多久发货 | 48 小时内（禁止说「当天发货」） |
| 包邮门槛 | 满 199 元；新疆/西藏/内蒙古需补差价 |
| 7 天无理由起算 | 自签收起算第 1 天 |
| 枕芯 | 贴身用品，**不支持** 7 天无理由 |
| 补偿 | 5 元补偿券 |
| 荧光剂 | 不含荧光剂 |
| 认证 | 仅 OEKO-TEX Standard 100、ISO 9001、浙江绍兴（知识库没有的一律不提） |
| 退款时效 | 1–3 个工作日原路退回 |

验证命令：`python3 eval/run_eval.py --facts`，必须 12/12。

---

### 场景 4：限流打满（P2，多为评测自己撞的）

**配额**：单 IP 每分钟 6 次、单 IP 每天 60 次、全站每天 500 次。`/api/health` 独立计数（30 次/分钟），不占提问额度。

| 症状 | 原因 | 处理 |
|------|------|------|
| 评测中途开始返回 429 | 每分钟限额被自己的连发撞了 | 已有 `SEC_GAP=12` 兜底安全用例；仍撞则重启服务清计数 |
| 全天大量 429 | 当天 60 次用完 | 次日再跑，或换网络出口（IP 变了计数独立） |
| 全站 500 打满 | 演示当天访问量过大 | 临时调高 `GLOBAL_PER_DAY`，或加白名单 |

**重启服务即可清空内存计数**（限流与 FAQ 缓存都在内存）：

```bash
pkill -f server.py && sleep 1 && python3 server.py
```

---

### 场景 5：流式失效（P1，演示时最明显）

**判定**：报告里「首字后收尾耗时」接近 0（如 0.02s）→ 说明整段一次性到达，不是真流式。

**三个已知原因**（按出现频率排序）：

| 原因 | 怎么确认 | 修法 |
|------|---------|------|
| 结束节点改回「返回变量」 | 输出被包成 `{"reply": ..., "branch": ...}`，代理必须收齐才能拆 | 改「返回文本 + `{{reply}}`」并开流式输出 |
| 代码节点又回来了 | 六路输出需要聚合，必然阻塞 | 保持单 LLM 节点，删掉聚合节点 |
| 智能体没发布 / API 渠道用旧版 | `version` 字段没变 | 重新发布工作流 + 智能体 |

验证：`python3 eval/run_eval.py --limit 3`，看报告第一节「首字后收尾耗时」是否 > 0.1s。

---

### 场景 6：部署没生效（P1）

本项目**不靠 Render 的 Auto-Deploy**，走 GitHub Actions：`main` 分支测试通过后调 Render Deploy Hook（Hook 存在 GitHub Secret `RENDER_DEPLOY_HOOK`）。

```bash
# 1. 看 CI 是否真的跑了
gh run list --limit 3          # 或直接在 GitHub 网页 Actions 页看

# 2. 看 CI 是否失败
gh run view <run-id> --log-failed

# 3. 看 Render 是否收到部署
#    Render 控制台 → Events，应有 deploy triggered
```

| 现象 | 原因 | 处理 |
|------|------|------|
| Actions 没触发 | push 没成功（网络/凭据） | `git log origin/main..HEAD` 确认是否真的推上去了 |
| Actions 绿但线上没变 | Deploy Hook Secret 失效 | Render 重新生成 Hook URL，更新 GitHub Secret |
| Actions 红 | 单测或 JSON 校验失败 | 按日志修，本地先跑 `python3 -m unittest discover -s tests -t . -v` |

---

### 场景 7：密钥相关（P0，最高优先级）

| 现象 | 说明 | 处理 |
|------|------|------|
| 前端源码里出现 `pat_` | 令牌被写进了前端 | 立即在扣子后台**吊销该令牌**，重新生成，只放 Render 环境变量 |
| 仓库里出现 `.env` | 被误提交 | 吊销令牌 → `git rm --cached .env` → 补 `.gitignore` → 强推前先问清影响 |
| 日志里出现明文令牌 | 打印了请求头 | 立即改日志脱敏，吊销令牌重发 |

**必查命令**：

```bash
git log -S "pat_" --oneline | head    # 历史里是否出现过令牌
git status --short | grep -i env     # 工作区是否有 .env
```

> 令牌一律由本人在平台侧填写，不进对话记录、不进代码、不进文档。

---

## 七、回滚 SOP

### A. 回滚后端（Render）

**方式一：控制台回滚（最快，1 分钟）**

1. Render → 服务 `youmian-customer-service` → **Deploys**
2. 找到上一个正常版本 → 右侧 `⋯` → **Rollback to this deploy**
3. 等 40–60 秒 → `curl .../api/health` 确认 `version` 已回到旧值

**方式二：代码回滚（需要改代码时）**

```bash
git log --oneline -10                          # 找到要回退的目标 commit
git revert <坏commit>                         # 生成反向提交，保留历史
git push origin main                          # CI 自动部署
```

> 优先用 `git revert` 而不是 `reset --hard`：历史可追溯，面试时能讲清「什么时候因为什么回滚」。

### B. 回滚扣子侧（工作流 / 提示词）

1. 扣子 → 资源库 → `customer_service_workflow` → **版本记录**
2. 选中上一个正常版本 → **回滚 / 重新发布**
3. **必须再发布一次智能体**（否则线上仍调用旧对话流）
4. 验证：页面发一句「多久发货」，答「48 小时内」即恢复

### C. 回滚顺序（同时坏了多处的场景）

```
① 先回滚后端（Render）—— 影响面最大，1 分钟见效
② 再回滚扣子工作流 + 智能体 —— 影响回答质量
③ 最后确认前端静态资源（强刷 Cmd+Shift+R）
```

**回滚后必做**：在第四节发布记录追加一行，写清「回滚原因 / 回滚到哪个版本 / 验证结果」。

---

## 八、诊断命令速查

```bash
# ---- 线上健康 ----
curl -s https://youmian-customer-service.onrender.com/api/health | python3 -m json.tool

# ---- 真实对话（看 SSE 事件流）----
curl -sN -X POST https://youmian-customer-service.onrender.com/api/chat \
  -H "Content-Type: application/json" -d '{"message":"多久能发货？"}'
#   期望：先 meta 事件，再逐个 delta，最后 done

# ---- 会话重置 ----
curl -s -X POST https://youmian-customer-service.onrender.com/api/reset

# ---- 待接管队列（I-4）----
curl -s https://youmian-customer-service.onrender.com/api/pending | python3 -m json.tool

# ---- 静态资源是否上线（I-3/I-5）----
curl -s -o /dev/null -w "widget-loader.js %{http_code}\n" https://youmian-customer-service.onrender.com/widget-loader.js
curl -s -o /dev/null -w "demo.html        %{http_code}\n" https://youmian-customer-service.onrender.com/demo.html
curl -s -o /dev/null -w "takeover.html    %{http_code}\n" https://youmian-customer-service.onrender.com/admin/takeover.html

# ---- 本地单测（44 条，不调扣子接口）----
python3 -m unittest discover -s tests -t . -v

# ---- 评测（按需选一，禁止无脑全量）----
python3 eval/run_eval.py --limit 6     # 每分支 1 条抽查
python3 eval/run_eval.py --facts       # 事实口径 12 条
python3 eval/run_eval.py --multi       # 多轮 5 组
python3 eval/run_eval.py --security    # 安全 6 条（自动 12s 间隔）
```

---

## 九、已知限制（面试主动说明，别等人追问）

| 限制 | 原因 | 后续方案 |
|------|------|---------|
| 免费实例 15 分钟休眠，冷启动 30–60s | Render 免费版策略 | I-2 定时探活（已暂停） |
| 待接管队列存在内存，重启即清空 | 轻量版无数据库 | 接 Redis / SQLite |
| 接管页无鉴权 | demo 阶段 | 加 token + 仅内网访问 |
| 接管后无法向用户回话 | 只读版 | 加 SSE 反向推送 |
| 分支归属一致率 83.9% 不作验收 | v2.0 无独立路由器，属循环论证 | 意图正确率改用人工抽检（20 条 100%） |
