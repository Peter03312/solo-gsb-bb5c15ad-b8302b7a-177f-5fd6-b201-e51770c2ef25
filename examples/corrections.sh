#!/usr/bin/env bash
# 本轮用法端到端示例: 已交评语的更正 (会务方发起 + 评审人提交)
#   发布 -> 两人确认并各交一份正式评语 -> 会务方发布反馈快照 ->
#   会务方对已交评语发起更正请求 (指定当前发布序号/论文/评审人/非空原因),
#   该稿既有作者反馈访问码立即失效 -> 评审人查看自己的待更正任务 ->
#   凭请求对应的发布序号与原评语收据提交更正 (新收据, 原评语留档追溯) ->
#   同内容重试幂等 / 异内容冲突 -> 会务方按现有规则重新发布快照 ->
#   发布序号变化后, 未完成的待更正请求失效
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/corrections.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 + 1 篇论文 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]}'

echo "== 2. 预演 + 发布, 两人确认并各交一份正式评语 =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SER=$(echo "$PUB" | j 'd["serial"]')
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
CA="r$(echo "$RA" | sed 's/R//')-secret"
CB="r$(echo "$RB" | sed 's/R//')-secret"
echo "P1 (serial=$SER) 分配: $RA, $RB"
for H in "$RA $CA" "$RB $CB"; do
  set -- $H
  curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
    -H "X-Reviewer-Id: $1" -H "X-Reviewer-Credential: $2" \
    -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
done
RVA=$(curl -s -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 4, \"comment\": \"选题重要, 建议补充消融实验。\"}")
RCPT_A=$(echo "$RVA" | j 'd["receipt"]')
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 2, \"comment\": \"实验设计有漏洞, 结论偏强。\"}"
echo "$RA 的评语收据: $RCPT_A"

echo "== 3. 会务方发布反馈快照, 持码者可读 =="
SNAP=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
CODE=$(echo "$SNAP" | j 'd["access_code"]')
echo "访问码: $CODE"
curl -s -o /dev/null -w '持码读取          -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE"

echo "== 4. 会务方对 $RA 的已交评语发起更正请求 (当前发布序号 + 非空原因) =="
REQ=$(curl -s -X POST "$BASE/papers/P1/review-corrections" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"reviewer_id\": \"$RA\", \"serial\": $SER, \"reason\": \"评分与评语明显不符, 请复核\"}")
echo "$REQ"
echo "-- 同原因重复发起 -> changed=false (幂等); 异原因 -> 409; 旧序号 -> 409; 空白原因 -> 422"
curl -s -X POST "$BASE/papers/P1/review-corrections" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"reviewer_id\": \"$RA\", \"serial\": $SER, \"reason\": \"评分与评语明显不符, 请复核\"}" \
  | j '{"changed": d["changed"]}'
curl -s -o /dev/null -w '异原因重复发起    -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/review-corrections" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"reviewer_id\": \"$RA\", \"serial\": $SER, \"reason\": \"另一个原因\"}"
curl -s -o /dev/null -w '过期发布序号      -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/review-corrections" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"reviewer_id\": \"$RA\", \"serial\": $((SER + 1)), \"reason\": \"x\"}"
curl -s -o /dev/null -w '空白原因          -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/review-corrections" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"reviewer_id\": \"$RA\", \"serial\": $SER, \"reason\": \"   \"}"

echo "== 5. 更正请求发起即令该稿既有作者反馈访问码失效 (统一 404) =="
curl -s -o /dev/null -w '旧访问码读取      -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE"
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  | j '[(s["version"], s["active"]) for s in d["snapshots"]]'

echo "== 6. $RA 凭本人凭据查看自己的待更正任务 (含原评语收据, 看不到他人任务) =="
curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA"; echo
curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  | j 'd["corrections"]'

