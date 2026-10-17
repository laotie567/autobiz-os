#!/usr/bin/env bash
# 发布流水线（L-3）：从私有仓库按白名单生成干净的发布目录，三道扫描后打 tar 包。
#   allowlist.txt   以 ! 开头的行是排除项
#   denylist.local.txt  私有词表（你的客户名 / 公司名 / 租户 / 本机路径，每行一个；本文件已 gitignore）
# 发布仓库是全新 git 仓库（不继承私有历史）；每个版本打 tag。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VER="${1:?用法：scripts/release.sh <版本号，如 1.0.0-rc1>}"
OUT="$(mktemp -d)/autobiz-os-$VER"
mkdir -p "$OUT"
STAGE="$(dirname "$OUT")"

# 1) 白名单导出（只含已跟踪文件；! 行转成 git pathspec 排除）
cd "$ROOT"
ARGS=()
while IFS= read -r raw; do
  line="${raw%%#*}"                                  # 去行内注释
  line="${line%"${line##*[![:space:]]}"}"            # 去行尾空白
  [ -z "$line" ] && continue
  if [[ "$line" == !* ]]; then ARGS+=(":(exclude)${line:1}"); else ARGS+=("$line"); fi
done < release/allowlist.txt
# shellcheck disable=SC2068
git archive --format=tar HEAD ${ARGS[@]+"${ARGS[@]}"} | tar -x -C "$OUT"

# 落地页 README（多语言）覆盖发布物根 README（私有 README 不对外）
cp release/README*.md "$OUT"/
# 干净版 ignore 覆盖（私有 .gitignore 含客户目录规则）
cp release/gitignore "$OUT/.gitignore"
cp release/gitignore "$OUT/.dockerignore"

# 2) 私有词扫描：命中即终止
if [ -f release/denylist.local.txt ]; then
  if grep -rInF -f release/denylist.local.txt "$OUT"; then
    echo "✗ 命中私有词，终止发布" >&2; exit 1
  fi
else
  echo "✗ 未找到 release/denylist.local.txt（私有词表缺失，禁止发布）" >&2; exit 1
fi

# 3) 密钥扫描（gitleaks 可选）
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --no-git -s "$OUT" --redact
else
  echo "⚠ 未安装 gitleaks，密钥扫描跳过（建议安装后重跑）"
  if grep -rInE 'sk-[A-Za-z0-9]{20,}|api[_-]?key.*=.*[A-Za-z0-9]{24,}' "$OUT" --include='*' -i | grep -v example; then
    echo "✗ 疑似密钥，终止发布" >&2; exit 1
  fi
fi

# 4) 个人信息系统性猜测（手机号 / 身份证 / 本机绝对路径）。tests/ 豁免：
#    出口检查器的测试夹具本身就含假手机号 / 假证件号样例，真正的泄漏向量（文档/配置/代码）仍全覆盖。
if grep -rInE '1[3-9][0-9]{9}|[1-9][0-9]{16}[0-9Xx]|/Users/[a-z]+/' "$OUT" \
     --include='*.py' --include='*.yaml' --include='*.md' --include='*.sh' --include='*.toml' \
     --exclude-dir=tests; then
  echo "✗ 疑似个人信息 / 本机路径，终止发布" >&2; exit 1
fi

# 5) 发布物语法自检（不依赖数据库；测试在 CI / 本地全量跑过即可）
( cd "$OUT" && python3 -m compileall -q autobiz ) || { echo "✗ 发布物语法错误" >&2; exit 1; }
( cd "$OUT" && AUTOBIZ_HOME="$(mktemp -d)" python3 -c "
import sys; sys.path.insert(0, '.')
from autobiz import ontology
onto = ontology.load()
assert 'start_video_production' not in onto.action_types, '视频板块必须排除'
print(f'✓ 发布物本体可编译：{len(onto.object_types)} 类 / {len(onto.action_types)} 行动')
" ) || { echo "✗ 发布物本体校验失败" >&2; exit 1; }

mkdir -p "$ROOT/release"
tar -C "$STAGE" -czf "$ROOT/release/autobiz-os-$VER.tar.gz" "$(basename "$OUT")"
echo "✓ 发布物：release/autobiz-os-$VER.tar.gz"
echo "  下一步：解包到新目录 → git init + 首次提交 + 打 tag v$VER → 推 GitHub / Gitee（全新仓库，不带私有历史）"
