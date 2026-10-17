-- 0002（B-3 L2）：闸门行动审批。审批码在预览时生成，用户在终端 approve 后 Agent 才能确认。
CREATE TABLE IF NOT EXISTS approvals (
  code         text PRIMARY KEY,
  action       text NOT NULL,
  params_hash  text NOT NULL,
  target_ver   timestamptz,
  preview      jsonb,
  status       text NOT NULL DEFAULT 'pending',  -- pending | approved | used | expired
  approved_by  text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS approver_failures (at timestamptz NOT NULL DEFAULT now());
