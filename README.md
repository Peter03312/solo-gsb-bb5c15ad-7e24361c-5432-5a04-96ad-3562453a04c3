# 陶片拼接系统

面向文物修复人员的陶片拼接 Web 应用：录入毫米制闭合轮廓，基于对应边缘标记
做纯平移 + 旋转的刚体配准，支持预览、采纳、撤销与连通组管理。

## 功能

- **碎片录入**：画布点击或粘贴 JSON 录入毫米制闭合轮廓；自交轮廓整次拒绝。
- **候选拼接**：两片各至少 2 处对应边缘标记（数量一致、同片不重合）。
- **预览**：仅经平移 + 旋转的组合，显示各片位置 (x, y)、角度 (°)、每对
  对应点误差及冲突原因，不落库。
- **采纳校验**（任一不满足即整次拒绝，原拼接组不变）：
  - 同组对应点误差均 ≤ 1 mm；
  - 同组陶片轮廓内部不重叠（允许共边/共点）；
  - 合并两组时整体移动其中一组，组内相对位姿不变；
  - 同组内新增关系（闭环）必须符合已有位姿。
- **撤销**：按剩余有效关系重算连通组，孤片恢复独立状态。
- **有序试拼**：基于当前装配版本快照编排**非空有序**的操作序列（每步为
  「撤销一条现存有效关系」或「以两片陶片 + 成对边缘标记建立关系」），
  在内存快照上逐步推演预演，返回每步结果、最终位姿与分组（或首个失败
  原因），全程不改数据；确认时按同一版本与同一序列整体提交，任一步失败
  或版本过期均不写入任何变化，成功仅递增一次装配版本。
- **轮廓修订**：修复师发现某片陶片轮廓测量有误时，可选中该片提交
  修订后的毫米制闭合轮廓、**该片每一条有效拼接关系**在本片一侧的全新
  边缘标记及期望装配版本，先预览再确认。修订轮廓须通过现有非自交校验；
  新标记须位于修订轮廓边缘、数量与关系另一端一致、至少 2 处且同片不
  重合；随后**保持现有位姿不变**，检查受影响连通组全部有效关系的逐对
  对应点误差 ≤ 1 mm、组成员轮廓内部不重叠。独立碎片只校验轮廓。预览
  返回逐条误差与冲突原因、不落库；确认时重新校验同一输入并原子更新
  轮廓、相关标记与误差——位姿、关系 ID、连通分组与已撤销历史均不变，
  装配版本只 +1。遗漏任何有效关系、任一校验失败均整次拒绝；版本过期
  返回 409。
- **乐观并发**：采纳 / 撤销 / 删除 / 试拼确认 / 轮廓修订确认携带期望版本号，过期版本返回 409 冲突提示。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

构建镜像时自动安装依赖，容器启动时自动初始化 SQLite 表结构与演示数据。

- **访问地址**：http://localhost:8000
- **端口**：`8000`（宿主机映射，可在 `docker-compose.yml` 的 `ports` 中修改）
- **API 文档**：http://localhost:8000/docs

## 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000 。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `data/app.db`（容器内 `/app/data/app.db`） | SQLite 数据库文件路径 |
| `SEED_DEMO_DATA` | `1` | 首次启动且库为空时写入 4 片演示陶片；设为 `0` 关闭 |

Compose 默认将数据库挂载到命名卷 `pottery-data` 持久化。

## 使用流程

1. **录入碎片**：点击「录入碎片」，在主画布单击添加轮廓顶点（毫米制，
   滚轮缩放、拖拽平移），≥3 个顶点后保存；或直接粘贴坐标 JSON。
2. **新建拼接**：点击「新建拼接」，选择碎片甲 / 乙，在两个小图中点击
   轮廓边缘放置对应标记（每片 ≥2 处、数量一致，按序号一一对应）。
3. **预览**：查看组合后的各片位置、角度、每对误差与冲突原因。
4. **采纳**：校验全部通过才落库；任何冲突整次拒绝，原拼接组不变。
5. **撤销**：在「查看装配」页对有效关系点「撤销」，连通组自动重算。
6. **有序试拼**（多步预演 + 一次确认）：
   - 在「新建拼接」页放好两片标记后点「**加入试拼**」，或在「查看装配」页
     点有效关系旁的「**试拼撤销**」，把操作排入序列；
   - 侧栏「有序试拼」自动向服务器预演，逐步显示可执行 / 冲突原因，主画布
     以蓝色展示预演后的最终位姿与连通组（此时数据未改变）；
   - 全部步骤通过后点「**确认整组提交**」：按当前版本与整个序列原子写入，
     版本只 +1；任一步失败或版本过期则没有任何变化，按提示刷新后重试。
