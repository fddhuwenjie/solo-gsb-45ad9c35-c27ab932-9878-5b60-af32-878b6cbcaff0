# 静态文档包链接一致性发布 API

用 Python + FastAPI + SQLite 实现的静态文档包发布服务。客户端携带**预期修订号**批量
新增、替换或删除以 `/` 开头的 HTML 页面；服务在**同一个 SQLite 写事务**内先试用整批改动，
再核对最终所有页面的站内 `href`，全部通过才提交发布，否则内容与修订号均不变。

服务还提供**页面搬迁**能力：提交预期修订号、已发布且非 `/index.html` 的旧路径与未占用的
新路径，搬迁后旧路径消失、新页面保留原有正文与 `id`，相关站内链接同步改指/重定位。

## 一键启动

```bash
docker compose up --build -d
```

启动时会自动安装依赖、初始化 SQLite 数据库（表 + 修订号 0）。

- 服务地址：**http://localhost:8000**
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据库存放在 Docker 命名卷 `docs-data` 中，容器/进程重启后数据仍可读取。

本地非 Docker 运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 首次启动自动建表
```

## 规则说明

- **修订号（乐观锁）**：初始为 `0`，每次成功发布 +1。请求必须带 `expected_revision`，
  与当前值不一致时返回 `409 Conflict`，不改变任何内容。
- **入口页**：首次发布（revision 0 → 1）的整批结果必须包含 `/index.html`；
  此后 `/index.html` 不得被删除（可以替换）。违反返回 `422`。
- **批量变更**：`op` 为 `upsert`（新增或替换，别名 `put`）或 `delete`。
  变更按数组顺序在内存中的当前快照上试用，得到最终页面集合后统一校验。
- **链接校验**（仅检查最终集合内所有页面的站内 `<a href>`）：
  - 绝对路径（`/a.html`）与相对路径（`b.html`、`../x.html`）都必须指向现存页面；
  - 目录形式以 `/` 结尾时映射到该目录的 `index.html`（如 `/docs/` → `/docs/index.html`）；
  - `#fragment` 必须是目标页面中现存的元素 `id`（`#` 单独使用指向当前页面）；
  - 查询参数 `?a=b` 不改变目标（`/x.html?v=2` 与 `/x.html` 同一目标）；
  - 链接路径越过站点根目录（如 `/../etc/passwd`、根目录下的 `../x`）判为非法；
  - 外部链接（`http(s)://`、`//host`、`mailto:`、`tel:` 等）不检查。
- 校验失败时 `422` 响应逐项列出 `source`（来源页面）、`target`（错误目标，原始 href）
  与 `reason`（`target page not found` / `target anchor not found` /
  `path escapes site root`）。
- 校验与发布在同一个 `BEGIN IMMEDIATE` 写事务中完成，读接口永远只读到完整提交的快照。

### 页面搬迁（`POST /move`）

请求体 `{"expected_revision":n,"old_path":"/old.html","new_path":"/new.html"}`，规则：

- **前置条件**：`old_path` 必须在当前修订中已发布，且不能是 `/index.html`；
  `new_path` 必须未被占用。违反返回 `422`，内容与修订号不变。
- **正文与 id 保留**：搬迁后的新页面逐字保留原正文文本、元素 `id` 与其他属性；
  旧路径从整包消失。
- **其他页面**：凡站内 `href` 原本指向旧页（含 `#fragment`、目录形式等），一律改指
  新页；绝对链接改绝对、相对链接改相对（相对引用基于各自所在目录重新计算）。
- **搬迁页自身**：相对 `href` 以新页面位置重新定位，使原本指向的页面与片段在搬迁后
  仍可到达；指向旧页的链接（含绝对自指）改指新页；纯片段（`#x`）、纯查询（`?x`）
  等同页引用原样保留。
- **查询参数与片段**：改写只替换路径段，`?a=b`、`#frag` 原文照旧保留。
- **外部链接与非链接内容不改**：`http(s)://`、`//host`、`mailto:` 等外链不动；
  `<img src>`、注释、文本、其他标签属性等均逐字节保留。
- 改写完成后**沿用同一套链接解析与整包校验**（与 `/publish` 完全一致）；新路径冲突或
  任一链接无法维持指向时，整次搬迁失败，**内容与修订号均不变**。
