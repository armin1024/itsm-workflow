"""Development-only AOPS identity stub. Never use in production."""

from fastapi import FastAPI, Header, HTTPException

app = FastAPI()


@app.get("/v2/user/self")
def user_self(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401)
    return {"status": 0, "count": 0, "data": {"uid": "S000001", "username": "流程测试员"}}
