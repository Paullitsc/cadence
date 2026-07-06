"""Phase 5: CV grouping (clustering on synthetic embeddings) + cache-key stability."""

from __future__ import annotations

from internship_pipeline.resume.grouping import cluster_jobs, cv_cache_key, keyword_overlap


def test_identical_jobs_share_one_cluster():
    vec = [1.0, 0.0, 0.0]
    clusters = cluster_jobs([vec, vec, vec], [["python", "aws"]] * 3)
    assert len(clusters) == 1
    assert clusters[0].representative == 0
    assert clusters[0].members == [0, 1, 2]


def test_dissimilar_vectors_stay_apart():
    clusters = cluster_jobs(
        [[1.0, 0.0], [0.0, 1.0]], [["python"], ["python"]], similarity_threshold=0.9
    )
    assert len(clusters) == 2


def test_keyword_sanity_check_blocks_similar_vectors_with_different_requirements():
    vec = [1.0, 0.0]
    clusters = cluster_jobs(
        [vec, vec],
        [["python", "django"], ["rust", "kubernetes"]],
        similarity_threshold=0.9,
        keyword_overlap_threshold=0.5,
    )
    assert len(clusters) == 2  # embeddings agree but the hard requirements don't


def test_representative_is_first_best_fit_member():
    # Inputs arrive best-fit-first; a later similar job joins the earlier cluster.
    a, b = [1.0, 0.0, 0.0], [0.98, 0.02, 0.0]
    clusters = cluster_jobs(
        [a, b], [["python", "aws"], ["python", "aws"]], similarity_threshold=0.9
    )
    assert len(clusters) == 1
    assert clusters[0].representative == 0


def test_empty_vectors_never_group():
    clusters = cluster_jobs([[], []], [["python"], ["python"]])
    assert len(clusters) == 2


def test_keyword_overlap_bounds():
    assert keyword_overlap([], ["x"]) == 0.0
    assert keyword_overlap(["A", "b"], ["a", "B"]) == 1.0
    assert keyword_overlap(["a", "b"], ["b", "c"]) == 1 / 3


def test_cache_key_stable_across_ordering_and_case():
    k1 = cv_cache_key(["b2", "b1"], ["Python", "AWS"])
    k2 = cv_cache_key(["b1", "b2"], ["aws", "python"])
    assert k1 == k2


def test_cache_key_changes_with_inputs():
    base = cv_cache_key(["b1"], ["python"])
    assert cv_cache_key(["b1", "b2"], ["python"]) != base
    assert cv_cache_key(["b1"], ["python", "aws"]) != base
