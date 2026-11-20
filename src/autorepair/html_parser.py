"""Static DOM extraction preserving source hierarchy, logical cells and inline links."""

from bs4 import BeautifulSoup, Comment, NavigableString

from autorepair.formatting import table_markdown
from autorepair.models import Block, Issue


def html_blocks(content: str, prefix: str = "") -> tuple[list[Block], list[Issue]]:
    soup = BeautifulSoup(content, "lxml")
    issues = []
    if soup.find("script") or soup.find("style") or soup.find("link", rel="stylesheet"):
        issues.append(
            Issue(
                code="html.rendering",
                severity="warning",
                message="含脚本或样式表，仅提取静态 DOM；需确认可见性",
            )
        )
    paths = {}
    for tag in soup.find_all(True):
        segments = []
        current = tag
        while getattr(current, "name", None) and current.name != "[document]":
            index = 1 + sum(
                getattr(x, "name", None) == current.name for x in current.previous_siblings
            )
            segments.append(f"{current.name}[{index}]")
            current = current.parent
        paths[id(tag)] = "/" + "/".join(reversed(segments))
    for tag in list(soup.find_all(True)):
        if tag.parent is None:
            continue
        style = "".join((tag.get("style") or "").lower().split())
        if (
            tag.name in {"script", "style", "noscript", "nav", "header", "footer"}
            or tag.has_attr("hidden")
            or str(tag.get("aria-hidden", "")).lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            tag.decompose()
    blocks = []
    section = ""
    stack = []
    seen_images = set()

    def inline(node):
        result = []
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                result.append(str(child))
            elif child.name == "br":
                result.append(" ")
            elif child.name not in {"ul", "ol", "table", "img"}:
                result.append(inline(child))
        return "".join(result).strip()

    def emit(tag, text, kind="paragraph", **kwargs):
        if not text and kind != "picture":
            return
        anchors = ([tag] if tag.name == "a" and tag.has_attr("href") else []) + tag.find_all(
            "a", href=True
        )
        labels = [
            {"label": a.get_text(" ", strip=True), "target": str(a["href"])}
            for a in anchors
            if not a.find_parent(["ul", "ol"]) or kind == "list"
        ]
        location = {"dom": prefix + paths.get(id(tag), "/"), "link_labels": labels}
        block = Block(
            block_id=f"b{len(blocks) + 1:04}",
            kind=kind,
            original=text,
            text=text,
            section=section,
            section_path=[x[1] for x in stack],
            location=location,
            links=[x["target"] for x in labels],
            **kwargs,
        )
        blocks.append(block)

    def images(tag):
        for image in tag.find_all("img"):
            walk(image)

    def logical_table(tag):
        grid = {}
        rows = tag.find_all("tr", recursive=True)
        rows = [r for r in rows if r.find_parent("table") is tag]
        for ri, row in enumerate(rows):
            col = 0
            for td in row.find_all(["th", "td"], recursive=False):
                while (ri, col) in grid:
                    col += 1
                try:
                    rs = min(200, max(1, int(td.get("rowspan", 1))))
                    cs = min(100, max(1, int(td.get("colspan", 1))))
                except (TypeError, ValueError):
                    rs = cs = 1
                    issues.append(
                        Issue(
                            code="html.invalid_span",
                            severity="warning",
                            message="非法表格合并范围需复核",
                        )
                    )
                text = td.get_text(" ", strip=True)
                for r in range(ri, ri + rs):
                    for c in range(col, col + cs):
                        grid[r, c] = text
                col += cs
        if not grid:
            return ""
        height = max(r for r, _ in grid) + 1
        width = max(c for _, c in grid) + 1
        if tag.find(attrs={"rowspan": True}) or tag.find(attrs={"colspan": True}):
            issues.append(
                Issue(
                    code="html.table_span",
                    severity="warning",
                    message="合并单元格已按逻辑网格展开，仍需核对共享范围",
                )
            )
        return table_markdown([[grid.get((r, c), "") for c in range(width)] for r in range(height)])

    def walk(node):
        nonlocal section, stack
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            if str(node).strip():
                parent = node.parent
                emit(parent, str(node).strip())
            return
        if node.name in {"head", "br"}:
            return
        if node.name == "img":
            if id(node) in seen_images:
                return
            seen_images.add(id(node))
            alt = node.get("alt", "").strip() or "未提供替代文字的图片"
            emit(node, alt, "picture", image_src=str(node.get("src", "")))
            issues.append(
                Issue(
                    code="image.unverified",
                    severity="warning",
                    message="已保留图片引用与替代文字，图内内容需复核",
                    block_id=blocks[-1].block_id,
                )
            )
            return
        if node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(node.name[1:])
            text = inline(node)
            section = node.get("id") or text
            stack = [x for x in stack if x[0] < level] + [(level, text)]
            emit(node, text, "heading", heading_level=level, anchor=str(node.get("id", "")))
            return
        if node.name == "table":
            emit(node, logical_table(node), "table")
            images(node)
            return
        if node.name == "li":
            parents = node.find_parents(["ul", "ol"])
            parent = node.parent
            index = 1 + sum(getattr(x, "name", None) == "li" for x in node.previous_siblings)
            emit(
                node,
                inline(node),
                "list",
                list_depth=max(0, len(parents) - 1),
                ordered=parent.name == "ol",
                ordinal=index,
            )
            for child in node.children:
                if getattr(child, "name", None) in {"ul", "ol"}:
                    walk(child)
            images(node)
            return
        if node.name in {"p", "pre", "blockquote"}:
            emit(node, inline(node))
            images(node)
            return
        for child in node.children:
            walk(child)

    walk(soup.body or soup)
    return blocks, issues
