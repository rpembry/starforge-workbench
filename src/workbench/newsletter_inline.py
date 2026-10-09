"""Safe full-content email HTML conversion; no network, scripts or task creation."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import unquote, urlsplit

VERSION = 'newsletter-inline.v1'
HEADER = '**Mobile reading copy — full newsletter**\n\nOriginal email attachment is preserved in the comment below. Article text is unchanged; links open the original destinations.\n\n'
MAX_HTML_BYTES = 2_000_000
MAX_DESCRIPTION = 16_384


class ConversionError(ValueError):
    pass


@dataclass(frozen=True)
class Conversion:
    description: str
    visible_text: str
    links: int
    headings: int


def safe_link(value: str) -> str | None:
    """Decode only the observed newsletter redirect format, never visit links."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.hostname == 'tracking.tldrnewsletter.com' and parsed.path.startswith('/CL0/'):
            value = unquote(parsed.path.split('/CL0/', 1)[1].split('/', 1)[0])
            if any(ord(c) < 32 or ord(c) == 127 for c in value):
                return None
            parsed = urlsplit(value)
        if parsed.scheme not in {'https', 'http', 'mailto'}:
            return None
        if parsed.scheme != 'mailto' and (not parsed.hostname or parsed.username or parsed.password):
            return None
        return value.replace('(', '%28').replace(')', '%29').replace(' ', '%20').replace('<', '%3C').replace('>', '%3E').replace('\\', '%5C')
    except ValueError:
        return None


def convert(html: str, *, enforce_limit: bool = True) -> Conversion:
    if len(html.encode('utf-8')) > MAX_HTML_BYTES:
        raise ConversionError('source_too_large')
    # Optional dependency: ordinary Workbench startup never imports it.
    try:
        from bs4 import BeautifulSoup, NavigableString
    except ImportError:
        raise ConversionError('parser_unavailable') from None

    soup = BeautifulSoup(html, 'html.parser')
    if soup.body is None:
        raise ConversionError('missing_body')
    for tag in list(soup.find_all(['script', 'style', 'iframe', 'object', 'embed', 'form', 'noscript'])):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag.parent is None:
            continue
        style = re.sub(r'\s+', '', tag.get('style', '')).lower()
        if 'display:none' in style or 'visibility:hidden' in style or tag.has_attr('hidden'):
            tag.decompose()
    links = 0
    headings = 0

    def escape(text):
        text = re.sub(r'([\\\[\]*_`#])', r'\\\1', re.sub(r'\s+', ' ', text))
        return re.sub(r'<(?=[!/?A-Za-z])', r'\\<', text)

    def render(node):
        nonlocal links, headings
        if isinstance(node, NavigableString):
            # Preserve literal text rather than letting source markup inject Markdown.
            return escape(str(node))
        if node.name == 'br':
            return '\n'
        if node.name == 'img':
            return escape(node.get('alt', ''))
        text = ''.join(render(child) for child in node.children)
        if node.name == 'a':
            destination = safe_link(node.get('href', ''))
            if destination and text.strip():
                links += 1
                return '[' + text.strip() + '](' + destination + ')'
        if node.name in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            headings += 1
            return '\n\n## ' + text.strip().replace('**', '') + '\n\n'
        if node.name in {'strong', 'b'} and text.strip():
            if text.strip().startswith('## '):
                return '\n\n' + text.strip() + '\n\n'
            return '**' + text.strip() + '**'
        if node.name in {'td', 'div', 'p', 'li', 'blockquote', 'pre'}:
            return '\n\n' + text.strip() + '\n\n'
        return text

    body = render(soup.body)
    body = re.sub(r'[ \t]*\n[ \t]*', '\n', body)
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    body = re.sub(r'(?m)^(>+)', lambda match: ''.join('\\' + c for c in match[0]), body)
    description = HEADER + body
    # Count UTF-16 units conservatively too, because clients use JS strings.
    if enforce_limit and len(description.encode('utf-16-le')) // 2 > MAX_DESCRIPTION:
        raise ConversionError('description_limit_exceeded')
    if not body:
        raise ConversionError('empty_body')
    return Conversion(description, ' '.join(soup.body.stripped_strings), links, headings)


def source_matches(task: dict, comment: dict, html: str, project_id: str) -> bool:
    """Require exact resource identity and a recognizable routed newsletter.

    This is routing evidence, not cryptographic sender authentication. No source
    instructions, title changes, assignments, or links can expand this scope.
    """
    if task.get('projectId', task.get('project_id')) != project_id:
        return False
    if task.get('checked', task.get('is_completed', False)) or task.get('description', ''):
        return False
    if comment.get('taskId', comment.get('task_id')) != task.get('id'):
        return False
    attachment = comment.get('fileAttachment', comment.get('attachment', {})) or {}
    title = task.get('content', '')
    if attachment.get('fileName', attachment.get('file_name')) != title:
        return False
    if attachment.get('fileType', attachment.get('file_type')) != 'text/html':
        return False
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    headings = [h.get_text(' ', strip=True) for h in soup.select('h1,h2')]
    if not any(re.fullmatch(r'TLDR(?: [A-Za-z]+)? \d{4}-\d{2}-\d{2}', h) for h in headings):
        return False
    # A direct email forward may omit the Gmail envelope. Its campaign link and
    # exact attachment/title are required; ordinary project notes cannot qualify.
    return any(urlsplit(link.get('href', '')).hostname in {'tldr.tech', 'tracking.tldrnewsletter.com', 'a.tldrnewsletter.com'}
               for link in soup.select('a[href]'))
