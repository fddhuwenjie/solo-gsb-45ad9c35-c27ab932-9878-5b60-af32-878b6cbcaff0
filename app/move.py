"""页面搬迁事务：旧路径消失、新路径保留原正文与 id，并改写相关站内 href。

改写在内存中的整包副本上进行，随后沿用 :mod:`app.linkcheck` 的链接解析与
整包校验；任一新路径冲突或链接无法维持指向即回滚，内容与修订号不变。
"""

from __future__ import annotations

import posixpath
import re
import sqlite3
from html.parser import HTMLParser
from urllib.parse import quote, unquote

from .linkcheck import check_links, is_external, resolve_target
from .publish import (
    INDEX,
    PublishConflict,
    PublishValidationError,
    _load_current_pages,
    commit_revision,
)


class _AnchorCollector(HTMLParser):
    """收集页面内所有 <a> 标签的 (起始索引, 原始标签文本, 解码后 href)。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.anchors: list[tuple[int, str, str | None]] = []
        self._line_starts: list[int] = [0]
        self._fed = ""

    def feed(self, data: str):  # type: ignore[override]
        # HTMLParser 可能分片回调；用全部已喂入文本统一计算行偏移，保证索引不串。
        self._fed += data
        self._line_starts = [0]
        offset = 0
        for line in self._fed.split("\n"):
            offset += len(line) + 1
            self._line_starts.append(offset)
        return super().feed(data)

    def _record(self, attrs: list[tuple[str, str | None]]) -> None:
        line, col = self.getpos()
        index = self._line_starts[line - 1] + col
        self.anchors.append((index, self.get_starttag_text() or "", dict(attrs).get("href")))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._record(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._record(attrs)


# 原始标签文本中 href 属性的捕获：引号内容 / 单引号内容 / 无引号值。
_HREF_ATTR_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)


def _split_path_query_fragment(href: str) -> tuple[str, str]:
    """返回 (路径部分, 原始 query+fragment 原文)。

    ``#`` 之前的 ``?`` 才是查询串；query/fragment 原文照旧保留。
    """
    cut = len(href)
    q = href.find("?")
    h = href.find("#")
    if q != -1:
        cut = min(cut, q)
    if h != -1:
        cut = min(cut, h)
    return href[:cut], href[cut:]


def _is_directory_form(path_part: str) -> bool:
    return path_part.endswith("/") or path_part.rstrip("/") == "."


def _encode_path(path: str) -> str:
    """百分号编码路径：保留 ``/`` 与已有的 ``%xx`` 转义。"""
    return quote(unquote(path), safe="/%")


def _format_path(
    target: str,
    *,
    base: str,
    absolute: bool,
    directory_form: bool,
) -> str:
    """按原有风格（绝对/相对、是否目录形式）输出指向 target 的 href 路径。"""
    if directory_form and target.endswith(INDEX):
        target_dir = target[: -len("index.html")]  # 保留结尾 '/'
        target = target_dir if target_dir != "/" else "/"

    if absolute:
        return target if target != "/" else "/"

    base_dir = posixpath.dirname(base).rstrip("/")
    if target.endswith("/"):  # 目录形式：相对计算基于目标目录本身
        rel = posixpath.relpath(target.rstrip("/") or "/", base_dir or "/")
        return (rel + "/") if rel != "." else "./"
    rel = posixpath.relpath(target, base_dir or "/")
    return rel if rel not in ("", ".") else "./"


def _rewrite_href(
    href: str,
    *,
    old_path: str,
    new_path: str,
    source: str,
    source_base: str,
    rebase_page: bool,
) -> tuple[str | None, str | None]:
    """计算单个站内 href 改写后的路径部分。

    返回 (新 href 完整路径部分, 错误原因)：
    - (None, None)：该 href 无需改写，原文保留；
    - (路径, None)：以该路径替换原 href 的路径段（query/fragment 原文照旧）；
    - (None, 原因)：href 本身解析非法（整包校验会一并拦下）。
    """
    h = href.strip()
    if not h or is_external(h):
        return None, None

    target_path, fragment, reason = resolve_target(source, h)
    if reason is not None:
        return None, reason

    path_part, _suffix = _split_path_query_fragment(h)
    targets_old = target_path == old_path
    absolute = path_part.startswith("/")
    directory_form = _is_directory_form(path_part)
    only_same_page_ref = path_part in ("", ".")

    # 其他页面：仅改写指向旧页的链接。
    # 搬迁页：指向旧页的链接（含绝对自指）改指新页；其余相对链接按新位置重定位。
    # 纯片段（#x）、纯查询（?x）、当前目录（.）等同页引用搬迁后仍指向当前页，原样保留。
    if only_same_page_ref:
        return None, None

    needs_rebase = rebase_page and not absolute and path_part
    if not targets_old and not needs_rebase:
        return None, None

    new_target = new_path if targets_old else target_path
    formatted = _format_path(
        new_target,
        base=source_base,
        absolute=absolute,
        directory_form=directory_form,
    )
    return _encode_path(formatted), None


def rewrite_page(
    content: str,
    *,
    old_path: str,
    new_path: str,
    source: str,
    source_base: str,
    rebase_page: bool,
) -> tuple[str, list[dict[str, str]]]:
    """改写单个页面内的 <a href>，返回 (新内容, 错误列表)。

    ``source`` 为改写前链接解析使用的页面路径（搬迁页用旧路径）；
    ``source_base`` 为输出相对链接时相对的目录（搬迁页用新路径）。
    非链接内容（标签、文本、其他属性、实体等）逐字节保留。
    """
    parser = _AnchorCollector()
    parser.feed(content or "")
    parser.close()

    errors: list[dict[str, str]] = []
    pieces: list[str] = []
    cursor = 0
    for index, raw_tag, href in parser.anchors:
        pieces.append(content[cursor:index])
        cursor = index + len(raw_tag)
        if href is None:
            pieces.append(raw_tag)
            continue

        new_path_part, reason = _rewrite_href(
            href,
            old_path=old_path,
            new_path=new_path,
            source=source,
            source_base=source_base,
            rebase_page=rebase_page,
        )
        if reason is not None:
            errors.append({"source": source, "target": href, "reason": reason})
            pieces.append(raw_tag)
            continue
        if new_path_part is None:
            pieces.append(raw_tag)
            continue

        # 仅替换原始属性值中的路径段；引号风格与 query/fragment 原文保留。
        def replace_attr(m: re.Match) -> str:
            text = m.group(0)
            eq = text.index("=")
            i = eq + 1
            while i < len(text) and text[i].isspace():
                i += 1
            # 直接在原始标签文本上定位属性值边界（i 指向引号或值首字符），
            # 避免使用 parser 已解码的值，保证 query/fragment 里的实体逐字节保留。
            if i < len(text) and text[i] in "\"'":
                quote, end = text[i], len(text) - 1
                raw_value = text[i + 1:end]
                _, tail = _split_path_query_fragment(raw_value)
                return f"{text[:i + 1]}{new_path_part}{tail}{quote}"
            raw_value = text[i:]
            _, tail = _split_path_query_fragment(raw_value)
            return f'{text[:i]}"{new_path_part}{tail}"'  # 无引号值统一加双引号

        new_tag, count = _HREF_ATTR_RE.subn(replace_attr, raw_tag, count=1)
        # 理论上必有 href 属性（parser 已确认）；兜底保留原文。
        pieces.append(new_tag if count else raw_tag)

    pieces.append(content[cursor:])
    return "".join(pieces), errors


def move_page(
    conn: sqlite3.Connection,
    expected_revision: int,
    old_path: str,
    new_path: str,
) -> int:
    """在单个写事务内完成页面搬迁与整包校验，成功返回新修订号。

    - 过期修订号抛 :class:`PublishConflict`（与发布接口同一冲突规则）；
    - 旧路径未发布/为入口页/新路径被占用等抛 :class:`PublishValidationError`；
    - 任一链接无法维持指向或整包校验不通过同样回滚，内容与修订号不变。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        current = row["current_revision"]
        if expected_revision != current:
            raise PublishConflict(current)

        errors: list[dict[str, str]] = []
        pages = _load_current_pages(conn, current)

        if current == 0 or old_path not in pages:
            errors.append({"source": old_path, "target": old_path,
                           "reason": "source page not found in published revision"})
        if old_path == INDEX:
            errors.append({"source": old_path, "target": INDEX,
                           "reason": "entry page /index.html must not be moved"})
        if new_path in pages:
            errors.append({"source": old_path, "target": new_path,
                           "reason": "destination path is already occupied"})
        if errors:
            raise PublishValidationError(errors)

        moved_content = pages[old_path]
        pages.pop(old_path)

        # 1) 其他页面：指向旧页的站内 href 改指新页。
        rewritten: dict[str, str] = {}
        for path, content in pages.items():
            new_content, errs = rewrite_page(
                content,
                old_path=old_path,
                new_path=new_path,
                source=path,
                source_base=path,
                rebase_page=False,
            )
            rewritten[path] = new_content
            errors.extend(errs)

        # 2) 搬迁页自身：保留正文与 id，相对 href 以新位置重新定位，
        #    指向旧页的链接同样改指新页。
        moved_new, errs = rewrite_page(
            moved_content,
            old_path=old_path,
            new_path=new_path,
            source=old_path,
            source_base=new_path,
            rebase_page=True,
        )
        rewritten[new_path] = moved_new
        errors.extend(errs)

        if errors:
            raise PublishValidationError(
                sorted(errors, key=lambda e: (e["source"], e["target"], e["reason"]))
            )

        # 3) 沿用现有链接解析与整包校验，确认所有链接原本指向的页面与片段仍可到达。
        link_errors = check_links(rewritten)
        if link_errors:
            raise PublishValidationError(link_errors)

        new_revision = commit_revision(conn, rewritten, current)
        conn.commit()
        return new_revision
    except Exception:
        conn.rollback()
        raise
