from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from .jobs import BomInputLine


class PostgresRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def create_job(self, source_text: str, lines: list[BomInputLine]) -> int:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            with connection.transaction():
                job = connection.execute(
                    "INSERT INTO job(quelltext) VALUES (%s) RETURNING id",
                    (source_text,),
                ).fetchone()
                job_id = int(job["id"])
                for line in lines:
                    connection.execute(
                        """
                        INSERT INTO bom_line(
                            job_id, position, originaltext, suchtext, menge
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            job_id,
                            line.position,
                            line.originaltext,
                            line.suchtext,
                            line.menge,
                        ),
                    )
        return job_id

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            job = connection.execute(
                """
                SELECT id, status, quelltext, erstellt_am, aktualisiert_am
                FROM job WHERE id = %s
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                return None
            lines = connection.execute(
                """
                SELECT id, position, originaltext, suchtext, menge, status, kommentar
                FROM bom_line WHERE job_id = %s ORDER BY position
                """,
                (job_id,),
            ).fetchall()
            result = dict(job)
            result["lines"] = [dict(line) for line in lines]
            return result
