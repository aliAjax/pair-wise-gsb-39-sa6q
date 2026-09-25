# 公共交通中断改道发布服务

一个仅使用 Python 标准库实现的线路、站点、班次、施工绕行和无障碍变化发布服务。方案按草稿、复核、批准、发布流转；路径计算会应用停运、跳站、绕行和无障碍限制。发布前必须在乘客影响通知台分析常用行程乘客、确认待通知名单；发布后名单随版本冻结。

## 模块划分

- `app.py`：基础数据、版本流转、Dijkstra 路径计算与 HTTP 入口。
- `domain.py`：领域异常、时间戳、规范化 JSON（供各模块共用，避免循环导入）。
- `subscriptions.py`：乘客订阅资料（起终点、常用服务时间窗口、是否只能走无障碍路线）。
- `impact.py`：影响判断。选定草稿版本后按基线路线和方案路线逐条比对。
- `notifications.py`：待通知名单的保存、调度员确认与发布冻结。
- `static/index.html`：线路与方案主页；`static/notifications.html`：乘客影响通知台。

## 运行

```bash
python app.py --init
python app.py --port 8010
```

打开 <http://127.0.0.1:8010>。`--init` 会导入两条示例线路、六个站点、一个 23:50 发车的跨日班次和两条乘客订阅示例。数据库默认是 `transit_disruption.db`，可用 `--db` 或 `TRANSIT_DB` 修改。

## 业务能力

- 基础数据导入会一次性检查线路、站点经纬度、连续站序、重复站点、站间行驶时间和班次时间。错误批次写入 `import_errors` 后整体拒绝，不留下半批数据。
- 中断事件可以包含 `stop_closure`、`skip_stop`、`detour`、`accessibility_change`，可以设置服务日分钟窗口。
- 路径使用 Dijkstra 算法比较基线与方案版本；跳站时车辆可继续通过，但乘客不能在跳站上下车，经过省略路段的行驶时间会计入下一段。绕行（detour）按线路替换走廊内的原通行边并跳过走廊内中间站，因此绕行变长会真实增加乘客耗时（其他线路仍可服务这些中间站）。
- 班次时间以服务日零点起算，允许超过 1440 分钟。例如 1430 分发车、21 分钟到达会显示为次日 `00:21`。
- 修改只允许发生在草稿版本；创建新版本会复制父版本变更，已发布快照继续保留。
- 发布在一个 SQLite 事务内写入方案快照和 SHA-256，旧发布版本不会被覆盖。

## 乘客影响通知台

乘客订阅包含起终点、常用服务时间窗口（服务日分钟，可跨日到 2880）和"只能走无障碍路线"标记。对**草稿版本**分析时，系统在每位乘客的服务时间窗口内取多个代表时刻，分别按基线和方案计算路径：

- `route_broken`：原来的走法在方案中中断（不可达），尝试给出可走替代路线，实在接不上时保留原因，`alternative` 为空。
- `detour_delay`：仍然可达但方案走法比基线**多出严格超过 15 分钟**（即 ≥16 分钟），方案走法作为替代路线给出（含经停站、各段是常规还是绕行、耗时和到达时刻）。
- `accessibility_lost`：只能走无障碍路线的乘客，方案下起/终点或路径无障碍条件不再满足；若仍存在无障碍走法则按绕行延误处理，否则保留原因不硬给替代。
- 基线下本就不通的订阅不纳入方案影响，作为 `skipped` 返回；不受影响的乘客不进名单。

名单确认与冻结规则：

- 分析结果落库为待通知名单，调度员可**逐条确认**或**整单确认**；即使没有乘客受影响，空名单也必须显式确认。
- 未确认整份名单时，已批准版本也不能发布（发布事务内含闸门）。
- 草稿方案内容再改，名单自动作废，需要重新分析确认。
- 存在已提交复核或已批准但未发布的版本时，订阅资料的增改删会被拒绝，避免名单被静默写脏；请先把版本退回草稿。
- 发布在同一事务内把每条通知的乘客资料、基线/方案路线和替代路线快照冻结；发布后基础数据或订阅再改都不动旧名单，被冻结引用的订阅不能删除。
- 新版本不继承名单，必须重新分析确认。

## API

使用 `X-User`、`X-Role` 身份头，角色包括 `planner`、`editor`、`reviewer`、`admin`。

- `POST /api/import`：导入基础数据。
- `POST /api/disruptions`：创建中断事件及第一版草稿。
- `POST /api/disruptions/{id}/versions`：从指定父版本复制出新草稿。
- `POST /api/versions/{id}/changes`：向草稿添加停运、跳站、绕行或无障碍变化。
- `POST /api/versions/{id}/submit|approve|reject|publish`：完成复核发布流程（publish 前必须确认通知名单）。
- `GET /api/route?from=1&to=5&version_id=1&at_minute=1430&accessible=true`：查询路径、耗时和到达时间。
- `GET /api/trips/{id}`：查看跨日班次各站时间。
- `GET /api/import-errors`：查看被隔离的错误批次。
- `GET/POST/PUT/DELETE /api/subscriptions[/{id}]`：乘客订阅资料增改查删（仅 planner/editor/admin 可写）。
- `POST /api/versions/{id}/analyze`：对草稿版本分析乘客影响并落库待通知名单。
- `GET /api/versions/{id}/notifications`：查看名单及确认/冻结状态。
- `GET /api/notifications/{id}`：查看单条通知。
- `POST /api/versions/{id}/confirm`：整单确认；`POST /api/versions/{id}/notifications/{nid}/confirm`：逐条确认。
- 页面：`/`（线路主页）、`/notifications`（乘客影响通知台）。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖基线/改道路径（含绕行替换走廊与 15 分钟边界）、版本复制与发布隔离、审批冲突、无障碍路径与无障碍条件失效、跨日时刻、坏数据整批隔离，以及订阅校验、三类影响判断、名单确认闸门、发布冻结与新版本重算。
