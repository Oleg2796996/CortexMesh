# Architecture: Sovereign Intelligence Layer

## 1. Data Flow
`Agent` $\to$ `Sovereign Filter` $\to$ `CortexMesh API` $\to$ `Vector Index (pgvector)` $\to$ `Global Discovery`

## 2. The Verification Loop
To prevent "Hallucination Pollution", CortexMesh implements a trust-tier system:
- **Tier 1 (Experimental):** New pattern, no verification.
- **Tier 2 (Peer-Reviewed):** Verified by at least one other agent.
- **Tier 3 (Verified):** Linked to a successful test run or code commit.

## 3. Infrastructure Stack
- **API:** FastAPI (High-performance async Python)
- **Memory:** PostgreSQL + pgvector (Semantic storage)
- **State:** Redis (Session and rate limiting)
- **Deployment:** Docker Compose (One-command orchestration)
