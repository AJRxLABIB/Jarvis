# Jarvis architecture diagrams

Every box is a real module in this repository. Border legend (Mermaid class): **impl** = implemented and unit-tested; **advisory** = implemented and tested but
not yet wired into the live loop; **unverified** = implemented, but its real-hardware/real-device behaviour has not been checked; **planned** = not built.
Nothing marked *planned* exists. View with any Mermaid renderer (GitHub, VS Code "Markdown Preview Mermaid Support").

```mermaid
%% classes used below
flowchart LR
  a[impl]:::impl --- b[advisory]:::advisory --- c[unverified]:::unverified --- d[planned]:::planned
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
  classDef advisory fill:#3d3a0b,stroke:#f1c40f,color:#fff
  classDef unverified fill:#3d2a0b,stroke:#e67e22,color:#fff
  classDef planned fill:#222,stroke:#777,color:#aaa,stroke-dasharray: 4
```

## 1. Unified architecture

```mermaid
flowchart TD
  UI[UI: command center + chat + voice\napp/ui, app/voice]:::impl --> INT[Intent and request context\nagent/shortcuts, core/request_context]:::impl
  INT --> COG[Cognitive plan: level, objectives, roles\ncore/cognition]:::advisory
  INT --> CONV[Conversation + tool loop\nagent/conversation, ai/router]:::impl
  COG -.advice.-> CONV
  CONV --> EXE[Tool executor: validate, permission, approval, limits, audit\nagent/tool_manager, security/*]:::impl
  EXE --> TOOLS[Tools: files, windows, browser, phone, ...\napp/tools]:::impl
  EXE --> VER[Verification\ncore/screen_verify, claim_verify, transactions]:::impl
  VER --> MEM[Memory, knowledge graph, learning\napp/memory, core/knowledge_graph, learning*]:::impl
  MEM --> INT
  EXE --> AUD[Audit chain\ncore/auditchain]:::impl
  META[Analytics + reliability lab\ncore/reliability_lab, mission_report]:::impl --> CONV
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
  classDef advisory fill:#3d3a0b,stroke:#f1c40f,color:#fff
```

## 2. Cognitive brain network (roles, not separate chatbots)

```mermaid
flowchart LR
  SUP[Supervisor: cognition.classify\n+ orchestrator]:::advisory --> R[Reasoning]:::advisory
  SUP --> P[Planning: missions, schedule_sim]:::impl
  SUP --> RS[Research: research_store, synthesis]:::impl
  SUP --> CR[Critic: interbrain.debate]:::advisory
  SUP --> PR[Predictive: predictions, twin]:::impl
  SUP --> M[Memory cognition: memory2, consolidation]:::impl
  SUP --> V[Verification: claim_verify, screen_verify]:::impl
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
  classDef advisory fill:#3d3a0b,stroke:#f1c40f,color:#fff
```
Roles are selected as names by `cognition.brains()`; the executors that already exist (agents, orchestrator) carry out the work. A separate "creative brain" and
"social brain" are not separate components; they are prompt styles of the one chat model.

## 3. InterBrain communication

```mermaid
sequenceDiagram
  participant S as Supervisor
  participant R as Research
  participant C as Critic
  participant V as Verification
  S->>R: TASK_REQUEST (task id, objective)
  R-->>S: EVIDENCE_REPORT (evidence refs, confidence)
  S->>C: HYPOTHESIS
  C-->>S: CHALLENGE (what it objects to)
  S->>R: CORRECTION (answers the challenge)
  S->>V: TASK_REQUEST (verify)
  V-->>S: VERIFICATION_RESULT (done / failed)
  S-->>S: TASK_COMPLETED (needs evidence)
```
`core/interbrain.py` validates the contract and keeps a bounded timeline; **advisory** (the live mission loop still uses `core/blackboard.py` entries).

## 4. Capability composition

```mermaid
flowchart LR
  REQ[Request] --> GAP[capability_gaps / opportunities]:::impl
  GAP --> REG[Tool registry + risk + category\ntools/base, catalog]:::impl
  REG --> FND[Foundry: parameterised workflow from existing tools\ncore/foundry]:::impl
  FND --> SIM[automation/simulator + what-if]:::impl
  SIM --> APPR[User approval]:::impl --> RUN[Executor]:::impl
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
```
Generated *code* is not produced (the foundry composes existing tools only); that needs an OS-level sandbox decision.

## 5. Meta-intelligence orchestration

