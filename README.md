# 公共交通中断改道发布服务

一个仅使用 Python 标准库实现的线路、站点、班次、施工绕行和无障碍变化发布服务。方案按草稿、复核、批准、发布流转；路径计算会应用停运、跳站、绕行和无障碍限制。

## 运行

```bash
python app.py --init
python app.py --port 8010
```

打开 <http://127.0.0.1:8010>。`--init` 会导入两条示例线路、六个站点和一个 23:50 发车的跨日班次。数据库默认是 `transit_disruption.db`，可用 `--db` 或 `TRANSIT_DB` 修改。

## 业务能力

- 基础数据导入会一次性检查线路、站点经纬度、连续站序、重复站点、站间行驶时间和班次时间。错误批次写入 `import_errors` 后整体拒绝，不留下半批数据。
- 中断事件可以包含 `stop_closure`、`skip_stop`、`detour`、`accessibility_change`，可以设置服务日分钟窗口。
- 路径使用 Dijkstra 算法比较基线与方案版本；跳站时车辆可继续通过，但乘客不能在跳站上下车，经过省略路段的行驶时间会计入下一段。
- 班次时间以服务日零点起算，允许超过 1440 分钟。例如 1430 分发车、21 分钟到达会显示为次日 `00:21`。
- 修改只允许发生在草稿版本；创建新版本会复制父版本变更，已发布快照继续保留。
- 发布在一个 SQLite 事务内写入方案快照和 SHA-256，旧发布版本不会被覆盖。
- 乘客影响通知台：乘客可登记起终点、常用服务时间窗和"只能走无障碍路线"；选定草稿版本后按基线与方案路线逐条比对，原走法断掉、绕行多出 15 分钟及以上或无障碍条件不满足的乘客进入待通知名单，并给出替代路线，接不上则保留原因。名单经调度员确认后方案才能发布；发布时名单随版本冻结成快照，之后订阅或基础数据再改都不影响，新版本必须重新评估。

## 模块分层

订阅资料、影响判断、保存与页面分开整理：

- `subscriptions.py`：乘客与常用行程订阅的登记、校验、停用（`passengers`、`subscriptions` 表）。
- `impacts.py`：纯逻辑的影响判断，不读写数据库；比对基线/方案路线，判定 `broken`、`detour_delay`（≥15 分钟）、`accessibility`，并把替代路线翻译成可展示的站链。
- `notifications.py`：待通知名单的生成（覆盖草稿旧结果）、调度员确认、发布事务内冻结快照（`notification_lists`、`notification_entries` 表）。
- `app.py`：HTTP 与版本流转接线；`static/index.html` 是方案发布台，`static/notifications.html` 是独立的乘客影响通知台页面。

## API

使用 `X-User`、`X-Role` 身份头，角色包括 `planner`、`editor`、`reviewer`、`admin`。

- `POST /api/import`：导入基础数据。
- `POST /api/disruptions`：创建中断事件及第一版草稿。
- `POST /api/disruptions/{id}/versions`：从指定父版本复制出新草稿。
- `POST /api/versions/{id}/changes`：向草稿添加停运、跳站、绕行或无障碍变化。
- `POST /api/versions/{id}/submit|approve|reject|publish`：完成复核发布流程。
- `GET /api/route?from=1&to=5&version_id=1&at_minute=1430&accessible=true`：查询路径、耗时和到达时间。
- `GET /api/trips/{id}`：查看跨日班次各站时间。
- `GET /api/import-errors`：查看被隔离的错误批次。
- `GET/POST /api/subscriptions`：查看生效订阅、登记乘客常用行程（`origin_stop_id`、`destination_stop_id`、`service_start_minute`、`service_end_minute`、`require_accessible`）；`POST /api/subscriptions/{id}/deactivate` 停用。
- `POST /api/versions/{id}/notifications/evaluate`：草稿版本重新评估，生成/覆盖待通知名单。
- `POST /api/versions/{id}/notifications/confirm`：调度员确认名单；未经确认的版本发布会被拒绝（名单为空也需确认）。
- `GET /api/versions/{id}/notifications`：未发布返回实时名单，已发布返回冻结快照。
- 通知台页面：<http://127.0.0.1:8010/notifications>。

### 影响判定规则

只比对生效订阅：乘客服务时间窗与变更生效时间窗重叠（或变更全天生效），比对时刻取重叠窗口的最早开始分钟，否则取乘客服务窗起点。

- `broken`：方案下普通路线也不可达（如起终点被关），不提供替代路线、保留原因；
- `detour_delay`：方案仍可达但最短耗时比基线多 15 分钟及以上，方案最短路径即替代路线；
- `accessibility`：乘客只能走无障碍路线而方案下无障碍路径不存在，但普通路线仍可达时，给非无障碍替代；普通路线也接不上则归入 `broken`。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖基线/改道路径、版本复制与发布隔离、审批冲突、无障碍路径、跨日时刻和坏数据整批隔离，以及订阅校验、三类影响判定、时间窗重叠、发布前确认门控、发布冻结与新版本重算。
