from fastapi import FastAPI

from starlette_apitally.middleware import ApitallyMiddleware


api = FastAPI()
api.add_middleware(ApitallyMiddleware, client_id="xxx", send_every=10)


@api.get("/healthz")
def health_check():
    return {"status": "ok"}
