# Vector search on glove-100-angular (1.18M x 100): build time, latency, recall@10 per engine.
# .venv/bin/python run.py <arm> [--queries N] [--knob value ...]
#   arms: pgvector, sqlitevec, lancedb, duckdb
#   knob (search-time setting for the tuned point): pgvector ef_search, lancedb nprobes[:refine_factor], duckdb ef_search
#   --train N: smoke test on the first N vectors (own work dir, exact neighbours computed here)
# One engine at a time; each run appends a line to results.jsonl. An engine is reused only if its index exists,
# so a build that died half way is redone instead of measured as a full scan.
# pgvector runs in Docker: docker run -d --name pgbench -e POSTGRES_PASSWORD=bench -p 5433:5432 --shm-size=2g pgvector/pgvector:pg17
import argparse, json, os, platform, shutil, statistics as st, sys, time
import h5py, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BENCH_DATA", os.path.join(HERE, "data", "glove-100-angular.hdf5"))
WORK = os.environ.get("BENCH_WORK", os.path.join(HERE, "data", "work"))
K = 10


def load(nq, ntrain=None):
    with h5py.File(DATA, "r") as f:
        train = np.asarray(f["train"][:ntrain] if ntrain else f["train"], dtype=np.float32)
        test = np.asarray(f["test"][:nq], dtype=np.float32)
        truth = np.asarray(f["neighbors"][:nq, :K])
    # angular distance = cosine on unit vectors; normalise once so every engine sees the same data
    train /= np.linalg.norm(train, axis=1, keepdims=True) + 1e-12
    test /= np.linalg.norm(test, axis=1, keepdims=True) + 1e-12
    if ntrain:  # the file's neighbours are for the full set
        truth = np.argsort(-(test @ train.T), axis=1)[:, :K]
    return train, test, truth


def recall(found, truth):
    return float(np.mean([len(set(f) & set(t)) / K for f, t in zip(found, truth)]))


# --- engines: build(train) -> (seconds, size_bytes); query(q) -> ids; close() ---

class PgVector:
    name = "pgvector"
    def __init__(self, knob):
        import psycopg
        from pgvector.psycopg import register_vector
        self.c = psycopg.connect("host=localhost port=5433 user=postgres password=bench dbname=postgres", autocommit=True)
        self.c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self.c)
        if knob: self.c.execute(f"SET hnsw.ef_search = {int(knob)}")
    def build(self, train):
        c = self.c
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        if c.execute("SELECT count(*) FROM pg_indexes WHERE tablename = 'items' AND indexdef LIKE '%ef_construction=''128''%'").fetchone()[0] \
                and c.execute("SELECT count(*) FROM items").fetchone()[0] == len(train):  # smoke runs share this database
            return None  # already built; reuse it for the tuned point
        c.execute("DROP TABLE IF EXISTS items")
        c.execute("CREATE TABLE items (id int, emb vector(100))")
        t0 = time.perf_counter()
        with c.cursor().copy("COPY items (id, emb) FROM STDIN WITH (FORMAT BINARY)") as cp:
            cp.set_types(["int4", "vector"])
            for i, v in enumerate(train): cp.write_row((i, v))
        c.execute("SET maintenance_work_mem = '1536MB'")  # pgvector README: the graph should fit in memory while building
        # same graph settings as DuckDB vss's defaults (pgvector's own default ef_construction is 64)
        c.execute("CREATE INDEX ON items USING hnsw (emb vector_cosine_ops) WITH (m = 16, ef_construction = 128)")
        secs = time.perf_counter() - t0
        size = c.execute("SELECT pg_total_relation_size('items')").fetchone()[0]
        return secs, size
    def query(self, q):
        return [r[0] for r in self.c.execute("SELECT id FROM items ORDER BY emb <=> %s LIMIT 10", (q,)).fetchall()]
    def explain(self, q):
        return "\n".join(r[0] for r in self.c.execute("EXPLAIN SELECT id FROM items ORDER BY emb <=> %s LIMIT 10", (q,)))
    def round_trip_ms(self):  # the network hop into Docker, paid by every query
        t = []
        for _ in range(200):
            t0 = time.perf_counter(); self.c.execute("SELECT 1").fetchone(); t.append((time.perf_counter() - t0) * 1000)
        return round(st.median(t), 3)
    def close(self): self.c.close()


