"""Pure selection logic for the review app (no I/O, fixture-testable).

Two jobs: turn a stored application + the master résumé into the checkbox model
the UI shows (AI-recommended bullets prechecked, everything else offered), and
turn the human's checked ids back into the priority-ordered ``TailoredBullet``
list the renderer consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import yaml

from ..models import Application
from ..resume.loader import all_bullets
from ..resume.models import BulletRef, MasterResume
from ..resume.tailoring import TailoredBullet, emphasize


def _normalize(text: str) -> str:
    """Bullet text normalized for matching: Markdown bold and whitespace removed."""
    return " ".join((text or "").replace("**", "").split()).lower()


def _recommendation_map(app: Application, refs: list[BulletRef]) -> dict[str, tuple[int, str]]:
    """``{bullet id: (priority order, tailored text)}`` for the AI's recommendation.

    Primary source: ``app.recommended_bullets`` (stored by ``match_and_slice``).
    Fallback for applications that predate that field: match the stored CV YAML's
    highlight texts back to master bullets by normalized text — rephrased bullets
    that no longer match are simply not prechecked.
    """
    rec: dict[str, tuple[int, str]] = {}
    known = {ref.id for ref in refs}
    for i, item in enumerate(app.recommended_bullets):
        rid = str(item.get("id", ""))
        if rid in known and rid not in rec:
            rec[rid] = (i, str(item.get("text") or ""))
    if rec:
        return rec

    try:
        doc = yaml.safe_load(app.tailored_resume_yaml or "") or {}
    except yaml.YAMLError:
        return {}
    by_text = {_normalize(ref.text): ref.id for ref in refs}
    sections = doc.get("cv", {}).get("sections", {}) if isinstance(doc, dict) else {}
    order = 0
    for section in ("experience", "projects"):
        for entry in sections.get(section) or []:
            for highlight in entry.get("highlights") or []:
                rid = by_text.get(_normalize(str(highlight)))
                if rid and rid not in rec:
                    rec[rid] = (order, str(highlight))
                    order += 1
    return rec


@dataclass
class BulletOption:
    """One checkbox in the review UI."""

    id: str
    text: str  # the text that will render if kept (tailored when recommended)
    recommended: bool = False
    order: Optional[int] = None  # priority position within the recommendation


@dataclass
class EntryOptions:
    """One experience/project block in the review UI."""

    source: str  # "experience" | "project"
    title: str
    subtitle: str = ""
    bullets: list[BulletOption] = field(default_factory=list)


def entry_options(resume: MasterResume, app: Application) -> list[EntryOptions]:
    """The full experience/project checkbox model, in master-résumé order."""
    refs = all_bullets(resume)
    rec = _recommendation_map(app, refs)

    entries: dict[tuple[str, int], EntryOptions] = {}
    for source, items in (("experience", resume.experiences), ("project", resume.projects)):
        for idx, item in enumerate(items):
            if source == "experience":
                title = f"{item.role} — {item.company}"
                dates = " – ".join(d for d in (item.start_date, item.end_date) if d)
            else:
                title = item.name
                dates = " – ".join(d for d in (item.start_date, item.end_date) if d)
            entries[(source, idx)] = EntryOptions(source=source, title=title, subtitle=dates)

    for ref in refs:
        recommended = ref.id in rec
        order, text = rec.get(ref.id, (None, ""))
        entries[(ref.source, ref.parent_index)].bullets.append(
            BulletOption(
                id=ref.id,
                text=text or ref.text,
                recommended=recommended,
                order=order,
            )
        )
    # Master order: all experiences, then all projects (entries with no bullets in
    # the master résumé are dropped — nothing to choose).
    ordered = [entries[k] for k in sorted(entries, key=lambda k: (k[0] == "project", k[1]))]
    return [e for e in ordered if e.bullets]


def selection_to_bullets(
    resume: MasterResume, app: Application, selected_ids: list[str]
) -> list[TailoredBullet]:
    """The human's checked ids → priority-ordered ``TailoredBullet`` list.

    Recommended bullets keep their tailored wording and recommendation order (they
    are what the render trims LAST), with master-résumé bold spans restored and
    JD-keyword bolding applied; bullets the human added beyond the recommendation
    follow in master order with the same emphasis post-pass.
    """
    refs = {ref.id: ref for ref in all_bullets(resume)}
    rec = _recommendation_map(app, list(refs.values()))
    chosen = [rid for rid in dict.fromkeys(selected_ids) if rid in refs]

    recommended = sorted((rid for rid in chosen if rid in rec), key=lambda rid: rec[rid][0])
    added = [rid for rid in chosen if rid not in rec]

    out: list[TailoredBullet] = []
    for rid in recommended:
        text = rec[rid][1] or refs[rid].text
        out.append(
            TailoredBullet(
                ref=refs[rid],
                text=emphasize(text, master_text=refs[rid].text, keywords=app.keywords),
            )
        )
    for rid in added:
        out.append(
            TailoredBullet(
                ref=refs[rid],
                text=emphasize(refs[rid].text, master_text=refs[rid].text, keywords=app.keywords),
            )
        )
    return out
