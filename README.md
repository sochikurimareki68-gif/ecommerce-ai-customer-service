# 电商 AI 智能客服系统

> 基于 Coze 平台搭建的电商智能客服系统，支持商品咨询、尺码推荐、退换货、物流查询等场景。

## 功能

- **智能问答**：基于知识库回答商品材质、尺寸、洗护、发货、退换货等 FAQ
- **多轮对话**：支持上下文追问（如"那红色款有货吗？"），配置会话记忆
- **意图识别 + 转人工**：复杂问题/投诉自动识别并引导转人工
- **安全兜底**：敏感问题过滤，无法回答时礼貌引导联系人工客服

## 技术栈

- 平台：扣子 Coze
- 知识库：RAG 检索增强
- 工作流：意图识别 → 知识库检索 → 多轮对话 → 兜底转人工
- 前端：HTML / CSS / JavaScript
- 部署：Github Pages

## Demo

- 在线体验：[点击访问](https://用户名.github.io/ecommerce-ai-customer-service)
- 演示视频：[B站链接](替换为实际链接)

## 项目结构

```
ecommerce-ai-customer-service/
├── index.html              # 前端聊天页面
├── docs/
│   ├── prompt-template.md       # 人设 Prompt 模板
│   ├── faq-knowledge-base.md   # FAQ 知识库（50条）
│   ├── workflow-design.md      # 工作流设计说明
│   ├── test-cases.md           # 测试用例记录
│   └── evaluation.md           # 评测数据和结果
└── README.md
```

## 实施步骤

1. 在 Coze 创建 Agent，配置人设 Prompt
2. 整理 50 条电商客服 FAQ，上传知识库
3. 设计工作流：意图识别 → 知识库检索 → 多轮对话 → 兜底转人工
4. 配置变量与会话记忆，设置敏感词过滤
5. 测试 20+ 组对话，记录 BadCase 并优化
6. 编写 HTML 前端页面，接入 Coze API
7. 部署到 Github Pages

## 作者

许民德 — AI 应用工程师