```mermaid
flowchart TD
  U[User goal] --> GC[goal_contract + missions]:::impl
  GC --> LEASE[capability leases\ncore/leases]:::impl
  GC --> ORCH[orchestrator + agent contracts + blackboard]:::impl
  ORCH --> EX[executor]:::impl --> PRED[predictions: said vs happened]:::impl
  PRED --> REP[mission_report analytics]:::impl
  COG[cognition level/budget]:::advisory -.advice.-> ORCH
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
  classDef advisory fill:#3d3a0b,stroke:#f1c40f,color:#fff
```

## 6. Memory and knowledge

```mermaid
flowchart LR
  CHAT[Conversations]:::impl --> MEMS[Memories: memory2, semantic search]:::impl
  DOCS[Documents / vault / research_store]:::impl --> KG[Knowledge graph + synthesis]:::impl
  MEMS --> KG
  KG --> CTX[request_context: sources, confidence, unknowns]:::impl
  MEMS --> CONS[consolidation: merge proposals]:::impl
  EXPT[experiments: graded conclusions]:::advisory --> MEMS
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
  classDef advisory fill:#3d3a0b,stroke:#f1c40f,color:#fff
```

## 7. Model routing and resources

```mermaid
flowchart LR
  REQ[Request] --> ROUTE[ai/router + roles + switching]:::impl
  ROUTE --> GW[inference_gateway: queue, lifecycle]:::impl
  GW --> RES[resources: VRAM plan, idle unload, scheduler]:::unverified
  RES --> OLL[Ollama / Foundry Local]:::unverified
  BENCH[model_compare + modelbench]:::impl --> ROUTE
  PERF[perf modes]:::impl --> ROUTE
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
  classDef unverified fill:#3d2a0b,stroke:#e67e22,color:#fff
```

## 8. Failure detection and recovery

```mermaid
flowchart LR
  FAIL[tool failure / verification fail]:::impl --> RC[root_cause classifier]:::impl
  RC --> RET[retry policy + circuit breakers\ncore/retry, circuit]:::impl
  RET --> CP[checkpoints / snapshots / undo]:::impl
  RC --> INV[investigate_problem + recheck]:::impl
  INV --> TE[tech_experience: failed attempts, verified fixes]:::impl
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
```

## 9. Execution and verification

```mermaid
flowchart LR
  PLAN[plan] --> GATE[gate: lease, autonomy mode, approval]:::impl --> ACT[tool / UIA / phone command]:::unverified
  ACT --> CHK[verify: screen_verify, claim_verify, phone ack]:::impl
  CHK -->|failed| REC[recovery]:::impl
  CHK -->|verified| LOG[audit + learning]:::impl
  DRIFT[ui_drift: interface changed?]:::unverified -.-> ACT
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
  classDef unverified fill:#3d2a0b,stroke:#e67e22,color:#fff
```

## 10. Cross-device

```mermaid
flowchart LR
  PC[This PC]:::impl --- MESH[device_mesh: trust, abilities]:::impl --- PH[Phone app]:::unverified
  MESH --> PLACE[task_placement: who should do it]:::impl
  PLACE --> HO[handoff / transactions: signed, expiring]:::impl
  HO --> PH
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
  classDef unverified fill:#3d2a0b,stroke:#e67e22,color:#fff
```

## 11. Learning loop

```mermaid
flowchart LR
  OUT[Verified outcome]:::impl --> LES[candidate lesson]:::impl --> QC[learning_quality: decay, harm guard, conflicts]:::impl
  QC --> USE[applied with effect tracking]:::impl --> OUT
  POST[cognition.postcheck: learn only if verified]:::advisory -.-> LES
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
  classDef advisory fill:#3d3a0b,stroke:#f1c40f,color:#fff
```

## 12. Security and permission boundaries

```mermaid
flowchart TD
  IN[Untrusted content: web, mail, files, plugins, other agents]:::impl --> SEP[instruction/data separation]:::impl
  SEP --> EXE[executor: arg validation, risk level, confirmation]:::impl
  EXE --> LEV{safe / moderate / dangerous / critical}:::impl
  LEV -->|critical| BLOCK[blocked]:::impl
  EXE --> EMG[emergency stop]:::impl
  EXE --> AUD[hash-chained audit]:::impl
  PLG[plugins: HMAC-signed manifests, sandbox]:::impl --> EXE
  classDef impl fill:#0b3d2e,stroke:#2ecc71,color:#fff
```
