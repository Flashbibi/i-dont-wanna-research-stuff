from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .jobs import BomInputLine


class PostgresRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def create_job(self, source_text: str, lines: list[BomInputLine]) -> int:
        with self._connect() as connection:
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
                        (job_id, line.position, line.originaltext, line.suchtext, line.menge),
                    )
        return job_id

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
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

    def next_job(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            job = connection.execute(
                """
                SELECT j.id, j.status, j.quelltext, j.erstellt_am
                FROM job j
                WHERE j.status IN ('offen', 'in_arbeit')
                  AND EXISTS (
                    SELECT 1 FROM bom_line bl
                    WHERE bl.job_id = j.id AND bl.status IN ('offen', 'in_arbeit')
                  )
                ORDER BY j.erstellt_am, j.id
                LIMIT 1
                """
            ).fetchone()
            if job is None:
                return None
            lines = connection.execute(
                """
                SELECT id, position, originaltext, suchtext, menge, status, kommentar
                FROM bom_line
                WHERE job_id = %s AND status IN ('offen', 'in_arbeit')
                ORDER BY position
                """,
                (job["id"],),
            ).fetchall()
            result = dict(job)
            result["lines"] = [dict(line) for line in lines]
            return result

    def get_line(self, line_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, job_id, position, originaltext, suchtext, menge, status, kommentar
                FROM bom_line WHERE id = %s
                """,
                (line_id,),
            ).fetchone()
            return dict(row) if row else None

    def check_line(self, line_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            line = connection.execute(
                """
                SELECT id, job_id, position, originaltext, suchtext, menge, status, kommentar
                FROM bom_line WHERE id = %s
                """,
                (line_id,),
            ).fetchone()
            if line is None:
                return None
            stock = connection.execute(
                """
                SELECT id, bezeichnung, menge, einheit, aktualisiert_am
                FROM stock
                WHERE menge > 0 AND lower(bezeichnung) = lower(%s)
                ORDER BY aktualisiert_am DESC
                """,
                (line["suchtext"],),
            ).fetchall()
            previous = connection.execute(
                """
                SELECT p.id AS purchase_id, p.bestellt_am, p.angekommen_am,
                       pi.menge, pi.einzelpreis_chf, o.produktname, o.produkt_url,
                       s.id AS shop_id, s.name AS shop_name
                FROM purchase_item pi
                JOIN purchase p ON p.id = pi.purchase_id
                JOIN bom_line old_line ON old_line.id = pi.line_id
                JOIN offer o ON o.id = pi.offer_id
                JOIN shop s ON s.id = o.shop_id
                WHERE lower(old_line.suchtext) = lower(%s)
                ORDER BY p.bestellt_am DESC
                LIMIT 10
                """,
                (line["suchtext"],),
            ).fetchall()
            cached = connection.execute(
                """
                SELECT o.id, o.produktname, o.produkt_url, o.preis_chf,
                       o.lieferzeit_tage, o.lager, o.gesehen_am,
                       s.id AS shop_id, s.name AS shop_name, s.status AS shop_status
                FROM offer o
                JOIN bom_line cached_line ON cached_line.id = o.line_id
                JOIN shop s ON s.id = o.shop_id
                WHERE lower(cached_line.suchtext) = lower(%s)
                  AND o.gesehen_am >= NOW() - INTERVAL '14 days'
                  AND s.status <> 'gesperrt'
                ORDER BY o.gesehen_am DESC
                LIMIT 20
                """,
                (line["suchtext"],),
            ).fetchall()
            return {
                "line": dict(line),
                "stock": [dict(row) for row in stock],
                "previous_purchases": [dict(row) for row in previous],
                "cached_offers": [dict(row) for row in cached],
            }

    def create_shop(self, **values: Any) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    INSERT INTO shop(
                        name, url, domain, land, versand_chf, gratis_ab_chf,
                        mindestbestellwert_chf, lieferzeit_default_tage
                    ) VALUES (%(name)s, %(url)s, %(domain)s, %(land)s,
                              %(versand_chf)s, %(gratis_ab_chf)s,
                              %(mindestbestellwert_chf)s, %(lieferzeit_default_tage)s)
                    RETURNING *
                    """,
                    values,
                ).fetchone()
                return dict(row)
        except UniqueViolation as error:
            raise ValueError("Shop-Name oder Domain ist bereits bekannt") from error

    def get_shop(self, shop_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM shop WHERE id = %s", (shop_id,)).fetchone()
            return dict(row) if row else None

    def create_offer(self, **values: Any) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        INSERT INTO offer(
                            line_id, shop_id, produktname, produkt_url, quelle_url,
                            preis_chf, lieferzeit_tage, lager
                        ) VALUES (%(line_id)s, %(shop_id)s, %(produktname)s,
                                  %(produkt_url)s, %(quelle_url)s, %(preis_chf)s,
                                  %(lieferzeit_tage)s, %(lager)s)
                        RETURNING *
                        """,
                        values,
                    ).fetchone()
                    connection.execute(
                        "UPDATE bom_line SET status = 'kandidaten' WHERE id = %s",
                        (values["line_id"],),
                    )
                    connection.execute(
                        """
                        UPDATE job SET status = 'in_arbeit', aktualisiert_am = NOW()
                        WHERE id = (SELECT job_id FROM bom_line WHERE id = %s)
                        """,
                        (values["line_id"],),
                    )
                    return dict(row)
        except UniqueViolation as error:
            raise ValueError("Dieses Angebot ist fuer die Zeile bereits erfasst") from error

    def mark_line(self, line_id: int, status: str, kommentar: str | None) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE bom_line SET status = %s, kommentar = %s
                WHERE id = %s RETURNING id, status, kommentar
                """,
                (status, kommentar, line_id),
            ).fetchone()
            return dict(row)

    def optimization_input(self, job_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            offers = connection.execute(
                """
                SELECT o.id, o.line_id, o.shop_id, o.preis_chf,
                       o.lieferzeit_tage, bl.menge
                FROM offer o
                JOIN bom_line bl ON bl.id = o.line_id
                JOIN decision d ON d.offer_id = o.id AND d.status = 'bestaetigt'
                JOIN shop s ON s.id = o.shop_id
                WHERE bl.job_id = %s AND s.status <> 'gesperrt'
                ORDER BY o.id
                """,
                (job_id,),
            ).fetchall()
            shop_ids = sorted({row["shop_id"] for row in offers})
            if not shop_ids:
                return {"offers": [], "shops": []}
            shops = connection.execute(
                """
                SELECT id, name, versand_chf, gratis_ab_chf,
                       mindestbestellwert_chf, lieferzeit_default_tage
                FROM shop WHERE id = ANY(%s)
                """,
                (shop_ids,),
            ).fetchall()
            return {
                "offers": [dict(row) for row in offers],
                "shops": [dict(row) for row in shops],
            }

    def create_purchase(
        self,
        job_id: int,
        variant: dict[str, Any],
        ordered_at: datetime,
        promised_days: dict[str, int],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            with connection.transaction():
                existing = connection.execute(
                    "SELECT id FROM purchase WHERE job_id = %s",
                    (job_id,),
                ).fetchone()
                if existing:
                    raise ValueError(f"Job {job_id} wurde bereits als Kauf erfasst")
                purchase = connection.execute(
                    """
                    INSERT INTO purchase(
                        job_id, variante, total_chf, bestellt_am, zugesagt_liefertage
                    ) VALUES (%s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        job_id,
                        Jsonb(variant),
                        Decimal(variant["total_chf"]),
                        ordered_at,
                        Jsonb(promised_days),
                    ),
                ).fetchone()
                for line_id_text, offer_id in variant["assignments"].items():
                    line_id = int(line_id_text)
                    item = connection.execute(
                        """
                        SELECT bl.menge, o.preis_chf
                        FROM bom_line bl JOIN offer o ON o.id = %s AND o.line_id = bl.id
                        WHERE bl.id = %s AND bl.job_id = %s
                        """,
                        (offer_id, line_id, job_id),
                    ).fetchone()
                    if item is None:
                        raise ValueError("Variante enthaelt ein ungueltiges Angebot")
                    connection.execute(
                        """
                        INSERT INTO purchase_item(
                            purchase_id, line_id, offer_id, menge, einzelpreis_chf
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (purchase["id"], line_id, offer_id, item["menge"], item["preis_chf"]),
                    )
                connection.execute(
                    "UPDATE job SET status = 'bestellt', aktualisiert_am = NOW() WHERE id = %s",
                    (job_id,),
                )
                return dict(purchase)
