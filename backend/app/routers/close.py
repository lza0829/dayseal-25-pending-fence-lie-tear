from fastapi import APIRouter, Body
from app.services import close_service as svc

router = APIRouter(prefix="/close", tags=["close"])


@router.get("/preview")
def preview(business_day: str | None = None):
    return svc.preview(business_day)


@router.get("/gate")
def gate():
    """未决长短款闸状态：与封账拒绝判定、总览计数同源。"""
    return svc.gate_status()


@router.post("/seal")
def seal(body: dict = Body(...)):
    return svc.seal(body)


@router.post("/variances/{variance_id}/resolve")
def resolve_variance(variance_id: int, body: dict = Body(...)):
    return svc.resolve_variance(variance_id, body)


@router.get("/batches")
def batches():
    return svc.list_batches()


@router.get("/variances")
def variances():
    return svc.list_variances()