7. **修订轮廓**（测量有误时，先预览再确认）：
   - 在「查看装配」页点碎片卡片上的「**修订轮廓**」，进入修订页；
   - 在主画布单击绘制修订后的闭合轮廓（局部坐标、毫米制，可粘贴 JSON /
     一键重置为现轮廓）；
   - 每条有效拼接关系都有一个小图，在本片修订轮廓边缘点击放置新标记
     （自动吸附边缘，数量须与另一端一致、≥2 处、同片不重合；独立碎片
     无需标记）；
   - 点「**预览**」：以现有位姿在主画布显示受影响组的逐对对应点（绿色
     通过 / 红色冲突），侧栏列出逐关系逐条误差、最大误差、碰撞与冲突
     原因，此时**不写库**；
   - 预览通过后点「**确认修订**」：服务器用同一输入重新校验，原子更新
     轮廓、相关标记与误差；位姿、关系 ID、连通分组、已撤销历史均不变，
     装配版本只 +1；任何冲突整次拒绝且没有任何变化，版本过期按提示刷新。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/fragments` | 录入碎片（自交轮廓 400 拒绝） |
| `GET` | `/api/fragments` | 碎片列表 |
| `DELETE` | `/api/fragments/{id}?expected_version=N` | 删除碎片及其关系 |
| `POST` | `/api/relations/preview` | 预览拼接（不落库） |
| `POST` | `/api/relations` | 采纳拼接（携带 `expected_version`） |
| `POST` | `/api/relations/{id}/undo` | 撤销关系（携带 `expected_version`） |
| `GET` | `/api/relations` | 关系列表（含已撤销历史） |
| `GET` | `/api/assembly` | 装配快照：版本、位姿、连通组、关系 |
| `POST` | `/api/trials/rehearse` | 有序试拼预演（不落库） |
| `POST` | `/api/trials/commit` | 有序试拼整组确认（原子，版本只 +1） |
| `POST` | `/api/fragments/{id}/revision/preview` | 轮廓修订预览（逐条误差/冲突，不落库） |
| `POST` | `/api/fragments/{id}/revision/confirm` | 轮廓修订确认（重新校验同一输入，原子更新，版本只 +1） |

错误约定：`400` 校验拒绝（轮廓自交 / 标记重合 / 标记不在边缘 / 标记数量
不一致 / 遗漏有效关系 / 误差超限 / 碰撞 / 空序列 / 撤销目标非法），`404`
对象不存在（碎片、关系缺失），`409` 版本冲突或重复撤销，`422` 请求体
结构不合法。

## 有序试拼 API

两个端点请求体相同：`expected_version`（所基于的装配版本）+ 非空 `steps`。
每个 step 通过 `action` 区分：

```jsonc
// 建立关系：两片陶片 + 成对边缘标记（每片 ≥2 处、数量一致、同片不重合）
{ "action": "relate", "fragment_a": 1, "fragment_b": 2,
  "markers_a": [[35,0],[38,15]], "markers_b": [[1,0],[4,15]],
  "client_id": "s1" }          // 可选，序列内唯一，供后续撤销步引用

// 撤销：relation_id 撤销已落库关系；client_id 撤销序列内前序新建关系
{ "action": "undo", "relation_id": 3 }
{ "action": "undo", "client_id": "s1" }
```

### 预演（不落库）

```bash
curl -X POST http://localhost:8000/api/trials/rehearse \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 1,
    "steps": [
      {"action":"relate","fragment_a":1,"fragment_b":2,
       "markers_a":[[35,0],[38,15],[33,30]],
       "markers_b":[[1,0],[4,15],[-1,30]],"client_id":"s1"},
      {"action":"undo","client_id":"s1"},
      {"action":"relate","fragment_a":1,"fragment_b":2,
       "markers_a":[[35,0],[38,15]],
       "markers_b":[[1,0],[4,15]],"client_id":"s2"}
    ]
  }'
```

成功响应：`ok=true`，`steps[]` 含每步类型、对应点误差、涉及碎片位姿、
当步后的连通组；`assembly` 为最终位姿 / 分组 / 关系快照（预演新建关系使用
负的临时 id，`assembly.version` 仍为所基于的版本）。

任一步失败时 HTTP 仍为 `200`，但 `ok=false`：`failed_at` 与 `failure`
给出首个失败步、错误码（如 `relation_inactive` / `fragment_missing` /
`relation_not_found` / `client_relation_not_found` / `rejected`）、人类可读
原因与冲突明细；`steps[]` 只含失败步之前的成功步，`assembly` 为失败前一刻
快照。版本过期返回 `409`。

### 确认（整体提交）

```bash
curl -X POST http://localhost:8000/api/trials/commit \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":1,"steps":[
    {"action":"relate","fragment_a":1,"fragment_b":2,
     "markers_a":[[35,0],[38,15]],"markers_b":[[1,0],[4,15]],
     "client_id":"s1"}
  ]}'
