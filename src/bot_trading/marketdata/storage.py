"""Stockage Parquet local, horodaté et traçable.

Arborescence (sous ``root``, par défaut ``data/store``, ignoré par git) ::

    <root>/<dataset>/year=<AAAA>.parquet   # partition par année de event_time
    <root>/_lineage.jsonl                  # une ligne par collecte (Provenance)

- Écriture atomique (fichier temporaire puis ``rename``).
- Dédoublonnage sur des colonnes clés : en cas de doublon, la ligne la plus
  récemment collectée (``fetched_at``) gagne.
- Lecture point-in-time : ``read(dataset, as_of=t)`` ne renvoie que les lignes
  dont ``available_at <= t``.

``*.parquet`` et ``data/`` sont ignorés par git : les données (dont certaines
sous licence) ne sont jamais committées.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from bot_trading.marketdata.common import Provenance, ensure_utc

REQUIRED_COLUMNS = ("event_time", "available_at", "source", "fetched_at", "ingest_id")
_DATASET_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,120}$")


@dataclass(frozen=True)
class WriteResult:
    dataset: str
    rows_in: int
    rows_new: int
    rows_total: int
    ingest_id: str


def _check_dataset(name: str) -> str:
    if not _DATASET_NAME.match(name):
        raise ValueError(f"Nom de dataset invalide : {name!r}")
    return name


def _validate(records: Sequence[dict]) -> None:
    for r in records:
        missing = [c for c in REQUIRED_COLUMNS if r.get(c) is None]
        if missing:
            raise ValueError(f"Colonnes obligatoires manquantes {missing} dans {r}")
        for c in ("event_time", "available_at", "fetched_at"):
            ensure_utc(r[c])
        if r["available_at"] < r["event_time"] and not r.get("allow_available_before_event", False):
            # Une donnée ne peut pas être connue avant l'instant qu'elle décrit,
            # sauf calendriers futurs (dates de publication annoncées), explicitement marqués.
            raise ValueError(f"available_at < event_time : {r}")


class ParquetStore:
    def __init__(self, root: str | Path = "data/store") -> None:
        self.root = Path(root)

    # ------------------------------------------------------------- écriture
    def write(self, dataset: str, records: Sequence[dict], keys: Sequence[str], provenance: Provenance) -> WriteResult:
        _check_dataset(dataset)
        stamped = [provenance.stamp(r) for r in records]
        _validate(stamped)
        by_year: dict[int, list[dict]] = {}
        for r in stamped:
            by_year.setdefault(r["event_time"].year, []).append(r)
        new_total = 0
        for year, rows in by_year.items():
            new_total += self._merge_partition(dataset, year, rows, keys)
        self._append_lineage(provenance, dataset, len(records), new_total)
        return WriteResult(dataset, len(records), new_total, self.count(dataset), provenance.ingest_id)

    def _merge_partition(self, dataset: str, year: int, rows: list[dict], keys: Sequence[str]) -> int:
        path = self.root / dataset / f"year={year}.parquet"
        existing: list[dict] = pq.read_table(path).to_pylist() if path.exists() else []
        merged: dict[tuple, dict] = {}
        for r in existing:
            merged[tuple(r.get(k) for k in keys)] = r
        before = set(merged)
        for r in rows:
            k = tuple(r.get(k) for k in keys)
            old = merged.get(k)
            if old is None or r["fetched_at"] >= old["fetched_at"]:
                merged[k] = {**(old or {}), **r}
        new_keys = len(set(merged) - before)
        table = pa.Table.from_pylist(sorted(merged.values(), key=lambda r: (r["event_time"], r["available_at"])))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        os.close(fd)
        try:
            pq.write_table(table, tmp)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return new_keys

    def _append_lineage(self, prov: Provenance, dataset: str, rows_in: int, rows_new: int) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        entry = json.loads(prov.to_json())
        entry.update({"stored_dataset": dataset, "rows_in": rows_in, "rows_new": rows_new})
        with open(self.root / "_lineage.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    # --------------------------------------------------------------- lecture
    def datasets(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def _files(self, dataset: str) -> list[Path]:
        d = self.root / _check_dataset(dataset)
        return sorted(d.glob("year=*.parquet")) if d.exists() else []

    def read_table(
        self,
        dataset: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        as_of: datetime | None = None,
        filters: dict | None = None,
    ) -> pa.Table:
        files = self._files(dataset)
        if start is not None:
            files = [f for f in files if int(f.stem.split("=")[1]) >= ensure_utc(start).year]
        if end is not None:
            files = [f for f in files if int(f.stem.split("=")[1]) <= ensure_utc(end).year]
        if not files:
            return pa.table({})
        table = pa.concat_tables([pq.read_table(f) for f in files], promote_options="default")
        mask = None

        def _and(m, cond):
            return cond if m is None else pc.and_(m, cond)

        if start is not None:
            mask = _and(mask, pc.greater_equal(table["event_time"], pa.scalar(ensure_utc(start), table.schema.field("event_time").type)))
        if end is not None:
            mask = _and(mask, pc.less(table["event_time"], pa.scalar(ensure_utc(end), table.schema.field("event_time").type)))
        if as_of is not None:
            mask = _and(mask, pc.less_equal(table["available_at"], pa.scalar(ensure_utc(as_of), table.schema.field("available_at").type)))
        for col, val in (filters or {}).items():
            mask = _and(mask, pc.equal(table[col], val))
        if mask is not None:
            table = table.filter(mask)
        return table.sort_by([("event_time", "ascending"), ("available_at", "ascending")])

    def read(self, dataset: str, **kwargs) -> list[dict]:
        return self.read_table(dataset, **kwargs).to_pylist()

    def count(self, dataset: str) -> int:
        return sum(pq.ParquetFile(f).metadata.num_rows for f in self._files(dataset))

    def last(self, dataset: str, column: str = "available_at", filters: dict | None = None) -> datetime | None:
        t = self.read_table(dataset, filters=filters)
        if t.num_rows == 0:
            return None
        return pc.max(t[column]).as_py()

    def lineage(self, dataset: str | None = None) -> list[dict]:
        path = self.root / "_lineage.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if dataset is None or r.get("stored_dataset") == dataset]


def iter_records(table: pa.Table) -> Iterable[dict]:
    yield from table.to_pylist()
