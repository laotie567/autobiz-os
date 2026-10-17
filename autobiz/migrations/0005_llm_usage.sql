-- 0005（H-4）：模型调用成本记账。单价写在 config/models.yaml 的 provider 上；预算在 budget 节。
CREATE TABLE IF NOT EXISTS llm_usage (
  id                 bigserial PRIMARY KEY,
  at                 timestamptz NOT NULL DEFAULT now(),
  task               text,
  model              text,
  prompt_tokens      int,
  completion_tokens  int,
  cost_cny           numeric(10,4),
  action_log_id      bigint
);
CREATE INDEX IF NOT EXISTS llm_usage_at_idx ON llm_usage (at);
