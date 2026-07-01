"""Phase 1 sourcing: pull public ATS JSON feeds + SimplifyJobs (+ optional JSearch)
and normalize everything into ``Job``.

Public entrypoints used by the ``source`` stage::

    from .companies import CompanyTarget, load_companies
    from .http import build_client
    from .ats import fetch_company
    from .simplify import fetch_simplify
    from .jsearch import fetch_jsearch
"""
