# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 收件凭证（不可变清单 / 版本链）

鉴定报告、移交文书等每次收件逐行保存**文件名、大小、SHA-256、提交人快照、关联业务快照**。
清单采用仅追加（append-only）设计，首件 / 补件 / 作废均产生新版本，旧版本只做状态迁移，
清单内容永不覆盖：

- 三层不可变防线：模型信号拦截实例写入、冻结 Manager 拦截批量 `update/delete`、
  数据库触发器（SQLite/PostgreSQL）拦截绕过 ORM 的原生 SQL；
- 关联业务以 `biz_type + biz_ref` 字符串快照引用，业务记录改名或换主键不影响凭证，
  收件编号 `packet_no`（`NB…`）为对外稳定引用；
- 每个版本带整单 `manifest_hash`（截至该版本全部清单行的确定性指纹），可随时重算复核。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/custody/packets/` | 首次收件，建立收件单与 v1（支持 JSON 摘要或 multipart 直传，哈希服务端计算） |
| GET | `/api/custody/packets/<no>/` | 收件单概要 |
| GET | `/api/custody/packets/<no>/versions/` | 版本链与各版本时点的完整清单 |
| POST | `/api/custody/packets/<no>/supplement/` | 补件，逐文件返回 `added`/`duplicate`/`name_conflict` |
| POST | `/api/custody/packets/<no>/revoke/` | 作废，追加 revocation 版本 |
| GET | `/api/custody/packets/<no>/verify/?version_no=N&sha256=…` | 验证摘要是否属于指定版本 |

上传判定规则：相同 SHA-256 记为重复（不产生新版本）；同名不同内容记为冲突并拒绝；
新内容追加新版本。并发补件由收件单锁串行化，相同内容并发提交时一方 `added`、一方 `duplicate`。

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
