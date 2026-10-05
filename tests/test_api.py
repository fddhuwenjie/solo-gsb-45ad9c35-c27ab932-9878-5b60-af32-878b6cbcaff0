"""端到端/单元测试。运行：pip install -r requirements.txt pytest && pytest"""

from __future__ import annotations

import os
import tempfile

import pytest

# 必须在导入 app 之前指定数据库文件
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DOCS_DB_PATH"] = _TMP.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.linkcheck import is_external, resolve_target  # noqa: E402
from app.main import app  # noqa: E402
from app.move import rewrite_page  # noqa: E402


@pytest.fixture(scope="module")
def client():
    db.init_db()
    with TestClient(app) as c:
        yield c


# ---------- 链接解析纯函数 ----------

@pytest.mark.parametrize(
    "href,expected",
    [
        ("https://example.com/x", True),
        ("http://a.com", True),
        ("//cdn.example/lib.js", True),
        ("mailto:a@b.com", True),
        ("tel:+123", True),
        ("/a.html", False),
        ("a.html", False),
        ("#section", False),
        ("/dir/?x=1#y", False),
    ],
)
def test_is_external(href, expected):
    assert is_external(href) is expected


def test_resolve_absolute_and_relative():
    assert resolve_target("/index.html", "/docs/a.html") == ("/docs/a.html", None, None)
    assert resolve_target("/docs/a.html", "b.html") == ("/docs/b.html", None, None)
    assert resolve_target("/docs/a.html", "../index.html") == ("/index.html", None, None)
    assert resolve_target("/docs/a.html", "/x?a=1#top") == ("/x", "top", None)
    assert resolve_target("/docs/a.html", "#sec") == ("/docs/a.html", "sec", None)
    assert resolve_target("/docs/a.html", "/dir/") == ("/dir/index.html", None, None)
    assert resolve_target("/docs/a.html", "?x=1#frag") == ("/docs/a.html", "frag", None)


def test_resolve_escape():
    assert resolve_target("/a.html", "/../etc/passwd")[2] == "path escapes site root"
    assert resolve_target("/index.html", "../escape.html")[2] == "path escapes site root"
    assert resolve_target("/docs/sub/a.html", "../../../escape.html")[2] == "path escapes site root"
    # 恰好回到根不算越界
    assert resolve_target("/docs/sub/a.html", "../../escape.html") == ("/escape.html", None, None)


# ---------- 搬迁 href 改写纯函数 ----------

def test_rewrite_other_page_repoints_only_old_target():
    html = ('<a href="/old.html">abs</a> <a href="old.html?q=1#f">rel</a> '
            '<a href="/other.html">o</a> <a href="https://x.com">e</a>')
    out, errors = rewrite_page(
        html, old_path="/old.html", new_path="/new.html",
        source="/p.html", source_base="/p.html", rebase_page=False,
    )
    assert errors == []
    assert '<a href="/new.html">abs</a>' in out
    assert '<a href="new.html?q=1#f">rel</a>' in out
    assert '<a href="/other.html">o</a>' in out  # 非旧页链接不动
    assert '<a href="https://x.com">e</a>' in out  # 外链不动


def test_rewrite_moved_page_rebases_relative_links():
    # 搬迁页（/docs/old.html -> /help/new.html，同深度换目录）：
    # 相对链接以新位置重新定位；同页片段/查询原样；绝对链接不动
    html = ('<a href="../index.html">up</a> <a href="img/a.html">img</a> '
            '<a href="#frag">f</a> <a href="?x=1">q</a> <a href="/other.html">a</a>')
    out, errors = rewrite_page(
        html, old_path="/docs/old.html", new_path="/help/new.html",
        source="/docs/old.html", source_base="/help/new.html", rebase_page=True,
    )
    assert errors == []
    assert '<a href="../index.html">up</a>' in out        # 两个目录都在根下，仍为 ../index
    assert '<a href="../docs/img/a.html">img</a>' in out  # /docs/img 相对 /help/ 重定位
    assert '<a href="#frag">f</a>' in out
    assert '<a href="?x=1">q</a>' in out
    assert '<a href="/other.html">a</a>' in out


def test_rewrite_directory_form_preserved():
    out, _ = rewrite_page('<a href="/old/">x</a>', old_path="/old/index.html",
                          new_path="/new/index.html", source="/p.html",
                          source_base="/p.html", rebase_page=False)
    assert out == '<a href="/new/">x</a>'
    out, _ = rewrite_page('<a href="/old/x.html">x</a>', old_path="/old/x.html",
                          new_path="/n.html", source="/p.html",
                          source_base="/p.html", rebase_page=False)
    assert out == '<a href="/n.html">x</a>'


