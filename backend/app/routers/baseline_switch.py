"""推荐基线切换接口：按场景版本差异重载、差异核对、版本断点复核与智能推荐构建上线。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.baseline_switch import (
    CODE_BACK_LIST,
    CODE_BROWSE_RELATED,
    CODE_BUILD_ONLINE,
    CODE_BUSINESS,
    CODE_REFRESH_VIEW,
    baseline_switch_service as service,
)

router = APIRouter(prefix="/api/baseline-switch", tags=["推荐基线切换"])


@router.get("/scenes")
def list_scenes() -> dict[str, Any]:
    """推荐场景：含当前可用基线、多样性权重与监控样本完整性。"""
    return {"核验码": CODE_BUSINESS, "items": service.list_scenes()}


@router.get("/switches")
def list_switches(detection_no: str | None = Query(default=None)) -> dict[str, Any]:
    """基线版本切换记录列表。"""
    items = service.list_switches(detection_no=detection_no)
    return {"核验码": CODE_BUSINESS, "total": len(items), "items": items}


@router.post("/switches", response_model=ActionResult)
def register_switch(payload: EntryPayload) -> ActionResult:
    """由数据漂移待处理明细登记基线切换，按场景版本差异重载受影响召回。"""
    detection_no = str(payload.values.get("检测编号") or "DATA-0001").strip()
    switch_row, message, idempotent = service.register_switch(detection_no, payload.values)
    if switch_row is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=message,
        entry={
            "核验码": CODE_BUSINESS,
            "幂等命中": idempotent,
            "切换记录": switch_row,
            "核对表": service.list_checks(switch_id=int(switch_row["id"])),
            "下一步": {"返回列表": CODE_BACK_LIST, "刷新页面": CODE_REFRESH_VIEW,
                      "浏览关联结果": CODE_BROWSE_RELATED},
        },
    )


@router.get("/switches/{switch_id}")
def get_switch(switch_id: int) -> dict[str, Any]:
    """切换记录详情，附差异清单、召回批次与切换凭据。"""
    switch_row = service.get_switch(switch_id)
    if switch_row is None:
        return {"ok": False, "message": f"切换记录 {switch_id} 不存在"}
    return {
        "ok": True,
        "核验码": CODE_BUSINESS,
        "切换记录": switch_row,
        "核对表": service.list_checks(switch_id=switch_id),
        "召回批次": [r for r in service.list_recalls()
                  if r.get("监控数据集") == switch_row.get("监控数据集")],
        "切换凭据": service.list_credentials(switch_id=switch_id),
    }


@router.post("/switches/{switch_id}/retry", response_model=ActionResult)
def retry_failed(switch_id: int, payload: EntryPayload) -> ActionResult:
    """重试刷新失败的场景；重试期间保留最后可用结果。"""
    switch_row, message = service.retry_failed(switch_id, payload.values)
    if switch_row is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=switch_row)


@router.post("/switches/{switch_id}/verify", response_model=ActionResult)
def verify_switch(switch_id: int) -> ActionResult:
    """核对切换前后结果：未引用新基线场景不变，变化场景可由漂移值与权重解释。"""
    switch_row, message = service.verify_switch(switch_id)
    if switch_row is None:
        return ActionResult(ok=False, message=message)
    ok = switch_row.get("状态") == "已核对"
    return ActionResult(ok=ok, message=message, entry={
        "核验码": CODE_BUSINESS,
        "切换记录": switch_row,
        "核对表": service.list_checks(switch_id=switch_id),
    })


@router.get("/switches/{switch_id}/breakpoint")
def locate_breakpoint(switch_id: int) -> dict[str, Any]:
    """沿原始调用链复核版本断点，确认当前召回只对应一套可解释基线。"""
    switch_row = service.get_switch(switch_id)
    if switch_row is None:
        return {"ok": False, "message": f"切换记录 {switch_id} 不存在"}
    result = service.locate_breakpoint(str(switch_row["检测编号"]))
    result["ok"] = True
    return result


@router.get("/checks")
def list_checks(switch_id: int | None = Query(default=None),
                detection_no: str | None = Query(default=None)) -> PageResult[dict]:
    """推荐基线切换核对表明细，兼容既有业务记录。"""
    items = service.list_checks(switch_id=switch_id, detection_no=detection_no)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/recalls")
def list_recalls(scene: str | None = Query(default=None)) -> dict[str, Any]:
    """召回批次：旧批次只读可回查，当前可用性只指向一个批次。"""
    items = service.list_recalls(scene=scene)
    return {"核验码": CODE_BUSINESS, "total": len(items), "items": items}


@router.get("/credentials")
def list_credentials(switch_id: int | None = Query(default=None)) -> dict[str, Any]:
    """切换凭据：多监控数据集时按场景分别保留旧/新召回与凭据。"""
    items = service.list_credentials(switch_id=switch_id)
    return {"核验码": CODE_BUSINESS, "total": len(items), "items": items}


@router.post("/sync-recommend", response_model=ActionResult)
def sync_recommend(payload: EntryPayload) -> ActionResult:
    """把智能推荐状态同步到数据漂移清单（默认主业务样本 DATA-0001）。"""
    detection_no = str(payload.values.get("检测编号") or "DATA-0001").strip()
    drift, message = service.sync_recommend_status(detection_no)
    if drift is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"核验码": CODE_BUSINESS, "数据漂移明细": drift})


@router.get("/deploy/dependency")
def deploy_dependency() -> dict[str, Any]:
    """读取智能推荐构建上线的部署依赖（联调环境数据库）状态。"""
    return {"核验码": CODE_BUILD_ONLINE, "依赖": service.deploy_dependency()}


@router.post("/deploy/connect-db", response_model=ActionResult)
def connect_env_db() -> ActionResult:
    """联通联调环境数据库，构建上线前置依赖。"""
    dep, message = service.connect_env_db()
    return ActionResult(ok=True, message=message, entry={"核验码": CODE_BUILD_ONLINE, "依赖": dep})


@router.post("/build-online", response_model=ActionResult)
def build_online() -> ActionResult:
    """智能推荐构建上线（核验码 30c7bd46），依赖联调环境数据库且核对表已通过。"""
    result, message = service.build_online()
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/export")
def export() -> dict[str, Any]:
    """导出基线切换全量核对数据。"""
    return {
        "module": "baseline-switch",
        "核验码": CODE_BUSINESS,
        "switches": service.list_switches(),
        "checks": service.list_checks(),
        "recalls": service.list_recalls(),
        "credentials": service.list_credentials(),
        "scenes": service.list_scenes(),
        "流程核验码": {"返回列表": CODE_BACK_LIST, "刷新页面": CODE_REFRESH_VIEW,
                    "浏览关联结果": CODE_BROWSE_RELATED, "构建上线": CODE_BUILD_ONLINE},
    }
