from fastapi import APIRouter, Body, Request
from . import service

router = APIRouter(tags=["M7-复核合并"])


@router.get("/review")
def page(request: Request):
    data = service.status(request.app.state.review_store)
    return request.app.state.templates.TemplateResponse(request, "m7_review.html", {"title": "复核合并", "data": data})


@router.post("/api/review/baselines")
def api_baselines(request: Request, payload: dict = Body(...)):
    return service.submit_baseline(request.app.state.review_store, payload)


@router.post("/api/review/opinions")
def api_opinions(request: Request, payload: dict = Body(...)):
    return service.submit_opinion(request.app.state.review_store, payload)


@router.post("/api/review/adjudications")
def api_adjudications(request: Request, payload: dict = Body(...)):
    return service.submit_adjudication(request.app.state.review_store, payload)


@router.post("/api/review/merge")
def api_merge(request: Request):
    return service.commit_merge(request.app.state.review_store)


@router.get("/api/review/status")
def api_status(request: Request):
    return service.status(request.app.state.review_store)
