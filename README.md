# 教学排课管理系统

面向医学影像住培基地的教学排课 + 技能考核（OSCE）+ 钉钉推送一体化系统。

- 后端：FastAPI + SQLModel + SQLite（单文件数据库）
- 前端：Vue 3（CDN/本地 vendor，无构建步骤）+ axios，单页应用
- 机器人：钉钉 Stream 模式（出站长连接，内网可用）

## 快速开始（Windows）

```bat
pip install -r requirements.txt
启动系统.bat
```

浏览器打开 http://127.0.0.1:8000 。

- 默认管理员：`admin` / `admin123`（首次使用后请在「用户管理」改密）
- 教师用工号登录、学生用学号登录，初始密码 `123456`
- 局域网其他电脑访问：`http://<本机IP>:8000`（需在防火墙放行 8000）

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./schedule.db` | 数据库位置，可指向其他目录/磁盘 |
| `TEACH_SECRET` | 自动生成 `secret.key` | JWT 签名密钥。未设置时首次运行自动生成并保存在程序目录；**换机器部署时把这个文件一起拷过去**，否则所有人需重新登录 |

`secret.key`、`schedule.db` 均含敏感信息，已在 `.gitignore` 中排除，请勿提交。

## 目录结构

| 文件 | 职责 |
|---|---|
| `main.py` | FastAPI 入口、静态资源挂载、启动时初始化数据库并拉起钉钉机器人 |
| `api.py` | 业务接口（教师/学生、排课、导入导出、发布推送、反馈、评分等） |
| `engine.py` | 排课算法（轮转指针 + 覆盖偏置 + 逐级放宽约束） |
| `rules.py` | 排课规则（默认值 + 增量覆盖，可在网页端修改） |
| `exam.py` | 技能考核（OSCE）多站排程与导出 |
| `dingtalk_bot.py` | 钉钉 Stream 机器人：查课表、填题目、催办、待办/日历同步 |
| `dingtalk_card.py` | 钉钉互动卡片模板 |
| `auth.py` | 登录、JWT、角色权限 |
| `db.py` | 数据库引擎（WAL/busy_timeout）、迁移与种子数据 |
| `models.py` | 数据模型 |
| `static/index.html` | 全部前端页面 |
| `static/help.html` | 使用帮助（可打印） |
| `_test_*.py` / `_test_*.js` | 离线自检脚本（每个脚本会在临时目录建库，不碰生产数据） |

## 数据库与备份

- SQLite 已开启 WAL + `busy_timeout=5000`，支持机器人线程与网页请求并发写。
- 自动迁移：启动时检查 `PRAGMA user_version` 并增量补列；**有数据的旧库在迁移前会自动备份**为 `schedule.db.bak-migrate-<时间戳>`。
- 手动备份：直接复制 `schedule.db`（建议先停止服务，或同时复制 `-wal`/`-shm` 文件）。

## 钉钉配置

管理员登录后「系统设置 → 钉钉配置」：

1. 主群 Webhook（发布课表时推送）；
2. 「其他钉钉群」可添加多个群，各自独立启用/停用；
3. 机器人（Stream）需要应用的 AppKey/AppSecret；
4. 群机器人若开启「加签」或「关键词」，需在对应群配置里填写。

## 常见问题

见 http://127.0.0.1:8000/help.html ，或在系统左侧栏点「使用帮助 ↗」。
