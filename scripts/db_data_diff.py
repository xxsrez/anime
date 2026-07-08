from dataclasses import dataclass
import sqlite3


CATALOG_TABLES = [
    "anime",
    "translations",
    "episodes",
    "anime_fields",
    "anime_genres",
    "anime_dubbings",
    "video_sources",
]

DELETE_TABLES = [
    "video_sources",
    "anime_fields",
    "anime_genres",
    "anime_dubbings",
    "episodes",
    "translations",
    "anime",
]

UPSERT_TABLES = CATALOG_TABLES


@dataclass(frozen=True)
class TableSpec:
    name: str
    key_columns: tuple[str, ...]
    conflict_columns: tuple[str, ...]
    omit_columns: tuple[str, ...] = ()


TABLE_SPECS = {
    "anime": TableSpec("anime", ("id",), ("id",)),
    "translations": TableSpec("translations", ("id",), ("id",)),
    "episodes": TableSpec("episodes", ("id",), ("id",)),
    "anime_fields": TableSpec("anime_fields", ("anime_id", "label"), ("anime_id", "label")),
    "anime_genres": TableSpec("anime_genres", ("anime_id", "genre"), ("anime_id", "genre")),
    "anime_dubbings": TableSpec("anime_dubbings", ("anime_id", "dubbing"), ("anime_id", "dubbing")),
    "video_sources": TableSpec(
        "video_sources",
        ("episode_id", "provider_id", "translation_id", "embed_url_redacted"),
        ("episode_id", "provider_id", "translation_id", "embed_url_redacted"),
        omit_columns=("id",),
    ),
}


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def table_columns(con, table):
    rows = con.execute(f"pragma table_info({quote_identifier(table)})").fetchall()
    if not rows:
        raise ValueError(f"missing table: {table}")
    return [row[1] for row in rows]


def row_key(row, key_columns):
    return tuple(row[column] for column in key_columns)


def load_table(con, spec):
    columns = table_columns(con, spec.name)
    order_by = ", ".join(quote_identifier(column) for column in spec.key_columns)
    rows = con.execute(
        f"select * from {quote_identifier(spec.name)} order by {order_by}"
    ).fetchall()
    return columns, {row_key(row, spec.key_columns): dict(row) for row in rows}


def comparable_row(row, columns, spec):
    omitted = set(spec.omit_columns)
    return tuple((column, row[column]) for column in columns if column not in omitted)


def where_key_clause(spec, key):
    parts = []
    for column, value in zip(spec.key_columns, key):
        quoted = quote_identifier(column)
        if value is None:
            parts.append(f"{quoted} is null")
        else:
            parts.append(f"{quoted} = {sql_literal(value)}")
    return " and ".join(parts)


def delete_statement(spec, key):
    return f"delete from {quote_identifier(spec.name)} where {where_key_clause(spec, key)};"


def upsert_statement(spec, columns, row):
    insert_columns = [column for column in columns if column not in spec.omit_columns]
    column_sql = ", ".join(quote_identifier(column) for column in insert_columns)
    value_sql = ", ".join(sql_literal(row[column]) for column in insert_columns)
    conflict_sql = ", ".join(quote_identifier(column) for column in spec.conflict_columns)
    update_columns = [column for column in insert_columns if column not in spec.conflict_columns]

    if not update_columns:
        return (
            f"insert or ignore into {quote_identifier(spec.name)} ({column_sql}) "
            f"values ({value_sql});"
        )

    set_sql = ", ".join(
        f"{quote_identifier(column)} = excluded.{quote_identifier(column)}"
        for column in update_columns
    )
    return (
        f"insert into {quote_identifier(spec.name)} ({column_sql}) values ({value_sql})\n"
        f"on conflict({conflict_sql}) do update set {set_sql};"
    )


def changed_rows(before_rows, after_rows, columns, spec):
    for key, after_row in after_rows.items():
        before_row = before_rows.get(key)
        if before_row is None:
            yield key, after_row
            continue
        if comparable_row(before_row, columns, spec) != comparable_row(after_row, columns, spec):
            yield key, after_row


def generate_data_migration_statements(before_db, after_db, tables=None):
    tables = tables or CATALOG_TABLES
    before = sqlite3.connect(before_db)
    after = sqlite3.connect(after_db)
    before.row_factory = sqlite3.Row
    after.row_factory = sqlite3.Row
    statements = []
    try:
        loaded = {}
        for table in tables:
            spec = TABLE_SPECS[table]
            before_columns, before_rows = load_table(before, spec)
            after_columns, after_rows = load_table(after, spec)
            if before_columns != after_columns:
                raise ValueError(f"schema mismatch for {table}")
            loaded[table] = (spec, before_columns, before_rows, after_rows)

        for table in DELETE_TABLES:
            if table not in loaded:
                continue
            spec, _, before_rows, after_rows = loaded[table]
            for key in sorted(set(before_rows) - set(after_rows)):
                statements.append(delete_statement(spec, key))

        for table in UPSERT_TABLES:
            if table not in loaded:
                continue
            spec, columns, before_rows, after_rows = loaded[table]
            for _, row in changed_rows(before_rows, after_rows, columns, spec):
                statements.append(upsert_statement(spec, columns, row))
    finally:
        before.close()
        after.close()

    return statements


def generate_data_migration_sql(before_db, after_db, tables=None):
    statements = generate_data_migration_statements(before_db, after_db, tables=tables)
    return "\n\n".join(statements) + ("\n" if statements else "")
