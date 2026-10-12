# 电商 AI 智能客服系统 · 优眠小助手

> 基于扣子 Coze 搭建的电商智能客服智能体，通过**六路条件判断对话流**按用户意图路由，结合 FAQ 知识库与商品目录，实现商品咨询、洗护指南、物流发货、退换售后、优惠活动等场景的自动应答。

## 项目链接

- **在线体验（可真实对话）**：[youmian-customer-service.onrender.com](https://youmian-customer-service.onrender.com)
  - 部署在 Render 免费实例上，闲置一段时间后首次访问需等待约 50 秒唤醒
- **源码仓库**：[ecommerce-ai-customer-service](https://github.com/sochikurimareki68-gif/ecommerce-ai-customer-service)
- **静态演示**：[GitHub Pages](https://sochikurimareki68-gif.github.io/ecommerce-ai-customer-service)（纯前端，展示界面与交互，无需后端）
- 演示视频：待补充

> 智能体运行在扣子 Coze 平台，需在扣子账号内搭建使用。本仓库提供完整的人设 Prompt、知识库、对话流设计、评测数据集，可按文档完整复现。

## 功能

- **六路意图路由**：选择器节点自带 LLM 意图识别，自动分类并路由到对应分支
- **知识库问答（RAG）**：基于 FAQ 知识库（50条）+ 商品目录（20款）检索回答
- **原生多轮对话**：对话流绑定会话，模型节点可读取历史消息，多轮追问无需手工补全上下文
- **分级消息处理**：问候语/转人工/结束语直接回复，业务咨询走对话流，异常走安全兜底
- **安全兜底**：无法回答时礼貌引导转人工，不编造答案、不冒充人工客服
- **可在线试用**：经后端代理与真实智能体流式对话，前端不暴露任何密钥
- **可量化评测**：附带 30 条用例的自动化评测脚本，意图路由准确率 96.7%

## 对话流架构

```
用户输入（USER_INPUT / CONVERSATION_NAME）
   │
   ▼
意图识别（选择器节点，自带 LLM，支持读会话历史）
   │
   ├─ ① 商品咨询 ──→ LLM 节点（技能：FAQ + 商品目录）─→ output
   ├─ ② 洗护指南 ──→ LLM 节点（技能：FAQ + 商品目录）─→ output1
   ├─ ③ 物流发货 ──→ LLM 节点（技能：FAQ + 商品目录）─→ output2
   ├─ ④ 退换售后 ──→ LLM 节点（技能：FAQ + 商品目录）─→ output3
   ├─ ⑤ 优惠活动 ──→ LLM 节点（技能：商品目录 + FAQ）─→ output4
   └─ ⑥ 其他兜底 ──→ LLM 节点（无技能）──────────────→ output5
   │
   ▼
代码节点（合并六路输出为单一 reply）
   │
   ▼
结束节点返回 reply
```

**为什么用对话流**：客服是典型多轮场景，对话流原生绑定会话（可读历史消息）、模型节点支持对话历史，且发布渠道更广（API&SDK、小程序、社交渠道等）。工作流与对话流可一键互转。

**智能体接入方式**：采用「单 Agent（对话流模式）」，每轮对话直接执行绑定的对话流，省去规划层，响应更快；结束节点使用「返回文本」+ `{{reply}}`，保证用户看到的是纯文本而非结构化 JSON。

## 技术栈

- 平台：扣子 Coze（www.coze.cn）
- 对话流：选择器节点（意图识别）+ 大模型节点 + 代码节点（Python）
- 知识库：RAG 检索增强（文本知识库，按需调用）
- 模型：豆包 2.0 pro
- 前端：HTML / CSS / JavaScript
- 后端：Python 3 标准库（`http.server` + `urllib`，零第三方依赖）
- 部署：GitHub Pages（静态演示）+ 任意 Python 托管平台（真实对话）

## 项目结构

```
ecommerce-ai-customer-service/
├── index.html                  # 前端聊天页面（有后端时真实对话，无后端时降级为界面演示）
├── server.py                   # 轻量后端代理（Python 标准库，零依赖）
├── render.yaml                 # Render 部署蓝图
├── Procfile                    # 通用启动命令（Render / Railway / Heroku 系）
├── requirements.txt            # 声明无第三方依赖
├── .env.example                # 环境变量示例（复制为 .env 并填写令牌）
├── eval/
│   ├── dataset.json            # 评测数据集（30 条用例 + 分支关键词）
│   ├── run_eval.py             # 自动化评测脚本（零依赖，可并发）
│   └── report.md               # 评测报告（自动生成）
├── docs/
│   ├── prompt-template.md      # 人设 Prompt 模板（7个模块）
│   ├── faq-knowledge-base.md   # FAQ 知识库（50条，6大类）
│   ├── product-catalog.md      # 商品目录与价格表（20款）
│   ├── chatflow-design.md      # 六路条件判断对话流设计
│   ├── test-cases.md           # 测试用例设计（25组，执行结果见 eval/）
│   └── integration-plan-v2.md  # v2.0 对接真实店铺方案（物流/人工/知识库同步）
└── README.md
```

## 本地运行（真实对话）

前端不保存任何令牌；令牌只存在服务端 `.env` 里，由后端代理转发请求。

```bash
# 1. 配置令牌（pat_ 开头，需勾选 Chat 权限）
cp .env.example .env        # 然后编辑 .env，填入 COZE_API_TOKEN

# 2. 启动服务（零第三方依赖，只用 Python 标准库）
python3 server.py

# 3. 打开页面
open http://localhost:3000
```

`server.py` 同时承担两件事：托管静态页面 + 提供 `POST /api/chat` 流式转发扣子接口。
页面加载时会探测 `/api/health`——探测成功走真实对话，失败（例如部署在 GitHub Pages）则自动降级为内置模拟回复的界面演示。

### 为什么需要后端代理

扣子 API 调用需要携带个人访问令牌（PAT）。如果把令牌写进网页源码，任何访客打开开发者工具都能抄走，
等于把账号交出去。所以令牌只放在服务端，浏览器只与本服务通信。

> 想让别人在线试用，把本仓库部署到任意支持 Python 的托管平台（如 Render / Railway / Fly.io），
> 在平台的环境变量中配置 `COZE_API_TOKEN` 即可。GitHub Pages 只能托管静态文件，跑不了后端。

## 部署到公网（让别人能访问）

GitHub Pages 只能托管静态文件，跑不了后端，所以**真实对话必须部署到支持 Python 的平台**。
下面以 Render 为例（免费、步骤最少，仓库里已经放好 `render.yaml`）。

### 方式一：Render（推荐）

1. 把本仓库推送到 GitHub（`.env` 已被 `.gitignore` 忽略，不会被提交）
2. 打开 [dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint**
3. 选择本仓库，Render 会自动读取根目录的 `render.yaml`
4. 在弹出的环境变量处填写 `COZE_API_TOKEN`（`pat_` 开头，需勾选 Chat 权限）
5. 点 **Apply**，等待首次构建完成（约 2-3 分钟）
6. 得到形如 `https://youmian-customer-service.onrender.com` 的地址——把它发给别人即可直接对话

注意事项：

- **自动部署**：仓库已连接 Render 的 GitHub App，向 `main` 分支推送后会自动构建并部署（约 40–60 秒）
- **免费版会休眠**：15 分钟无访问后实例休眠，下次访问需要约 1 分钟唤醒。面试前先自己打开预热一次
- **区域**：`render.yaml` 里设了 `singapore`（离国内最近）。若提示区域不可用，删掉那一行用默认区域即可
- **令牌安全**：只填在 Render 控制台的 Environment 里，永远不要写进代码或提交到仓库

### 方式二：其他平台

本项目没有第三方依赖、启动命令固定，任何能跑 Python 的平台都能部署：

| 平台 | 启动命令 | 必须配置 |
|------|---------|---------|
| Render | `python server.py` | `COZE_API_TOKEN` |
| Railway | 同左（已提供 `Procfile`，可自动识别） | `COZE_API_TOKEN` |
| Fly.io | 同左 | `COZE_API_TOKEN` |
| 阿里云函数计算 / 腾讯云 CloudBase | 同左 | `COZE_API_TOKEN` |

通用要求只有三条：

1. 启动命令 `python server.py`
2. 监听端口读环境变量 `PORT`（`server.py` 已支持），绑定 `0.0.0.0`（默认已是）
3. 环境变量里配置 `COZE_API_TOKEN`，可选 `COZE_BOT_ID` / `COZE_API_BASE`

### 部署后自检

打开 `https://你的域名/api/health`，应返回：

```json
{"ok": true, "bot_id": "7694865098007035940", "api_base": "https://api.coze.cn", "token_ready": true}
```

`token_ready` 为 `false` 说明环境变量没配上；`ok` 都拿不到说明服务没起来。

### GitHub Pages 静态演示仍然有效

部署后端**不影响**原来的 GitHub Pages 页面——同一个 `index.html` 会先探测 `/api/health`：
探到后端就走真实对话，探不到就自动降级为内置模拟回复的界面演示。所以两处可以同时存在。

## 效果评测（v1.3）

用 `eval/` 下的数据集与脚本对智能体做自动化回归，量化意图路由与回复质量。

**最新一次评测**（30 条用例 · 2026-10-12）：

| 指标 | 结果 | 目标 |
|------|------|------|
| 意图路由准确率 | **96.7%**（29/30） | ≥ 90% |
| 关键信息覆盖率 | 100%（30/30） | ≥ 95% |
| 回复规范性 | 100%（30/30，无冒充人工 / 无 XML 标签 / 无代码块） | 100% |
| 平均响应延迟 | 20.2s（P95 30.0s） | — |

五条业务分支（商品咨询 / 洗护指南 / 物流发货 / 退换售后 / 优惠活动）路由准确率均为 **100%**。

唯一未达标的一条在「其他兜底」分支：输入「能便宜点吗？」时，设计文档预期走兜底，实际被分类器判为
**优惠活动** —— 从业务角度看这个判定更合理（用户问的就是价格优惠），回复也正确给出了「不议价 + 满减/领券」信息。
这一条已作为「设计预期与实际实现不一致」记录在评测报告中。

**运行方式**：

```bash
python3 server.py            # 终端 1：启动代理
python3 eval/run_eval.py     # 终端 2：跑全部用例，结果写入 eval/report.md
```

支持 `--limit N`（只跑前 N 条）、`--workers N`（并发数）、`--base URL`（指向线上地址）三个参数。

> 说明：扣子对话流的分类结果不对外暴露，因此「实际分支」由**回复文本中的分支关键词**反推，
> 并按关键词长度加权（越长的短语越具体）。这是**代理指标**而非真实标签，报告里如实标注了这一局限。

## 实施步骤

1. 在扣子创建智能体，配置人设 Prompt（角色/语气/处理规则/禁答范围等）
2. 整理 50 条电商客服 FAQ + 20 款商品目录，上传至知识库
3. 创建六路条件判断对话流：意图识别 → 六条分支 → 代码合并 → 返回
4. 将对话流绑定到智能体，配置消息处理规则与开场白
5. 测试 25 组对话用例，覆盖六大意图，记录 BadCase 并优化
6. 编写 HTML 前端页面，并在页面内提供源码入口
7. 用 Python 标准库实现后端代理（`server.py`），令牌只留在服务端
8. 部署：静态页面发到 GitHub Pages；需要真实对话则部署到任意支持 Python 的托管平台

## 后续规划（Roadmap）

- **v2.0 对接真实店铺**：新增扣子自定义插件，实现物流实时查询、真实人工客服、商品知识库自动同步。详细方案见 [对接方案文档](docs/integration-plan-v2.md)
- **v1.4 真实标签评测**：在对话流结束节点额外输出分类结果，把「关键词反推」升级为真实标签，让路由指标更精确

### 已完成的关键迭代

- **v1.3 效果评测**：新增 `eval/` 数据集与自动化回归脚本，30 条用例量化意图路由（96.7%）、关键信息覆盖（100%）与响应延迟
- **v1.2 后端代理**：新增 `server.py`，令牌由服务端持有并流式转发扣子接口，前端不再接触密钥，公开网页也能安全对话
- **v1.1 由工作流迁移为对话流**：获得原生多轮记忆，删除人设中冗余的"上下文补全"规则（人设由 11 模块精简为 7 模块）

## 作者

[sochikurimareki68-gif](https://github.com/sochikurimareki68-gif)

## 许可

MIT License，详见 [LICENSE](LICENSE)。

