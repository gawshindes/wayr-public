"""Mirror writes go out in chunks and survive one transient failure."""

from unittest.mock import MagicMock

from wayr.services.supabase_store import UPSERT_CHUNK, SupabasePipelineStore


def _store_with_client():
    store = SupabasePipelineStore.__new__(SupabasePipelineStore)
    client = MagicMock()
    store.client = client
    store.region = None
    return store, client


def test_rows_are_chunked():
    store, client = _store_with_client()
    rows = [{"url_hash": str(i)} for i in range(UPSERT_CHUNK * 2 + 5)]
    store._upsert(rows)
    calls = client.table.return_value.upsert.call_args_list
    assert [len(c.args[0]) for c in calls] == [UPSERT_CHUNK, UPSERT_CHUNK, 5]


def test_transient_failure_is_retried_once_per_chunk():
    store, client = _store_with_client()
    execute = client.table.return_value.upsert.return_value.execute
    execute.side_effect = [Exception("sslv3 alert bad record mac"), None]
    store._upsert([{"url_hash": "a"}])
    assert execute.call_count == 2


def test_missing_column_stops_immediately():
    store, client = _store_with_client()
    execute = client.table.return_value.upsert.return_value.execute
    execute.side_effect = Exception("column source_origin does not exist")
    store._upsert([{"url_hash": str(i)} for i in range(UPSERT_CHUNK * 3)])
    assert execute.call_count == 1
