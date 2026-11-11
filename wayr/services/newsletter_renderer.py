"""Render a NewsletterIssue to HTML with Jinja2.

Autoescape is always on — titles and blurbs come from third-party feeds and
from a model. A custom template (NEWSLETTER_TEMPLATE_FILE) is tried first;
if it is missing or broken the packaged default renders instead and the
failure is logged as an ERROR, because the send must still go out.
"""

import re
from pathlib import Path
from typing import Any, Dict, Optional

from jinja2 import Environment, FileSystemLoader, TemplateError
from markupsafe import Markup, escape

from wayr.models.newsletter import NewsletterIssue
from wayr.utils import get_logger

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
DEFAULT_TEMPLATE = "newsletter_default.html"
EMAIL_TEMPLATE = "email_default.html"


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def prose(text: Optional[str]) -> Markup:
    """Escape, then allow the two things a voice guide may ask for in running
    text: **bold** and paragraph breaks. Nothing else is interpreted."""
    if not text:
        return Markup("")
    safe = str(escape(text))
    safe = _BOLD.sub(r"<strong>\1</strong>", safe)
    paragraphs = [p.strip().replace("\n", "<br>") for p in safe.split("\n\n")]
    return Markup("<br><br>".join(p for p in paragraphs if p))


class NewsletterRenderer:
    def __init__(self, template_file: Optional[str] = None):
        self.custom: Optional[Path] = Path(template_file) if template_file else None
        search = [str(TEMPLATES_DIR)]
        if self.custom is not None:
            search.insert(0, str(self.custom.parent))
        self.env = Environment(
            loader=FileSystemLoader(search),
            autoescape=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["prose"] = prose
        if self.custom is not None and not self.custom.is_file():
            logger.error(
                f"NEWSLETTER_TEMPLATE_FILE {self.custom} does not exist; "
                f"the packaged default will be used"
            )
            self.custom = None

    @staticmethod
    def context(issue: NewsletterIssue) -> Dict[str, Any]:
        return {
            "issue": issue,
            "newsletter_title": issue.title,
            "subject_line": issue.subject_line,
            "intro": issue.intro,
            "date": issue.date_label,
            "sections": issue.sections,
            "story_count": issue.story_count,
            "platform_name": issue.platform_name,
            "ai_disclosure_notice": issue.ai_disclosure_notice,
            "generated_at": issue.generated_at,
            "llm_generated": issue.llm_generated,
        }

    def render(self, issue: NewsletterIssue) -> str:
        ctx = self.context(issue)
        if self.custom is not None:
            try:
                return self.env.get_template(self.custom.name).render(ctx)
            except (TemplateError, OSError) as e:
                logger.error(
                    f"Custom template {self.custom} failed ({e}); "
                    f"falling back to the packaged default"
                )
        return self.env.get_template(DEFAULT_TEMPLATE).render(ctx)

    def render_email(
        self,
        issue: NewsletterIssue,
        body_html: str,
        *,
        web_url: Optional[str],
        unsubscribe_url: str,
    ) -> str:
        """Wrap an already-rendered issue for the inbox: a full HTML document with
        a read-on-the-web link and the mailer's unsubscribe placeholder."""
        ctx = self.context(issue)
        ctx.update(
            body_html=Markup(body_html),  # rendered by us, already escaped
            web_url=web_url,
            unsubscribe_url=unsubscribe_url,
        )
        return self.env.get_template(EMAIL_TEMPLATE).render(ctx)
