-- 0004（I-1）：向量索引；疑似重复检测从 overview 的平方级自连接移入后台任务（autobiz dedupe）。
CREATE INDEX IF NOT EXISTS objects_embedding_hnsw ON objects USING hnsw (embedding vector_cosine_ops);
CREATE TABLE IF NOT EXISTS dup_candidates (
  a        text,
  b        text,
  dist     real,
  found_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (a, b)
);
