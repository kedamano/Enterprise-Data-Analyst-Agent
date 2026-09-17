"""临时探针：验证 Milvus Lite 在指定路径下能否**持久化**（跨进程）。

用法：
    python _milvus_probe.py write <path>
    python _milvus_probe.py read  <path>
"""
import os
import sys

mode, path = sys.argv[1], sys.argv[2]
os.environ["MILVUS_LITE_PATH"] = path

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
from app.infrastructure.vectorstore.milvus import get_client  # noqa: E402

COLL = "probe_persist"
DIM = 8

c = get_client()
if c is None:
    print("RESULT=NO_CLIENT")
    sys.exit(0)

try:
    if mode == "write":
        if c.has_collection(COLL):
            c.drop_collection(COLL)
        from pymilvus import DataType

        schema = c.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field("pk", DataType.INT64, is_primary=True)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=DIM)
        c.create_collection(COLL, schema=schema)
        c.insert(COLL, [{"vector": [0.9] + [0.1] * (DIM - 1), "source": "持久化探针文档A"}])
        c.insert(COLL, [{"vector": [0.1] * (DIM - 1) + [0.9], "source": "持久化探针文档B"}])
        n = c.query(COLL, filter="", output_fields=["source"])
        print("RESULT=WROTE rows=%d" % len(n))
    else:
        if not c.has_collection(COLL):
            print("RESULT=LOST collection不存在（持久化失败）")
            sys.exit(0)
        r = c.search(COLL, [[0.9] + [0.1] * (DIM - 1)], limit=1, output_fields=["source"])
        top = r[0][0]["entity"]["source"] if r and r[0] else "?"
        print("RESULT=READ top=%s" % top)
except Exception as e:
    print("RESULT=ERROR %s %s" % (type(e).__name__, str(e)[:200]))
