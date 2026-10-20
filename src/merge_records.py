def merge_records(existing, incoming, key):
    """Объединяем старые и новые записи без дублей."""
    indexed = {key(row): row for row in existing}

    for row in incoming:
        indexed[key(row)] = row

    return list(indexed.values())