from firecloud import fiss

from terra_notebook_utils import WORKSPACE_GOOGLE_PROJECT, WORKSPACE_NAME

def _iter_table(table: str):
    for item in fiss.fapi.get_entities(WORKSPACE_GOOGLE_PROJECT, WORKSPACE_NAME, table).json():
        yield item

def _get_item_val(item: dict, key: str):
    if "name" == key:
        return item['name']
    else:
        return item['attributes'][key]

def fetch_attribute(table: str, filter_column: str, filter_val: str, attribute: str):
    """
    Fetch `attribute` from `table` from the same row containing `filter_val` in column `filter_column`
    """
    for item in _iter_table(table):
        if filter_val == _get_item_val(item, filter_column):
            return _get_item_val(item, attribute)
    else:
        raise ValueError(f"No row found for table {table}, filter_column {filter_column} filter_val {filter_val}")

def fetch_object_id(table: str, file_name: str):
    """
    Fetch `object_id` associated with `file_name` from `table`.
    DRS urls, when available, are stored in `object_id`.
    """
    return fetch_attribute(table, "file_name", file_name, "object_id")

def fetch_drs_url(table: str, file_name: str):
    val = fetch_object_id(table, file_name)
    if not val.startswith("drs://"):
        raise ValueError(f"Expected DRS url in {table} for {file_name}, got {val} instead.")
    return val

def print_column(table: str, column: str):
    for item in _iter_table(table):
        print(_get_item_val(item, column))