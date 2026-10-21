# LangGraph integration plan

Approved scope: the user's implementation request and prior architecture discussion; design in ../specs/2026-10-21-langgraph-design.md. Execution: implement inline, use fresh design/code review as required by skills. Workspace is not a Git checkout, so preserve touched file snapshots rather than create a fake repository or commits.

## Review focus
- Cancellation-swallowing model clients cannot launch late generation or release robot ownership.
- Plan-only/request modes cannot start hidden physical work/inference respectively.
- Missing evidence cannot pass completion; stop must preserve feedback through inspection.
- Replans and repairs retain original deadlines/budgets and cannot repeat completed motion.
- Durable clarification resumes with a new object, but physical graphs must never replay automatically.

## Tasks
1. Add required LangGraph dependencies and common graph runner/topology. Tests: legal conditional transitions, trace, exception/cancellation propagation, graph export. Back up touched sources and install in current venv.
2. Convert planner dispatch and PlanGenerator repair flow to nodes. Tests: reuse/generate/clarify, invalid output repair, no side effects and model timeout/lifecycle. Preserve signatures and counters.
3. Convert native mission control to graph stages, retaining outer admission/monitor/finalization. Test normal execution, inspector contradiction, missing evidence, request no hidden planner, stop/cancellation and replay protections with full existing suites.
4. Convert A2 host continuation and worker execution/inspection stages. Test serial workers, bounds and localization feedback ordering. Update and build only owned adapter image; test import in container without starting ROS or movement.
5. Add planning-only persistent clarification workflow with SQLite saver. Test pause/resume/new instance, input identity, isolation and no backend calls. Add graph CLI/docs/architecture Mermaid and explicit physical-resume limitation.
6. Run all regression tests plus controlled SDK/CLI smoke cases and dependency checks. Save report with actual results. Fresh reviewer checks integration and safety; resolve actionable findings and rerun affected checks.