def test_rewrite_preserves_non_link_markup():
    html = ('<!-- <a href="/old.html">decoy</a> --> <A HREF=\'/old.html\'>cap</A> '
            '<a\nhref = /old.html\n>nl</a> <img src="/old.html"> <span title="/old.html">t</span>')
    out, errors = rewrite_page(
        html, old_path="/old.html", new_path="/n.html",
        source="/p.html", source_base="/p.html", rebase_page=False,
    )
    assert errors == []
    assert '<!-- <a href="/old.html">decoy</a> -->' in out  # 注释内不改
    assert "<A HREF='/n.html'>cap</A>" in out               # 大写/单引号
    assert 'href = "/n.html"' in out                        # 无引号值加引号
    assert '<img src="/old.html">' in out                   # 非 <a> 不改
    assert '<span title="/old.html">t</span>' in out


# ---------- HTTP 端到端 ----------

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_initial_state(client):
    assert client.get("/revision").json() == {"revision": 0}
    assert client.get("/pages").json() == {"revision": 0, "pages": {}}


def test_first_publish_requires_index(client):
    resp = client.post("/publish", json={
        "expected_revision": 0,
        "changes": [{"op": "upsert", "path": "/about.html", "content": "<p>x</p>"}],
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "first publish must include /index.html"
    # 修订号不变
    assert client.get("/revision").json() == {"revision": 0}


def test_first_publish_success(client):
    html = '<html><body><h1 id="top">Home</h1><a href="/about.html">about</a><a href="https://ext.com">ext</a></body></html>'
    about = '<html><body><h2 id="s2">About</h2><a href="/#top">home top</a></body></html>'
    resp = client.post("/publish", json={
        "expected_revision": 0,
        "changes": [
            {"op": "upsert", "path": "/index.html", "content": html},
            {"op": "upsert", "path": "/about.html", "content": about},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"revision": 1, "published": True}

    pages = client.get("/pages").json()
    assert pages["revision"] == 1
    assert set(pages["pages"]) == {"/index.html", "/about.html"}


def test_bad_path_rejected(client):
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [{"op": "upsert", "path": "relative.html", "content": "x"}],
    })
    assert resp.status_code == 422


def test_broken_link_blocks_publish(client):
    # 新页面引用不存在页面 -> 整批拒绝，内容与修订号不变
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [
            {"op": "upsert", "path": "/new.html",
             "content": '<a href="/ghost.html">ghost</a><a href="/about.html#nope">bad anchor</a>'},
        ],
    })
    assert resp.status_code == 422
    errs = {(e["source"], e["reason"]) for e in resp.json()["errors"]}
    assert ("/new.html", "target page not found") in errs
    assert ("/new.html", "target anchor not found") in errs
    assert client.get("/revision").json() == {"revision": 1}
    assert "/new.html" not in client.get("/pages").json()["pages"]


def test_delete_referenced_page_blocks(client):
    # 删除 about.html，但 index.html 仍引用它 -> 报旧页引用被删页面
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [{"op": "delete", "path": "/about.html"}],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["source"] == "/index.html"
    assert e["target"] == "/about.html"
    assert e["reason"] == "target page not found"
    assert client.get("/revision").json() == {"revision": 1}


def test_delete_index_blocked(client):
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [
            {"op": "delete", "path": "/about.html"},
            {"op": "delete", "path": "/index.html"},
        ],
    })
    assert resp.status_code == 422
    assert "must not be deleted" in resp.json()["errors"][0]["reason"]


