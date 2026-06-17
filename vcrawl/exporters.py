import json
import os
import csv


def export_jsonl(store, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    rows = store.list_videos(limit=100000)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    return len(rows)


def export_csv(store, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    rows = store.list_videos(limit=100000)
    fieldnames = [
        "page_url",
        "media_url",
        "kind",
        "title",
        "source",
        "download_status",
        "output_path",
        "download_error",
    ]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})
    return len(rows)
