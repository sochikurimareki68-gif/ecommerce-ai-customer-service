# Keepalive 外部探活接入（G2）

> 适用：Render 免费实例（闲置一段时间自动休眠）。休眠后首次访问要等约 50 秒，
> 面试官点开 Demo 会以为服务挂了。
>
> **为什么不能只靠 GitHub Actions**：低活跃仓库的定时任务会被 GitHub 降频到 3.5–7 小时
> 一次，`cron: "*/5 * * * *"` 写了也不会按 5 分钟跑。必须用外部探活服务作为主方案。

## 一、探活地址

| 用途 | URL | 说明 |
|------|-----|------|
| 探活目标 | `https://youmian-customer-service.onrender.com/` | **打首页，不要打 `/api/health`** |
| 验证在线 | `https://youmian-customer-service.onrender.com/api/health` | 看 `uptime`（隔几分钟两次采样递增 = 没被休眠）/ `keepalive_ok`（最近 20 分钟有无真实流量） |

为什么探首页：`/api/health` 被特意排除在心跳统计之外，否则探针自己会把
`seconds_since_activity` 永远刷成 0，keepalive 失效也看不出来。首页是静态资源，
`do_GET` 会调用 `mark_activity()`，成本几乎为零。

## 二、cron-job.org（推荐，免费，支持 1 分钟）

1. 打开 https://cron-job.org ，注册并登录。
2. 控制台 → **Create cronjob**。
3. 填写：
   - **Title**：`youmian keepalive`
   - **URL**：`https://youmian-customer-service.onrender.com/`
   - **Schedule**：Every 5 minutes（`*/5 * * * *`）
   - **Request method**：GET
   - **Timeout**：30 秒（冷启动首包会慢，给足时间）
   - **Enable job**：勾选
4. 保存后等 5–10 分钟，打开 `/api/health` 确认：
   - `uptime` 隔几分钟连续采样是递增的（递增 = 进程一直活着，没被休眠重建）
   - `keepalive_ok` 为 `true`
   - `seconds_since_activity` 小于 300 左右
   - `last_activity_at` 在最近 5 分钟内
5. 在 cron-job.org 的 **History** 里确认最近几次执行是 200 而不是超时。

## 三、UptimeRobot（备选，免费版 5 分钟）

1. 打开 https://uptimerobot.com ，注册并登录。
2. **Add New Monitor**：
   - **Monitor Type**：HTTP(s)
   - **Friendly Name**：`youmian keepalive`
   - **URL**：`https://youmian-customer-service.onrender.com/`
   - **Monitoring Interval**：5 minutes
   - **Alert Contacts**：填自己的邮箱（服务挂了也能收到通知）
3. 保存后同样用 `/api/health` 验证：`uptime` 两次采样递增，且 `keepalive_ok` 为 `true`。
4. 免费版最小间隔 5 分钟，刚好小于 Render 的休眠阈值。

## 四、Render 侧确认

- 免费实例不需要额外配置；探活流量本身就是「唤醒 + 保持」。
- 可在 Render → 服务 → Settings 确认实例类型仍是 Free；Free 实例无法关闭休眠，只能靠探活。
- 部署后第一次访问仍可能慢一次，属正常；之后由外部探活维持。

## 五、失效判定与排查

`/api/health` 返回（节选）：

```json
{
  "uptime": 3600.0,
  "last_activity_at": "2026-11-09T08:05:00Z",
  "seconds_since_activity": 42.3,
  "keepalive_window_s": 1200,
  "keepalive_ok": true
}
```

- 判读顺序：先看 `uptime` —— 隔几分钟两次采样若归零重算，说明实例刚被唤醒（探活没兜住）；
  `uptime` 持续增长后再看 `keepalive_ok`。
- `keepalive_ok: false` 且 `seconds_since_activity` 持续增大 → 探活没生效，按顺序查：
  1. 探活任务是否被暂停 / 欠费；
  2. URL 是否误填成 `/api/health`（健康检查不计心跳）；
  3. Render 服务是否被暂停（Free 实例长期无流量会被 suspend）；
  4. 探针超时时间是否太短（冷启动首包可能 > 30 秒）。
- 窗口默认 20 分钟，可通过环境变量 `KEEPALIVE_MAX_GAP_S` 调整。
- 面试前 10 分钟手动打开一次首页预热，作为双保险。

## 六、GitHub Actions 定时任务的定位

`.github/workflows/keepalive.yml` 保留作兜底，不要当作主方案。它被降频时不会报错，
只会「看起来配置了但实际没跑」—— 这正是 G2 要防的事。

## 七、面试前 3 分钟检查清单

1. `curl -s https://youmian-customer-service.onrender.com/api/health` 看 `uptime`（隔 2 分钟再查一次，应递增）；
2. 浏览器打开首页，确认气泡/聊天能正常加载；
3. 发一条「有什么优惠活动」确认端到端可用；
4. 若刚睡醒，等 60 秒再刷新，不要连续猛刷触发限流。