echo "== 7. $RA 以请求对应的发布序号 + 原评语收据提交更正 (新评分+新评语) =="
echo "-- 请求未完成时收据不符 -> 409"
curl -s -o /dev/null -w '收据不符          -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"receipt\": \"rvw-nonexistent\", \"score\": 5, \"comment\": \"x\"}"
COR=$(curl -s -X POST "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"receipt\": \"$RCPT_A\", \"score\": 5, \"comment\": \"更正后: 选题重要且实验充分, 建议接收。\"}")
echo "$COR"
NCOR=$(echo "$COR" | j 'd["receipt"]')
echo "更正收据: $NCOR (原收据 $RCPT_A 留档)"

echo "== 7b. 同内容重试 -> 原更正收据 changed=false; 异内容 -> 409; 未知收据 -> 404 =="
curl -s -X POST "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"receipt\": \"$RCPT_A\", \"score\": 5, \"comment\": \"  更正后: 选题重要且实验充分, 建议接收。 \"}" \
  | j '{"changed": d["changed"], "same_receipt": d["receipt"] == "'"$NCOR"'"}'
curl -s -o /dev/null -w '异内容更正        -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"receipt\": \"$RCPT_A\", \"score\": 1, \"comment\": \"改成别的\"}"
curl -s -o /dev/null -w '未知收据          -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"receipt\": \"rvw-nonexistent\", \"score\": 5, \"comment\": \"x\"}"

echo "== 8. 更正后: 评审人本人视图与会务方进度均为更正后内容; 原评语留档追溯 =="
curl -s "$BASE/reviewer/reviews" -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  | j '[(r["receipt"], r["score"], r["comment"]) for r in d["reviews"]]'
curl -s "$BASE/papers/P1/review-corrections" -H "$ORG"; echo

echo "== 9. 会务方按现有完整性规则重新发布快照 (同序号, 收据已变) -> 新版本新码 =="
SNAP2=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshot" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"serial\": $SER}")
CODE2=$(echo "$SNAP2" | j 'd["access_code"]')
echo "version=$(echo "$SNAP2" | j 'd["version"]'), 新码=$CODE2"
curl -s "$BASE/feedback-snapshots/$CODE2"; echo
curl -s -o /dev/null -w '旧访问码仍失效    -> HTTP %{http_code}\n' "$BASE/feedback-snapshots/$CODE"

echo "== 10. 发布序号变化后, 未完成的待更正请求失效 =="
curl -s -o /dev/null -X POST "$BASE/papers/P1/review-corrections" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"reviewer_id\": \"$RB\", \"serial\": $SER, \"reason\": \"请补充评语细节\"}"
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
curl -s -o /dev/null -X POST "$BASE/assignment/backfill/publish" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $(echo "$DRY" | j 'd["revision"]'), \"base_serial\": $(echo "$DRY" | j 'd["serial"]')}"
echo "补位发布完成 (serial $SER -> $((SER + 1)))"
echo "-- $RB 的待更正任务列表已为空:"
curl -s "$BASE/reviewer/review-corrections" -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  | j 'd["corrections"]'
RCPT_B=$(curl -s "$BASE/papers/P1/review-corrections" -H "$ORG" \
  | j '[c for c in d["corrections"] if c["reviewer_id"] == "'"$RB"'"][0]["original"]["receipt"]')
curl -s -o /dev/null -w '按旧序号提交更正  -> HTTP %{http_code}\n' -X POST \
  "$BASE/reviewer/review-corrections/P1" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"receipt\": \"$RCPT_B\", \"score\": 3, \"comment\": \"x\"}"
echo "-- 会务方追溯: 该请求状态为 expired; 保留槽位沿用的仍是更正后的评语:"
curl -s "$BASE/papers/P1/review-corrections" -H "$ORG" \
  | j '[(c["reviewer_id"], c["status"]) for c in d["corrections"]]'
curl -s "$BASE/papers/P1/reviews" -H "$ORG" \
  | j '[(s["reviewer_id"], (s["review"] or {}).get("receipt")) for s in d["slots"]]'
