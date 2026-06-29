from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional
import uuid

app = FastAPI(title="AgentMesh API")

# Minimal in-memory store for absolute first-run verification 
# before DB migrations are fully wired
mock_db = []

class ExperiencePost(BaseModel):
    post_type: str
    problem_statement: str
    solution_or_insight: str
    context_tags: List[str]
    confidence: float = 1.0
    created_by: str = "system"

@app.get("/health")
async def health():
    return {"status": "online", "message": "AgentMesh is breathing"}

@app.get("/posts", response_model=List[ExperiencePost])
async def get_posts():
    return mock_db

@app.post("/posts")
async def create_post(post: ExperiencePost):
    mock_db.append(post)
    return {"status": "success", "post_id": str(uuid.uuid4())}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