class SqliteVec:
    name = "sqlite-vec"
    def __init__(self, knob):
        import sqlean as sqlite3, sqlite_vec  # this Python's sqlite3 cannot load extensions; sqlean is the same SQLite API
        self.path = os.path.join(WORK, "vec.db")
        self.c = sqlite3.connect(self.path)
        self.c.enable_load_extension(True); sqlite_vec.load(self.c); self.c.enable_load_extension(False)
    def build(self, train):
        if self.c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'items_done'").fetchone()[0]:
            return None
        self.c.close(); os.remove(self.path); self.__init__(None)
        t0 = time.perf_counter()
        self.c.execute("CREATE VIRTUAL TABLE items USING vec0(id integer primary key, emb float[100] distance_metric=cosine)")
        self.c.executemany("INSERT INTO items (id, emb) VALUES (?, ?)", ((i, v.tobytes()) for i, v in enumerate(train)))
        self.c.execute("CREATE TABLE items_done (x)")  # marks a finished load
        self.c.commit()
        return time.perf_counter() - t0, os.path.getsize(self.path)
    def query(self, q):
        return [r[0] for r in self.c.execute("SELECT id FROM items WHERE emb MATCH ? AND k = 10", (q.tobytes(),))]
    def close(self): self.c.close()


class LanceDB:
    name = "lancedb"
    def __init__(self, knob):
        import lancedb
        self.db = lancedb.connect(os.path.join(WORK, "lance"))
        self.nprobes, self.refine = (list(map(int, knob.split(":"))) + [None])[:2] if knob else (None, None)
    def build(self, train):
        import pyarrow as pa
        if "items" in self.db.table_names():
            self.t = self.db.open_table("items")
            if self.t.list_indices(): return None
        t0 = time.perf_counter()
        emb = pa.FixedSizeListArray.from_arrays(pa.array(train.reshape(-1)), 100)
        self.t = self.db.create_table("items", pa.table({"id": pa.array(np.arange(len(train), dtype=np.int32)), "emb": emb}),
                                      mode="overwrite")
        # IVF_PQ; the default sub-vector count is dim/16, and 100 is not a multiple of 16, so it falls back to 1 sub-vector
        # (recall ~0.05 in the smoke test). 10 sub-vectors of 10 dims each; partitions stay at the default.
        self.t.create_index(metric="cosine", vector_column_name="emb", num_sub_vectors=10)
        secs = time.perf_counter() - t0
        size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(os.path.join(WORK, "lance")) for f in fs)
        return secs, size
    def query(self, q):
        s = self.t.search(q, vector_column_name="emb").distance_type("cosine").select(["id"]).limit(K)
        if self.nprobes: s = s.nprobes(self.nprobes)
        if self.refine: s = s.refine_factor(self.refine)
        return s.to_arrow()["id"].to_pylist()
    def explain(self, q):
        return self.t.search(q, vector_column_name="emb").distance_type("cosine").limit(K).explain_plan(True)
    def close(self): pass


