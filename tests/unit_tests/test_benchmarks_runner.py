from benchmarks.runner import measure, measure_seed


def test_measure_excludes_warmup_and_counts_iterations():
    calls = {"n": 0}

    def call():
        calls["n"] += 1

    samples = measure(call, warmup=2, iterations=3)
    assert calls["n"] == 5  # warmup + iterations were all invoked
    assert len(samples) == 3  # only the post-warmup samples are recorded
    assert all(isinstance(s, float) for s in samples)


class _FakeClient:
    def __init__(self):
        self.created = 0
        self.seeded = 0
        self.deleted = 0

    def create_session(self, base_image):
        self.created += 1
        return f"sid-{self.created}"

    def seed(self, session_id, archive_bytes):
        self.seeded += 1

    def delete_session(self, session_id, *, force=True):
        self.deleted += 1


def test_measure_seed_uses_fresh_session_each_iteration():
    fake = _FakeClient()
    samples = measure_seed(fake, "img", b"archive", warmup=1, iterations=4)
    assert len(samples) == 4
    assert fake.created == 5  # warmup + iterations
    assert fake.seeded == 5
    assert fake.deleted == 5  # every session force-deleted
