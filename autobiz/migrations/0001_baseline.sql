-- 0001 基线：原 db.SCHEMA 原样迁入（对已有库幂等）。此后 schema 变更只新增编号文件。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS objects (
  id            text PRIMARY KEY,
  type          text NOT NULL,
  title         text NOT NULL,
  props         jsonb NOT NULL DEFAULT '{}',
  aliases       text[] NOT NULL DEFAULT '{}',
  verified      boolean NOT NULL DEFAULT false,
  verified_at   timestamptz,
  confidence    real,
  extracted_by  text,
  evidence      text,
  origin        text NOT NULL DEFAULT 'human',
  embedding     vector(512),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS objects_type_idx  ON objects (type);
CREATE INDEX IF NOT EXISTS objects_title_trgm ON objects USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS objects_props_idx ON objects USING gin (props);

CREATE TABLE IF NOT EXISTS links (
  link_type  text NOT NULL,
  from_id    text NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
  to_id      text NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by bigint,
  PRIMARY KEY (link_type, from_id, to_id)
);
CREATE INDEX IF NOT EXISTS links_to_idx ON links (to_id);

CREATE TABLE IF NOT EXISTS llm_cache (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  model      text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS action_log (
  id          bigserial PRIMARY KEY,
  action      text NOT NULL,
  actor       text NOT NULL,
  role        text NOT NULL,
  params      jsonb NOT NULL,
  status      text NOT NULL,
  result      jsonb,
  error       text,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