- 过期 `expected_revision` 仍按发布接口的冲突规则返回 `409`（响应体相同）。
- 成功时在**同一个 SQLite 写事务**内生成一个新修订（修订号 +1），历史快照仍可通过
  `GET /pages?revision=N` 读取，原发布与读取接口继续可用。

## API

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /revision` | 当前修订号 |
| `GET /pages` | 当前修订的完整页面集合 `{"revision":n,"pages":{路径:内容}}` |
| `GET /pages?revision=N` | 读取第 N 次发布的完整集合；不存在返回 404 |
| `POST /publish` | 批量发布，请求体 `{"expected_revision":n,"changes":[...]}` |
| `POST /move` | 页面搬迁，请求体 `{"expected_revision":n,"old_path":"...","new_path":"..."}` |

## 可执行的 HTTP 调用示例

启动服务后直接运行（仓库内已带可执行脚本）：

```bash
chmod +x examples.sh && ./examples.sh
```

或手动用 curl 逐步体验：

```bash
# 初始状态
curl -s http://localhost:8000/revision            # {"revision":0}

# 首次发布（成功，revision -> 1）
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 0,
    "changes": [
      {"op":"upsert","path":"/index.html",
       "content":"<h1 id=\"top\">首页</h1><a href=\"/about.html\">关于</a>"},
      {"op":"upsert","path":"/about.html",
       "content":"<a href=\"/#top\">回到锚点</a>"}
    ]
  }'

# 删除仍被引用的页面 -> 422，列出来源 /index.html 与错误目标 /about.html
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"changes":[{"op":"delete","path":"/about.html"}]}'

# 过期修订号 -> 409
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":0,"changes":[]}'

# 同一批内替换 index（去掉失效链接）并删除 about -> 200，revision -> 2
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 1,
    "changes": [
      {"op":"upsert","path":"/index.html","content":"<h1>首页</h1>"},
      {"op":"delete","path":"/about.html"}
    ]
  }'

# 读取当前完整集合 / 历史集合
curl -s http://localhost:8000/pages
curl -s "http://localhost:8000/pages?revision=1"
```

### 页面搬迁示例

```bash
# 再发布一页，并让 index 引用它（revision -> 3）
curl -s -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 2,
    "changes": [
      {"op":"upsert","path":"/index.html","content":"<h1>首页</h1><a href=\"/docs/guide.html\">指南</a>"},
      {"op":"upsert","path":"/docs/guide.html","content":"<a href=\"../index.html\">返回</a><h2 id=\"ch1\">第一章</h2>"}
    ]
  }'

# 搬迁 /docs/guide.html -> /help/manual.html（revision -> 4）
# index 中的 /docs/guide.html 自动改指 /help/manual.html；
# 搬迁页自身的 ../index.html 按新位置重定位为 ../index.html（深度相同），id="ch1" 保留
curl -s -X POST http://localhost:8000/move \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":3,"old_path":"/docs/guide.html","new_path":"/help/manual.html"}'
# {"revision":4,"moved":true,"old_path":"/docs/guide.html","new_path":"/help/manual.html"}

# 新路径已被占用 -> 422，内容与修订号不变
curl -s -X POST http://localhost:8000/move \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":4,"old_path":"/help/manual.html","new_path":"/index.html"}'

# 搬迁入口页 -> 422
curl -s -X POST http://localhost:8000/move \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":4,"old_path":"/index.html","new_path":"/home.html"}'

# 过期修订号 -> 409（与 /publish 同一冲突响应）
curl -s -X POST http://localhost:8000/move \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"old_path":"/help/manual.html","new_path":"/x.html"}'
```

## 项目结构

```
app/
  db.py         # SQLite 连接、建表、修订号元数据
  linkcheck.py  # href 解析（相对/绝对/#片段/查询串/越界）与整批链接校验
  publish.py    # 单写事务：试用整批改动 -> 校验 -> 提交；冲突/校验错误；提交修订辅助
  move.py       # 页面搬迁：href 改指/重定位（仅改链接、逐字节保留其他内容）+ 整包校验
  main.py       # FastAPI 路由与请求模型（/publish、/move）
tests/          # pytest 端到端 + 链接解析/搬迁改写单测
Dockerfile
docker-compose.yml
entrypoint.sh   # 启动时初始化数据库再拉起 uvicorn
examples.sh     # 可执行的 HTTP 调用示例
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```
