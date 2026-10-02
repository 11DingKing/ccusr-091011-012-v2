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

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```

## 文书摘要清单 API（鉴定报告 / 移交文书）

保管服务接收鉴定报告、移交文书的文件摘要（文件名、大小、SHA-256、提交人），
每次收件生成一个**不可变清单版本**；补件与作废均通过追加新版本表达，
历史摘要一经写入不可修改、不可删除。每个版本携带 `prev_hash` / `manifest_hash`
哈希链与业务编号、业务标题、提交人快照，可独立复算验证"当时接收的内容"。

| 接口 | 说明 |
| --- | --- |
| `POST /api/documents/` | 建立业务记录（`doc_no` 业务编号创建后不可变更，保证引用稳定） |
| `GET /api/documents/` | 业务记录列表（`doc_type`、`keyword` 过滤） |
| `GET /api/documents/{doc_no}/` | 业务记录详情（含当前版本号、有效文件数） |
| `PUT /api/documents/{doc_no}/` | 更新标题/备注；试图变更 `doc_no` 返回 400 |
| `POST /api/documents/{doc_no}/receipts/` | 收件登记，追加新清单版本 |
| `POST /api/documents/{doc_no}/invalidations/` | 作废既有条目，追加作废版本（原摘要保留） |
| `GET /api/documents/{doc_no}/versions/` | 清单版本列表 |
| `GET /api/documents/{doc_no}/versions/{n}/` | 指定版本详情（含全部条目与清单哈希） |
| `POST /api/documents/{doc_no}/versions/{n}/verify/` | 验证某摘要是否属于指定版本 |
| `GET /api/documents/{doc_no}/files/` | 当前有效文件清单（已登记且未作废） |
| `POST /api/documents/{doc_no}/verify/` | 验证摘要当前状态：`active` / `voided` / `unknown` |

### 收件示例

```json
POST /api/documents/JD-2026-0001/receipts/
{
  "files": [
    {"file_name": "鉴定意见.pdf", "file_size": 1024, "sha256": "<64位十六进制>"}
  ],
  "note": "首次收件"
}
```

边界行为均有明确结果：

- **相同文件重复上传**：与当前有效条目 SHA-256 相同的文件不重复登记；
  全部重复时不产生新版本，返回 `result: "duplicate"` 及既有条目位置。
- **同名不同内容**：正常登记为新条目，响应 `name_conflicts` 列出同名有效条目，
  两个摘要都保留在有效清单中，互不覆盖。
- **并发补件**：行锁 + `(document, version_no)` 唯一约束 + 冲突重试，
  版本号连续唯一、哈希链完整；重试耗尽返回 409。
- **重复作废**：返回 `result: "already_voided"`，不产生新版本。
- **作废后重新收件**：允许，生成新版本（仅对当前有效条目去重）。
