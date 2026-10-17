-- 0003（C-3）：LLM 缓存过期时间；缓存读写已改走独立连接，不再污染行动事务。
ALTER TABLE llm_cache ADD COLUMN IF NOT EXISTS expires_at timestamptz;
UPDATE llm_cache SET expires_at = now() + interval '7 days' WHERE expires_at IS NULL;