class DuckDB:
    name = "duckdb-vss"
    def __init__(self, knob):
        import duckdb
        self.path = os.path.join(WORK, "duck.db")
        self.c = duckdb.connect(self.path)
        self.c.execute("INSTALL vss; LOAD vss; SET hnsw_enable_experimental_persistence = true")
        if knob: self.c.execute(f"SET hnsw_ef_search = {int(knob)}")
    def build(self, train):
        if self.c.execute("SELECT count(*) FROM duckdb_indexes() WHERE index_name = 'items_hnsw'").fetchone()[0]:
            return None
        t0 = time.perf_counter()
        self.c.execute("DROP TABLE IF EXISTS items")
        self.c.execute("CREATE TABLE items (id INTEGER, emb FLOAT[100])")
        import pyarrow as pa
        emb = pa.FixedSizeListArray.from_arrays(pa.array(train.reshape(-1)), 100)
        src = pa.table({"id": pa.array(np.arange(len(train), dtype=np.int32)), "emb": emb})
        self.c.execute("INSERT INTO items SELECT id, emb::FLOAT[100] FROM src")
        self.c.execute("CREATE INDEX items_hnsw ON items USING HNSW (emb) WITH (metric = 'cosine')")
        self.c.execute("CHECKPOINT")
        return time.perf_counter() - t0, os.path.getsize(self.path)
    def query(self, q):
        return [r[0] for r in self.c.execute(
            "SELECT id FROM items ORDER BY array_cosine_distance(emb, ?::FLOAT[100]) LIMIT 10", [q.tolist()]).fetchall()]
    def explain(self, q):
        return "\n".join(r[1] for r in self.c.execute(
            "EXPLAIN SELECT id FROM items ORDER BY array_cosine_distance(emb, ?::FLOAT[100]) LIMIT 10", [q.tolist()]).fetchall())
    def close(self): self.c.close()


ARMS = {"pgvector": PgVector, "sqlitevec": SqliteVec, "lancedb": LanceDB, "duckdb": DuckDB}


def versions():
    from importlib.metadata import version
    v = {"python": platform.python_version()}
    for p in ("sqlite-vec", "lancedb", "pylance", "duckdb", "psycopg", "pgvector", "numpy"):
        try: v[p] = version(p)
        except Exception: pass
    return v


def main():
    a = argparse.ArgumentParser()
    a.add_argument("arm", choices=ARMS); a.add_argument("--queries", type=int, default=1000)
    a.add_argument("--knob"); a.add_argument("--warmup", type=int, default=100); a.add_argument("--train", type=int)
    o = a.parse_args()
    global WORK
    if o.train: WORK += f"-smoke{o.train}"
    os.makedirs(WORK, exist_ok=True)
    train, test, truth = load(o.queries + o.warmup, o.train)
    eng = ARMS[o.arm](o.knob)
    built = eng.build(train)
    if built: print(f"{eng.name}: built in {built[0]:.1f} s, {built[1] / 1e6:.0f} MB", flush=True)
    plan = eng.explain(test[0]) if hasattr(eng, "explain") else "exact scan"
    print("plan uses the index:", any(w in plan.lower() for w in ("hnsw", "ivfpq", "index scan", "annsubindex", "annivf")), flush=True)
    rtt = eng.round_trip_ms() if hasattr(eng, "round_trip_ms") else None
    for q in test[o.queries:]: eng.query(q)  # warm-up queries (not measured, not in the recall set)
    lat, found = [], []
    for q in test[:o.queries]:
        t0 = time.perf_counter(); ids = eng.query(q); lat.append((time.perf_counter() - t0) * 1000); found.append(ids)
    eng.close()
    lat.sort()
    row = {"arm": eng.name, "knob": o.knob, "queries": o.queries, "recall@10": round(recall(found, truth[:o.queries]), 4),
           "p50_ms": round(st.median(lat), 3), "p99_ms": round(lat[int(len(lat) * 0.99) - 1], 3),
           "build_s": round(built[0], 1) if built else None, "size_mb": round(built[1] / 1e6) if built else None,
           "train": len(train), "round_trip_ms": rtt, "plan": plan,
           "versions": versions(), "when": time.strftime("%Y-%m-%d %H:%M")}
    print(json.dumps({k: v for k, v in row.items() if k not in ("plan", "versions")}), flush=True)
    out = "smoke.jsonl" if o.train else "results.jsonl"
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), out), "a") as f: f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