def test_replace_index_without_link_errors_ok(client):
    # 同时替换 index（去掉 about 链接）并删除 about -> 成功
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [
            {"op": "upsert", "path": "/index.html",
             "content": '<html><body><a href="https://ext.com">ext only</a></body></html>'},
            {"op": "delete", "path": "/about.html"},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2
    assert set(client.get("/pages").json()["pages"]) == {"/index.html"}


def test_query_string_does_not_change_target(client):
    # /index.html?v=2# 与 /index.html?x 查询串不应影响目标判定
    resp = client.post("/publish", json={
        "expected_revision": 2,
        "changes": [
            {"op": "upsert", "path": "/q.html",
             "content": '<a href="/index.html?v=2">query</a>'},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 3


def test_relative_links_and_directory_index(client):
    resp = client.post("/publish", json={
        "expected_revision": 3,
        "changes": [
            {"op": "upsert", "path": "/docs/guide.html",
             "content": '<a href="../index.html">up</a> <a href="/">dir</a>'},
        ],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 4


def test_path_escape_blocked(client):
    resp = client.post("/publish", json={
        "expected_revision": 4,
        "changes": [
            {"op": "upsert", "path": "/evil.html",
             "content": '<a href="/../etc/passwd">x</a>'},
        ],
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "path escapes site root"


def test_stale_revision_conflict(client):
    resp = client.post("/publish", json={
        "expected_revision": 1,
        "changes": [{"op": "upsert", "path": "/x.html", "content": "<p>x</p>"}],
    })
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "revision conflict"
    assert body["expected"] == 1
    assert body["current"] == 4
    assert client.get("/revision").json() == {"revision": 4}


def test_historical_revision_readable(client):
    rev3 = client.get("/pages", params={"revision": 3}).json()
    assert rev3["revision"] == 3
    assert set(rev3["pages"]) == {"/index.html", "/q.html"}

    missing = client.get("/pages", params={"revision": 99})
    assert missing.status_code == 404


def test_upsert_alias_put(client):
    resp = client.post("/publish", json={
        "expected_revision": 4,
        "changes": [{"op": "put", "path": "/putalias.html", "content": "<p>p</p>"}],
    })
    assert resp.status_code == 200


def test_removed_anchor_blocks_publish(client):
    # 现状（revision 5）：/index.html 含 id="top"，/q.html 以 /index.html?v=2 引用（无片段）。
    # 替换 index 去掉 id="top"，再新增引用该锚点的页面 -> anchor 错误，整体拒绝。
    body_before = client.get("/pages", params={"revision": 5}).json()
    assert "/putalias.html" in body_before["pages"]
    resp = client.post("/publish", json={
        "expected_revision": 5,
        "changes": [
            {"op": "upsert", "path": "/index.html",
             "content": '<h1>no ids anymore</h1>'},
            {"op": "upsert", "path": "/anchor-ref.html",
             "content": '<a href="/index.html#top">top</a>'},
        ],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["source"] == "/anchor-ref.html"
    assert e["target"] == "/index.html#top"
    assert e["reason"] == "target anchor not found"
    assert client.get("/revision").json() == {"revision": 5}


def test_empty_changes_is_valid_empty_publish(client):
    # 空变更集同样经历整批校验；内容不变，作为一次合法空发布推进修订号。
    before = client.get("/pages").json()
    resp = client.post("/publish", json={"expected_revision": 5, "changes": []})
    assert resp.status_code == 200
    assert resp.json()["revision"] == 6
    after = client.get("/pages").json()
    assert after["pages"] == before["pages"]


# ---------- 页面搬迁 /move ----------

def test_move_basic_relocation(client):
    # 现状 revision 6：/index.html、/q.html、/docs/guide.html（含 ../index.html 与 /）、/putalias.html
    before = client.get("/pages").json()["pages"]
    assert "/docs/guide.html" in before
    guide_before = before["/docs/guide.html"]

    resp = client.post("/move", json={
        "expected_revision": 6,
        "old_path": "/docs/guide.html",
        "new_path": "/help/guide.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "revision": 7,
        "moved": True,
        "old_path": "/docs/guide.html",
        "new_path": "/help/guide.html",
    }

    pages = client.get("/pages").json()["pages"]
    # 旧路径消失，新路径出现，正文与 id 逐字保留（同深度搬迁，相对/绝对链接仍合法）
    assert "/docs/guide.html" not in pages
    assert pages["/help/guide.html"] == guide_before
    assert set(pages) == {"/index.html", "/q.html", "/help/guide.html", "/putalias.html"}

    # 历史快照仍可读取
    rev6 = client.get("/pages", params={"revision": 6}).json()["pages"]
    assert "/docs/guide.html" in rev6 and "/help/guide.html" not in rev6


def test_move_repoints_other_pages_and_rebases_own_relative(client):
    # revision 7：新增一个引用搬迁页的页面（绝对 + 相对 + 查询串 + 外链）
    resp = client.post("/publish", json={
        "expected_revision": 7,
        "changes": [
            {"op": "upsert", "path": "/ref.html",
             "content": '<a href="/help/guide.html">a</a>'
                        '<a href="help/guide.html?x=1">b</a>'
                        '<a href="https://ext.example">e</a>'},
        ],
    })
    assert resp.status_code == 200, resp.text

    # /help/guide.html -> /guide2.html（目录变浅：help/ -> 根）
    resp = client.post("/move", json={
        "expected_revision": 8,
        "old_path": "/help/guide.html",
        "new_path": "/guide2.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 9

    pages = client.get("/pages").json()["pages"]
    assert "/help/guide.html" not in pages
    assert "/guide2.html" in pages

    # 其他页面：指向旧页的站内 href 改指新页；查询参数照旧；外链不动
    assert '<a href="/guide2.html">a</a>' in pages["/ref.html"]
    assert '<a href="guide2.html?x=1">b</a>' in pages["/ref.html"]
    assert '<a href="https://ext.example">e</a>' in pages["/ref.html"]

    # 搬迁页自身：相对 href 以新位置重新定位（../index.html -> index.html）；绝对链接原样
    assert '<a href="index.html">up</a>' in pages["/guide2.html"]
    assert '<a href="/">dir</a>' in pages["/guide2.html"]


def test_move_preserves_fragment_targets(client):
    # 新增带 id 的页面与指向它的片段链接，搬迁后页面与片段仍可到达
    resp = client.post("/publish", json={
        "expected_revision": 9,
        "changes": [
            {"op": "upsert", "path": "/sec.html",
             "content": '<h2 id="sec-id">S</h2><a href="/#x">home</a>'},
            {"op": "upsert", "path": "/index.html",
             "content": '<h1 id="x">Home</h1><a href="/sec.html?v=2#sec-id">s</a>'},
        ],
    })
    assert resp.status_code == 200, resp.text

    resp = client.post("/move", json={
        "expected_revision": 10, "old_path": "/sec.html", "new_path": "/info/section.html",
    })
    assert resp.status_code == 200, resp.text
    pages = client.get("/pages").json()["pages"]
    # id 保留
    assert '<h2 id="sec-id">S</h2>' in pages["/info/section.html"]
    # 搬迁页自身的绝对主页链接保持绝对；index 内绝对引用改指新页，查询串与片段照旧
    assert '<a href="/#x">home</a>' in pages["/info/section.html"]
    assert 'href="/info/section.html?v=2#sec-id"' in pages["/index.html"]


def test_move_conflicts_and_bad_inputs(client):
    current = client.get("/revision").json()["revision"]
    assert current == 11

    # 新路径被占用 -> 422，修订号不变
    resp = client.post("/move", json={
        "expected_revision": 11, "old_path": "/q.html", "new_path": "/index.html",
    })
    assert resp.status_code == 422
    assert "occupied" in resp.json()["errors"][0]["reason"]

    # 旧路径未发布 -> 422
    resp = client.post("/move", json={
        "expected_revision": 11, "old_path": "/ghost.html", "new_path": "/g2.html",
    })
    assert resp.status_code == 422
    assert "not found" in resp.json()["errors"][0]["reason"]

    # 入口页不允许搬迁 -> 422
    resp = client.post("/move", json={
        "expected_revision": 11, "old_path": "/index.html", "new_path": "/home.html",
    })
    assert resp.status_code == 422
    assert "must not be moved" in resp.json()["errors"][0]["reason"]

    # 非法路径 -> 422（请求模型校验，与发布接口同一规则）
    resp = client.post("/move", json={
        "expected_revision": 11, "old_path": "q.html", "new_path": "/q2.html",
    })
    assert resp.status_code == 422
    resp = client.post("/move", json={
        "expected_revision": 11, "old_path": "/q.html", "new_path": "/q2.html?x=1",
    })
    assert resp.status_code == 422

    assert client.get("/revision").json() == {"revision": 11}

    # 过期修订号 -> 409，沿用发布接口冲突规则
    resp = client.post("/move", json={
        "expected_revision": 1, "old_path": "/q.html", "new_path": "/q2.html",
    })
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "revision conflict"
    assert body["expected"] == 1
    assert body["current"] == 11
    assert client.get("/revision").json() == {"revision": 11}


def test_move_failure_leaves_content_untouched(client):
    # 占用失败是事务前置检查；确认旧页与链接内容均未被改动
    before = client.get("/pages").json()
    resp = client.post("/move", json={
        "expected_revision": 11, "old_path": "/q.html", "new_path": "/index.html",
    })
    assert resp.status_code == 422
    after = client.get("/pages").json()
    assert after == before
    # 成功搬迁一次推进一个修订，之后发布/读取接口继续可用
    resp = client.post("/move", json={
        "expected_revision": 11, "old_path": "/putalias.html", "new_path": "/alias/page.html",
    })
    assert resp.status_code == 200
    assert resp.json()["revision"] == 12
    assert client.get("/pages", params={"revision": 11}).json()["pages"].get("/putalias.html") == "<p>p</p>"