# 201: {"ok":true,"version":2,"committed_steps":1,
#       "relation_ids":{"s1":1},"assembly":{...}}
```

- 全部步骤通过 → `201`，返回新版本号、`client_id → 真实关系 id` 映射和
  最新装配快照，整个序列只递增一次版本；
- 任一步失败 → `400/404/409`，`detail` 含 `failed_at / step / action /
  code / message`，**不写入任何变化**；
- `expected_version` 过期 → `409`，同样不写入。

明确的报错约定：重复撤销（关系已失效）→ `409 relation_inactive`；碎片缺失
→ `404 fragment_missing`；关系 id 缺失 → `404 relation_not_found`；撤销
未知 `client_id` → `404 client_relation_not_found`；同片建立关系、标记数量
不足 / 重合、误差 >1mm、轮廓内部重叠、闭环不符位姿 → `400 rejected`
（冲突明细在 `conflicts[]`）；空序列 / 撤销同时给或不给目标 /
`client_id` 重复 → `400`。

## 轮廓修订 API

选中碎片 `#1`，提交**修订后的毫米制闭合轮廓** `contour`、**该片每一条有效
关系**在本片一侧的新边缘标记 `relations[]`，以及所基于的
`expected_version`。每个条目按 `relation_id` 指定关系、`markers` 给出本片
（关系的 A 侧或 B 侧由系统判定）修订后的标记；关系另一端的标记不可在此
修改。

新标记的约束与建拼一致并额外要求**位于修订轮廓边缘**：数量与另一端一致、
每侧 ≥2 处、同片不重合；遗漏任何一条有效关系整次拒绝。独立碎片
（无有效关系）提交空 `relations`，只做轮廓非自交校验。

### 预览（不落库）

```bash
curl -X POST http://localhost:8000/api/fragments/1/revision/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 3,
    "contour": [[0,0],[35,0],[38,15],[33,30],[37,45],[34,60],[0,60]],
    "relations": [
      {"relation_id": 1, "markers": [[35,0],[38,15],[33,30]]}
    ]
  }'
```

HTTP 恒为 `200`（版本过期为 `409`、碎片/关系不存在为 `404`），响应：

```jsonc
{
  "ok": true,
  "fragment_id": 1,
  "expected_version": 3,
  "independent": false,
  "missing_relation_ids": [],            // 未提交的有效关系 id（有则必拒绝）
  "contour": [[0.0,0.0], "..."],         // 规范化后的修订轮廓
  "conflicts": [],                       // 全局冲突原因（轮廓/标记/误差/碰撞）
  "involved_fragment_ids": [1, 2],       // 受影响连通组
  "relations": [
    { "relation_id": 1, "revised": true, "side": "a",
      "fragment_a": 1, "fragment_b": 2, "other_fragment_id": 2,
      "required_count": 3, "marker_count": 3,
      "errors": [0.0, 0.0, 0.0],         // 逐对对应点误差（mm，位姿保持不变）
      "max_error_mm": 0.0,
      "conflicts": [],                   // 该关系自身的冲突（超限/标记问题）
      "markers_a": [[35,0],"..."],       // 预览生效后的两侧标记（仅替换本片侧）
      "markers_b": [[1,0],"..."] }
  ],
  "overlaps": []                         // 组成员轮廓内部重叠明细（mm²）
}
```

- 受影响连通组内**不直接涉及**修订片的关系也会在 `relations[]` 中逐条核验
  （`revised=false`），其误差必须仍 ≤ 1 mm；
- `ok=false` 时 `conflicts[]` 汇总全部冲突，`relations[].conflicts` 给出逐
  关系明细，`overlaps[]` 给出碰撞双方与重叠面积；预览不写库，可修改后重预。

### 确认（原子更新，版本只 +1）

请求体与预览**完全相同**（服务端重新校验同一输入）：

```bash
curl -X POST http://localhost:8000/api/fragments/1/revision/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 3,
    "contour": [[0,0],[35,0],[38,15],[33,30],[37,45],[34,60],[0,60]],
    "relations": [
      {"relation_id": 1, "markers": [[35,0],[38,15],[33,30]]}
    ]
  }'
# 201: {"ok":true,"fragment_id":1,"version":4,"assembly":{...}}
```

- 全部通过 → `201`：原子写入修订轮廓、每条相关关系本片侧的新标记及最新
  最大误差；**位姿（x/y/theta）、关系 id、连通分组（group_id）、已撤销
  历史（active/undone_at/created_at）保持不变**，装配版本只 +1，返回最新
  装配快照；
- 任一校验失败 → `400`，`detail` 内附与预览相同结构的逐条结果
  （`conflicts / relations / overlaps / missing_relation_ids`），**不写入
  任何变化**；
- `expected_version` 过期 → `409` 冲突提示，不写入；碎片不存在 → `404`；
  提交了库里不存在的关系 id → `404 relation_not_found`；提交已撤销 / 不涉及
  本片的关系、同关系重复提交、遗漏有效关系、标记数量不一致 / 不足 2 处 /
  同片重合 / 不在修订轮廓边缘 → `400`。

## 项目结构

```
app/
  main.py          # FastAPI 路由、单条拼接、版本控制、试拼与轮廓修订端点
  revision.py      # 轮廓修订：边缘标记校验、受影响组误差/碰撞演算（只读）
  trial.py         # 有序试拼：内存快照逐步推演 + 整组提交 + 位姿演算
  geometry.py      # 刚体配准（最小二乘）、轮廓/边缘标记校验、重叠检测
  database.py      # SQLite 持久化、连通组重算
  static/          # 浏览器二维画布前端（原生 JS）
Dockerfile
docker-compose.yml
requirements.txt
```
