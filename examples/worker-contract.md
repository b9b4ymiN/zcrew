# Example worker contract

OBJECTIVE
Implement the approved Scenario Comparison UI in Plan Inspector.

CONTEXT
- The planner already exposes current/proposed scenario data through the existing hook.
- Keep business rules outside React presentation components.

SCOPE
- `components/PlanInspector/**`
- the existing scenario hook and focused tests when required

DO NOT
- change the API contract;
- change the Memgraph schema;
- introduce a new UI framework;
- modify unrelated planner logic.

ACCEPTANCE CRITERIA
1. Current and proposed scenarios are distinguishable.
2. PDD delta and capacity violations are visible.
3. Existing behavior remains intact when no proposed scenario exists.
4. Relevant tests/typecheck/build pass.

VERIFY
- project-specific unit tests
- typecheck
- production build if the change affects compile/runtime integration

REPORT
Return changed files, verification commands/results, and any unresolved risk. Do not self-approve.
