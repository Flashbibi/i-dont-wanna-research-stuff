from app.database import PostgresRepository


class Result:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM bom_line WHERE id" in normalized:
            return Result(one={"id": 1, "job_id": 1, "suchtext": "Servo", "menge": 1})
        if "lower(%s)" in normalized:
            raise RuntimeError("PostgreSQL would infer the placeholder as bytea")
        return Result(many=[])


def test_check_line_explicitly_casts_case_insensitive_query_parameters_to_text():
    repository = PostgresRepository("unused")
    repository._connect = lambda: Connection()

    checked = repository.check_line(1)

    assert checked["stock"] == []
    assert checked["previous_purchases"] == []
    assert checked["cached_offers"] == []
