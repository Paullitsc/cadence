"""Pipeline stages.

Each stage module exposes ``NAME: str`` and ``run(ctx: StageContext) -> StageResult``,
is idempotent, and is independently runnable::

    python -m internship_pipeline.stages.source
"""
