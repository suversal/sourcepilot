"""HTML → Markdown，用于通用提取器处理不好的站点。

**为什么不用 trafilatura 兜住所有情况**：它的强项是从一整个页面里判断哪块是
正文，为此会做激进的降噪——实测公众号文章 5360 字正文提得很干净，但 6 张配图、
4 个小标题全被当噪音丢掉了。给它还原 `data-src` 也没用，被丢弃的是节点本身。

所以分工是：**容器已知时用这里，容器未知时用 trafilatura**。公众号的正文
恒在 `#js_content` 里（这个 id 多年没变），既然不需要「猜正文在哪」，就不必
承担猜错的代价，直接按标签逐个翻译成 Markdown。

只覆盖内容型标签。样式、脚本、交互元素一律跳过——它们在 Markdown 里没有对应。
"""

from __future__ import annotations

from bs4 import BeautifulSoup, NavigableString, Tag

#: 块级标签 → Markdown 前缀。
_BLOCK_PREFIX = {
    "h1": "# ", "h2": "## ", "h3": "### ",
    "h4": "#### ", "h5": "##### ", "h6": "###### ",
    "blockquote": "> ",
}

#: 行内标签 → Markdown 包裹标记。
_INLINE_MARK = {
    "strong": "**", "b": "**",
    "em": "*", "i": "*",
    "code": "`", "del": "~~", "s": "~~",
}

#: 整棵子树都不要的标签。
_DROP = {"script", "style", "noscript", "iframe", "svg", "form", "button", "input"}


def _image_src(tag: Tag) -> str | None:
    """图片地址。

    懒加载站点把真实地址放在 `data-src` 之类的属性里，`src` 要么缺失、要么是
    占位图——公众号就是这样，6 张配图全部只有 `data-src`。按优先级挨个找，
    找不到就当这张图不存在（宁可少一张，不要一个坏链接）。
    """
    for attr in ("data-src", "data-original", "data-actualsrc", "src"):
        value = (tag.get(attr) or "").strip()
        if value and not value.startswith("data:"):  # data: 是内联占位图
            return value
    return None


def _inline(node, out: list[str]) -> None:
    """行内元素：把标记织进文本流。"""
    if isinstance(node, NavigableString):
        text = str(node)
        # 保留词间空格，但把换行和缩进压平——HTML 里的换行不是内容。
        out.append(" ".join(text.split()) if text.strip() else (" " if text else ""))
        return
    if not isinstance(node, Tag) or node.name in _DROP:
        return

    if node.name == "br":
        out.append("\n")
        return
    if node.name == "img":
        src = _image_src(node)
        if src:
            alt = (node.get("alt") or "").strip()
            out.append(f"![{alt}]({src})")
        return

    inner: list[str] = []
    for child in node.children:
        _inline(child, inner)
    text = "".join(inner)

    if node.name == "a":
        href = (node.get("href") or "").strip()
        # 锚点和 javascript: 不是真链接，退化成纯文本。
        if href and href.startswith(("http://", "https://")):
            out.append(f"[{text.strip()}]({href})" if text.strip() else "")
            return
        out.append(text)
        return

    mark = _INLINE_MARK.get(node.name)
    if mark and text.strip():
        # 标记必须紧贴文字，`** x **` 不是合法的 Markdown 强调。
        lead = " " if text[:1].isspace() else ""
        tail = " " if text[-1:].isspace() else ""
        out.append(f"{lead}{mark}{text.strip()}{mark}{tail}")
    else:
        out.append(text)


def _emit_list_item(li: Tag, marker: str, lines: list[str]) -> None:
    """一个列表项。

    不能把 li 交给 `_walk`——那会去遍历它的**子节点**，标记就没机会加上了。
    嵌套列表另行下钻，缩进两格。
    """
    nested = [c for c in li.children if isinstance(c, Tag) and c.name in ("ul", "ol")]
    for sub in nested:
        sub.extract()

    parts: list[str] = []
    for child in li.children:
        _inline(child, parts)
    text = "".join(parts).strip()
    if text:
        lines.append(f"{marker}{text}")

    for sub in nested:
        inner: list[str] = []
        _walk(sub, inner)
        lines.extend(f"  {line}" for line in inner)


def _walk(node: Tag, lines: list[str], list_prefix: str | None = None) -> None:
    """块级遍历：每个块产出一行（或若干行）。"""
    for child in node.children:
        if isinstance(child, NavigableString):
            text = " ".join(str(child).split())
            if text:
                lines.append(text)
            continue
        if not isinstance(child, Tag) or child.name in _DROP:
            continue

        name = child.name

        if name in ("ul", "ol"):
            ordered = name == "ol"
            for index, li in enumerate(child.find_all("li", recursive=False), start=1):
                marker = f"{index}. " if ordered else "- "
                _emit_list_item(li, marker, lines)
            continue

        if name == "li":  # 脱离 ul/ol 单独出现的 li，少见但别丢
            _emit_list_item(child, list_prefix or "- ", lines)
            continue

        if name in ("table",):
            # 表格保持原样文本。做成 Markdown 表格需要列对齐信息，
            # 公众号的表格常常是排版用的，硬转会产出一堆空列。
            text = " ".join(child.get_text(" ", strip=True).split())
            if text:
                lines.append(text)
            continue

        block_tags = ("div", "section", "article", "span", "p", "blockquote")
        if name in block_tags or name in _BLOCK_PREFIX:
            # 标题永远当一整块处理，不走下面的「纯容器就下钻」那条路。
            # 站点常把标题文字再包一层 `<span>` 做样式（公众号的 4 个 h2
            # 全是这样），下钻的话文字是出来了，`##` 前缀却丢在半路——
            # 结果整篇正文一个小标题都没有。
            has_own = name in _BLOCK_PREFIX or any(
                (isinstance(c, NavigableString) and c.strip())
                or (isinstance(c, Tag) and c.name in {*_INLINE_MARK, "a", "img", "br", "code"})
                for c in child.children
            )
            if has_own:
                # 图片先摘出来单独成行。它们在 Markdown 里本就是块级的，
                # 而站点常把图和说明文字塞进同一个标签做排版——公众号用
                # `<h6><strong><img>△说明</strong></h6>`，不摘的话会产出
                # `###### **![](url)△**说明` 这种既是标题又是强调的畸形串。
                for img in child.find_all("img"):
                    src = _image_src(img)
                    if src:
                        lines.append(f"![]({src})")
                    img.decompose()

                parts: list[str] = []
                for sub in child.children:
                    _inline(sub, parts)
                text = "".join(parts).strip()
                if text:
                    lines.append(f"{_BLOCK_PREFIX.get(name, '')}{text}")
            else:
                _walk(child, lines, list_prefix)
            continue

        if name in ("pre",):
            code = child.get_text("\n", strip=False).strip("\n")
            if code.strip():
                lines.append(f"```\n{code}\n```")
            continue

        if name == "hr":
            lines.append("---")
            continue

        _walk(child, lines, list_prefix)


def to_markdown(container: Tag) -> str:
    """把一个已知的正文容器转成 Markdown。"""
    lines: list[str] = []
    _walk(container, lines)
    # 去掉相邻重复行——嵌套容器偶尔会让同一段落出现两次。
    deduped: list[str] = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return "\n\n".join(deduped).strip()


def extract(html: str, selector: str) -> str | None:
    """按选择器取出容器并转 Markdown。选不中返回 None，让调用方回落。"""
    container = BeautifulSoup(html, "lxml").select_one(selector)
    if container is None:
        return None
    markdown = to_markdown(container)
    return markdown or None
