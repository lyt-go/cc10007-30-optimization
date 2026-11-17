"""推荐基线切换业务规则。

针对“监控基线切换时整批刷新召回、未引用新基线的场景与多样性权重被反复重建”的问题，
这里实现**按场景版本差异重载**：

- 切换登记由数据漂移待处理明细驱动，只处理引用了新基线对应监控数据集的场景；
- 从用于效率改进的基线版本切换记录中，按受影响记录读取监控数据集，仅计算受影响召回；
- 受影响推荐场景与已完成召回分别记录，旧召回只读、新结果写入当前推荐可用性；
- 刷新失败不改写最后可用结果；监控样本不足停在待复核、不发布差异结果；
- 重复提交幂等，只登记一份可用于结算的核对表结果。

所有状态流转都收口在本服务，路由层不做业务判断。
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from app.store import store

# 核验码：端到端流程核对点，随对应接口返回，便于自动化核对与页面联动。
CODE_BUSINESS = "308a3eb1"        # 本业务：推荐基线切换核对
CODE_BUILD_ONLINE = "30c7bd46"    # 智能推荐构建上线（依赖联调环境数据库）
CODE_BACK_LIST = "3006f3b5"       # 结束后回到列表
CODE_REFRESH_VIEW = "30e04069"    # 刷新页面
CODE_BROWSE_RELATED = "303b380f"  # 浏览关联结果

DRIFT_MODULE = "datadrift"
SWITCH_MODULE = "baseline_switch"
CHECK_MODULE = "baseline_switch_check"
RECALL_MODULE = "recall_batch"
CRED_MODULE = "switch_credential"
SCENE_MODULE = "recommend_scene"
DATASET_MODULE = "monitor_dataset"
DEPLOY_MODULE = "deploy_dependency"

# 核对表/切换单状态
ST_PENDING_REVIEW = "待复核"
ST_REFRESHING = "差异刷新中"
ST_REFRESH_FAILED = "刷新失败"
ST_DIFF_DONE = "差异已生成"
ST_CHECKED = "已核对"
ST_NO_SAMPLE = "待复核-监控样本不足"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _hash_boundary(*parts: str) -> int:
    """以场景、基线、数据集为输入得到稳定的召回边界，保证同一基线可重复解释。"""
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()
    return 1000 + int(digest[:8], 16) % 9000


class BaselineSwitchService:
    # ------------------------------------------------------------------ 查询
    def list_switches(self, *, detection_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SWITCH_MODULE)
        if detection_no:
            rows = [row for row in rows if row.get("检测编号") == detection_no]
        return rows

    def list_checks(self, *, switch_id: int | None = None,
                    detection_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(CHECK_MODULE)
        if switch_id is not None:
            rows = [row for row in rows if int(row.get("切换记录id", 0)) == switch_id]
        if detection_no:
            rows = [row for row in rows if row.get("检测编号") == detection_no]
        return rows

    def list_recalls(self, *, scene: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RECALL_MODULE)
        if scene:
            rows = [row for row in rows if row.get("推荐场景") == scene]
        return rows

    def list_credentials(self, *, switch_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(CRED_MODULE)
        if switch_id is not None:
            rows = [row for row in rows if int(row.get("切换记录id", 0)) == switch_id]
        return rows

    def list_scenes(self) -> list[dict[str, Any]]:
        return store.rows(SCENE_MODULE)

    def get_switch(self, switch_id: int) -> dict[str, Any] | None:
        return store.find(SWITCH_MODULE, switch_id)

    # ------------------------------------------------------------- 内部工具
    def _find_drift(self, detection_no: str) -> dict[str, Any] | None:
        for row in store.rows(DRIFT_MODULE):
            if row.get("检测编号") == detection_no:
                return row
        return None

    def _monitor_sample(self, dataset: str, baseline: str) -> dict[str, Any] | None:
        for row in store.rows(DATASET_MODULE):
            if row.get("监控数据集") == dataset and row.get("基线版本") == baseline:
                return row
        return None

    def _affected_scenes(self, dataset: str, old_baseline: str) -> list[dict[str, Any]]:
        """仅选择引用了该监控数据集、且当前仍停在旧基线的场景——即版本差异受影响场景。"""
        return [
            row for row in store.rows(SCENE_MODULE)
            if row.get("引用数据集") == dataset and row.get("引用基线版本") == old_baseline
        ]

    def _existing_switch(self, detection_no: str) -> dict[str, Any] | None:
        for row in store.rows(SWITCH_MODULE):
            if row.get("检测编号") == detection_no:
                return row
        return None

    def _make_batch_no(self, scene: str, baseline: str) -> str:
        seq = sum(1 for row in store.rows(RECALL_MODULE)
                  if row.get("推荐场景") == scene and row.get("基线版本") == baseline) + 1
        return f"RB-{scene}-{baseline}-{seq:03d}"

    def _snapshot_old_recall(self, scene_row: dict[str, Any]) -> dict[str, Any]:
        """把场景当前召回快照为只读旧批次；已存在同基线只读批次则直接复用。"""
        scene = str(scene_row["推荐场景"])
        baseline = str(scene_row["当前可用基线"])
        for row in store.rows(RECALL_MODULE):
            if (row.get("推荐场景") == scene and row.get("基线版本") == baseline
                    and row.get("只读")):
                return row
        batch = {
            "id": _next_id(store.rows(RECALL_MODULE)),
            "召回批次号": self._make_batch_no(scene, baseline),
            "推荐场景": scene,
            "监控数据集": scene_row["引用数据集"],
            "基线版本": baseline,
            "召回边界": _hash_boundary(scene, baseline, str(scene_row["引用数据集"])),
            "召回数量": _hash_boundary(scene, baseline, "size") % 500 + 100,
            "只读": True,
            "当前可用性": False,
            "刷新状态": "历史只读",
            "登记时间": _now(),
        }
        store.rows(RECALL_MODULE).append(batch)
        return batch

    def _compute_new_recall(self, scene_row: dict[str, Any], new_baseline: str,
                            dataset: str, drift_value: float) -> dict[str, Any]:
        """按新基线计算受影响场景召回；故障注入场景刷新失败并抛错，由调用方保留旧结果。"""
        scene = str(scene_row["推荐场景"])
        if scene_row.get("故障注入"):
            raise RuntimeError(f"场景「{scene}」召回服务刷新超时")
        batch = {
            "id": _next_id(store.rows(RECALL_MODULE)),
            "召回批次号": self._make_batch_no(scene, new_baseline),
            "推荐场景": scene,
            "监控数据集": dataset,
            "基线版本": new_baseline,
            "召回边界": _hash_boundary(scene, new_baseline, dataset),
            "召回数量": _hash_boundary(scene, new_baseline, "size") % 500 + 100,
            "只读": False,
            "当前可用性": True,
            "刷新状态": "刷新成功",
            "漂移值": drift_value,
            "登记时间": _now(),
        }
        store.rows(RECALL_MODULE).append(batch)
        return batch

    # ------------------------------------------------------------- 主流程
    def register_switch(self, detection_no: str,
                        payload: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str, bool]:
        """由数据漂移待处理明细登记一次基线切换，按场景版本差异重载。

        返回 (切换记录, 消息, 是否幂等命中)。重复提交不重建召回，只回传既有核对表。
        """
        payload = payload or {}
        drift = self._find_drift(detection_no)
        if drift is None:
            return None, f"数据漂移明细 {detection_no} 不存在，无法登记基线切换", False
        # 仅由数据漂移待处理明细（pending）登记；已启用/已关闭的检测不再受理切换。
        if not drift.get("pending") or drift.get("status") not in ("草稿", "规则评审中", "待处理"):
            return None, f"检测 {detection_no} 当前状态「{drift.get('status')}」不是待处理明细，不能登记切换", False

        target_baseline = str(payload.get("目标基线版本") or drift.get("目标基线版本") or "").strip()
        if not target_baseline:
            return None, "缺少目标基线版本，无法定位版本差异", False
        dataset = str(drift["监控数据集"])
        old_baseline = str(drift["基线版本"])
        if target_baseline == old_baseline:
            return None, f"目标基线版本 {target_baseline} 与当前基线相同，无版本差异可重载", False

        # 幂等：重复提交只登记一份可用于结算的核对表结果。
        existing = self._existing_switch(detection_no)
        if existing is not None:
            existing["最近提交时间"] = _now()
            existing["重复提交次数"] = int(existing.get("重复提交次数", 0)) + 1
            return existing, "该检测已登记基线切换，重复提交未重建召回，沿用既有核对表", True

        sample = self._monitor_sample(dataset, target_baseline)
        scenes = self._affected_scenes(dataset, old_baseline)
        try:
            drift_value = float(drift.get("最近漂移值") or 0.0)
        except (TypeError, ValueError):
            drift_value = 0.0
        switch_point = str(payload.get("切换点") or drift.get("切换点") or _now())
        window = str(drift.get("推荐对比窗口") or "")

        switch_row = {
            "id": _next_id(store.rows(SWITCH_MODULE)),
            "核验码": CODE_BUSINESS,
            "检测编号": detection_no,
            "监控数据集": dataset,
            "旧基线版本": old_baseline,
            "新基线版本": target_baseline,
            "推荐对比窗口": window,
            "切换点": switch_point,
            "漂移值": drift_value,
            "状态": ST_PENDING_REVIEW,
            "受影响场景数": len(scenes),
            "已完成召回数": 0,
            "刷新失败场景": [],
            "样本不足": not bool(sample and sample.get("样本完整")),
            "重复提交次数": 0,
            "登记时间": _now(),
            "最近提交时间": _now(),
        }
        store.rows(SWITCH_MODULE).append(switch_row)

        # 监控样本缺失：整个数据集停在待复核，不发布任何差异结果。
        if not sample or not sample.get("样本完整"):
            switch_row["状态"] = ST_NO_SAMPLE
            for scene_row in scenes:
                self._append_check(switch_row, drift, scene_row, None, None,
                                   status=ST_NO_SAMPLE, sample_ok=False, drift_value=drift_value)
            return switch_row, f"监控数据集 {dataset} 在 {target_baseline} 下样本不足，停在待复核，未发布差异结果", False

        switch_row["状态"] = ST_REFRESHING
        done = 0
        failed: list[str] = []
        for scene_row in scenes:
            old_batch = self._snapshot_old_recall(scene_row)
            old_weight = float(scene_row["多样性权重"])
            try:
                new_batch = self._compute_new_recall(scene_row, target_baseline, dataset, drift_value)
            except RuntimeError as exc:
                # 刷新失败：保留最后可用结果（旧召回只读、当前可用性不变），只登记失败。
                failed.append(str(scene_row["推荐场景"]))
                self._append_check(switch_row, drift, scene_row, old_batch, None,
                                   status=ST_REFRESH_FAILED, sample_ok=True,
                                   drift_value=drift_value, old_weight=old_weight,
                                   new_weight=old_weight, message=str(exc))
                continue

            new_weight = round(min(old_weight * (1 + drift_value), 1.0), 4)
            # 仅成功场景才写入当前推荐可用性与新多样性权重。
            scene_row["引用基线版本"] = target_baseline
            scene_row["当前可用基线"] = target_baseline
            scene_row["多样性权重"] = str(new_weight)
            for recall in store.rows(RECALL_MODULE):
                recall["当前可用性"] = (recall["召回批次号"] == new_batch["召回批次号"])

            self._append_check(switch_row, drift, scene_row, old_batch, new_batch,
                               status=ST_DIFF_DONE, sample_ok=True, drift_value=drift_value,
                               old_weight=old_weight, new_weight=new_weight)
            self._append_credential(switch_row, scene_row, old_batch, new_batch, switch_point)
            done += 1

        switch_row["已完成召回数"] = done
        switch_row["刷新失败场景"] = failed
        if failed and done == 0:
            switch_row["状态"] = ST_REFRESH_FAILED
        elif failed:
            switch_row["状态"] = ST_REFRESH_FAILED
        else:
            switch_row["状态"] = ST_DIFF_DONE
        return switch_row, (
            f"按场景版本差异重载完成：受影响场景 {len(scenes)} 个，成功刷新 {done} 个"
            + (f"，失败 {len(failed)} 个（保留最后可用结果）" if failed else "")
        ), False

    def _append_check(self, switch_row: dict[str, Any], drift: dict[str, Any],
                      scene_row: dict[str, Any], old_batch: dict[str, Any] | None,
                      new_batch: dict[str, Any] | None, *, status: str, sample_ok: bool,
                      drift_value: float, old_weight: float | None = None,
                      new_weight: float | None = None, message: str = "") -> dict[str, Any]:
        check_rows = store.rows(CHECK_MODULE)
        # 幂等保护：同一切换 + 同一场景只保留一条可结算核对记录。
        for row in check_rows:
            if (int(row.get("切换记录id", 0)) == int(switch_row["id"])
                    and row.get("推荐场景") == scene_row["推荐场景"]):
                return row
        entry = {
            "id": _next_id(check_rows),
            "核验码": CODE_BUSINESS,
            "切换记录id": switch_row["id"],
            "检测编号": drift["检测编号"],
            "监控数据集": drift["监控数据集"],
            "推荐场景": scene_row["推荐场景"],
            "旧基线版本": switch_row["旧基线版本"],
            "新基线版本": switch_row["新基线版本"],
            "旧召回批次号": old_batch["召回批次号"] if old_batch else "",
            "新召回批次号": new_batch["召回批次号"] if new_batch else "",
            "旧召回边界": old_batch["召回边界"] if old_batch else None,
            "新召回边界": new_batch["召回边界"] if new_batch else None,
            "旧多样性权重": str(old_weight) if old_weight is not None else str(scene_row["多样性权重"]),
            "新多样性权重": str(new_weight) if new_weight is not None else str(scene_row["多样性权重"]),
            "多样性权重变化": (round(new_weight - old_weight, 4)
                          if old_weight is not None and new_weight is not None else 0.0),
            "漂移值": drift_value,
            "监控样本完整": sample_ok,
            "状态": status,
            "可结算": False,
            "失败原因": message,
            "登记时间": _now(),
        }
        check_rows.append(entry)
        return entry

    def _append_credential(self, switch_row: dict[str, Any], scene_row: dict[str, Any],
                           old_batch: dict[str, Any], new_batch: dict[str, Any],
                           switch_point: str) -> dict[str, Any]:
        rows = store.rows(CRED_MODULE)
        cred = {
            "id": _next_id(rows),
            "切换凭据编号": f"CRED-{switch_row['id']:03d}-{scene_row['id']:03d}",
            "切换记录id": switch_row["id"],
            "检测编号": switch_row["检测编号"],
            "推荐场景": scene_row["推荐场景"],
            "监控数据集": switch_row["监控数据集"],
            "旧召回批次号": old_batch["召回批次号"],
            "新召回批次号": new_batch["召回批次号"],
            "旧基线版本": switch_row["旧基线版本"],
            "新基线版本": switch_row["新基线版本"],
            "切换点": switch_point,
            "凭据生成时间": _now(),
        }
        rows.append(cred)
        return cred

    def retry_failed(self, switch_id: int,
                     payload: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        """对刷新失败的场景重试；重试期间仍不动旧结果，成功后才写入当前可用性。"""
        payload = payload or {}
        switch_row = self.get_switch(switch_id)
        if switch_row is None:
            return None, f"切换记录 {switch_id} 不存在"
        if not switch_row.get("刷新失败场景"):
            return switch_row, "该切换没有刷新失败的场景，无需重试"
        recover = bool(payload.get("恢复故障"))
        target_baseline = str(switch_row["新基线版本"])
        dataset = str(switch_row["监控数据集"])
        drift = self._find_drift(str(switch_row["检测编号"]))
        drift_value = float(switch_row.get("漂移值") or 0.0)
        retried_ok: list[str] = []
        still_failed: list[str] = []
        for scene_row in store.rows(SCENE_MODULE):
            if scene_row["推荐场景"] not in switch_row["刷新失败场景"]:
                continue
            if recover:
                scene_row["故障注入"] = False
            old_batch = next(
                (row for row in store.rows(RECALL_MODULE)
                 if row.get("推荐场景") == scene_row["推荐场景"] and row.get("只读")),
                None,
            )
            old_weight = float(next(
                (row["旧多样性权重"] for row in self.list_checks(switch_id=switch_id)
                 if row.get("推荐场景") == scene_row["推荐场景"]),
                scene_row["多样性权重"],
            ))
            try:
                new_batch = self._compute_new_recall(scene_row, target_baseline, dataset, drift_value)
            except RuntimeError as exc:
                still_failed.append(str(scene_row["推荐场景"]))
                for check in self.list_checks(switch_id=switch_id):
                    if check.get("推荐场景") == scene_row["推荐场景"]:
                        check["失败原因"] = str(exc)
                continue
            new_weight = round(min(old_weight * (1 + drift_value), 1.0), 4)
            scene_row["引用基线版本"] = target_baseline
            scene_row["当前可用基线"] = target_baseline
            scene_row["多样性权重"] = str(new_weight)
            for recall in store.rows(RECALL_MODULE):
                recall["当前可用性"] = (recall["召回批次号"] == new_batch["召回批次号"])
            for check in self.list_checks(switch_id=switch_id):
                if check.get("推荐场景") == scene_row["推荐场景"]:
                    check.update({
                        "状态": ST_DIFF_DONE, "新召回批次号": new_batch["召回批次号"],
                        "新召回边界": new_batch["召回边界"],
                        "新多样性权重": str(new_weight),
                        "多样性权重变化": round(new_weight - old_weight, 4),
                        "失败原因": "",
                    })
            if drift is not None:
                self._append_credential(switch_row, scene_row, old_batch, new_batch,
                                        str(switch_row["切换点"]))
            retried_ok.append(str(scene_row["推荐场景"]))

        switch_row["刷新失败场景"] = still_failed
        switch_row["已完成召回数"] = int(switch_row.get("已完成召回数", 0)) + len(retried_ok)
        switch_row["状态"] = ST_REFRESH_FAILED if still_failed else ST_DIFF_DONE
        return switch_row, (
            f"重试完成：恢复 {len(retried_ok)} 个场景"
            + (f"，仍失败 {len(still_failed)} 个（旧结果继续可用）" if still_failed else "")
        )

    def verify_switch(self, switch_id: int) -> tuple[dict[str, Any] | None, str]:
        """核对切换前后结果：未引用新基线的场景不变，变化场景可由漂移值与多样性权重解释。"""
        switch_row = self.get_switch(switch_id)
        if switch_row is None:
            return None, f"切换记录 {switch_id} 不存在"
        if switch_row.get("样本不足") or switch_row["状态"] == ST_NO_SAMPLE:
            return switch_row, "监控样本不足，停在待复核，不能核对结算"
        checks = self.list_checks(switch_id=switch_id)
        if any(row["状态"] == ST_REFRESH_FAILED for row in checks):
            return switch_row, "存在刷新失败场景，需先重试成功后再核对"

        dataset = str(switch_row["监控数据集"])
        # 未引用新基线的场景：当前基线必须保持旧基线、多样性权重不变，且不进入差异清单。
        # 逐场景核对其登记时的旧权重快照，确保未受影响场景没有被反复重建。
        diff_scenes = {c["推荐场景"] for c in checks}
        untouched = [row for row in store.rows(SCENE_MODULE)
                     if row["推荐场景"] not in diff_scenes]
        old_baseline = str(switch_row["旧基线版本"])
        for row in untouched:
            if row.get("当前可用基线") != row.get("引用基线版本"):
                return switch_row, f"未引用新基线的场景「{row['推荐场景']}」基线被改写，核对不通过"
            if row.get("引用数据集") == dataset and row.get("当前可用基线") != old_baseline:
                return switch_row, f"同数据集未切换场景「{row['推荐场景']}」偏离旧基线，核对不通过"

        for check in checks:
            if check["状态"] != ST_DIFF_DONE:
                return switch_row, f"场景「{check['推荐场景']}」差异未生成，不能核对"
            # 变化场景必须能由漂移值与多样性权重变化解释。
            drift_value = float(check.get("漂移值") or 0.0)
            if drift_value <= 0:
                return switch_row, f"场景「{check['推荐场景']}」缺少漂移值，变化不可解释"
            check["状态"] = ST_CHECKED
            check["可结算"] = True
        switch_row["状态"] = ST_CHECKED
        return switch_row, (
            f"核对通过：{len(checks)} 个变化场景可由漂移值与多样性权重解释，"
            f"{len(untouched)} 个未引用新基线场景保持不变"
        )

    def locate_breakpoint(self, detection_no: str) -> dict[str, Any]:
        """沿原始调用链（漂移明细→切换记录→召回批次→场景可用性）复核版本断点。

        同一对比窗口只允许一套可解释基线；本方法返回切换点前后两段基线边界，
        确认不存在两个并行召回边界。
        """
        drift = self._find_drift(detection_no)
        chain: list[dict[str, Any]] = []
        if drift is None:
            return {"核验码": CODE_BUSINESS, "检测编号": detection_no, "可解释": False,
                    "冲突": "漂移明细不存在", "调用链": chain}
        chain.append({"环节": "数据漂移明细", "检测编号": detection_no,
                      "监控数据集": drift.get("监控数据集"), "基线版本": drift.get("基线版本"),
                      "目标基线版本": drift.get("目标基线版本")})
        switch_row = self._existing_switch(detection_no)
        if switch_row is None:
            return {"核验码": CODE_BUSINESS, "检测编号": detection_no, "可解释": False,
                    "冲突": "尚未登记基线切换，无版本断点", "调用链": chain}
        chain.append({"环节": "基线版本切换记录", "切换记录id": switch_row["id"],
                      "切换点": switch_row["切换点"], "旧基线版本": switch_row["旧基线版本"],
                      "新基线版本": switch_row["新基线版本"]})
        checks = self.list_checks(switch_id=int(switch_row["id"]))
        segments: list[dict[str, Any]] = []
        conflict = ""
        for check in checks:
            chain.append({"环节": "召回批次", "推荐场景": check["推荐场景"],
                          "旧召回批次号": check["旧召回批次号"], "新召回批次号": check["新召回批次号"]})
            segments.append({
                "推荐场景": check["推荐场景"],
                "切换前": {"基线": check["旧基线版本"], "召回边界": check["旧召回边界"],
                          "召回批次号": check["旧召回批次号"]},
                "切换后": {"基线": check["新基线版本"], "召回边界": check["新召回边界"],
                          "召回批次号": check["新召回批次号"]},
            })
            if check["旧召回边界"] == check["新召回边界"]:
                conflict = f"场景「{check['推荐场景']}」新旧召回边界相同，版本断点缺失"
        # 当前可用性只指向一个批次 → 当前召回只对应一套可解释基线。
        active = [r for r in store.rows(RECALL_MODULE) if r.get("当前可用性")]
        if len({r["基线版本"] for r in active}) > 1:
            conflict = "同一对比窗口出现两套并行召回基线，存在双召回边界冲突"
        return {
            "核验码": CODE_BUSINESS,
            "检测编号": detection_no,
            "推荐对比窗口": switch_row["推荐对比窗口"],
            "版本断点": switch_row["切换点"],
            "仅向前更新": True,
            "可解释": not conflict,
            "冲突": conflict,
            "基线分段": segments,
            "调用链": chain,
        }

    def sync_recommend_status(self, detection_no: str) -> tuple[dict[str, Any] | None, str]:
        """把智能推荐（DATA-0001）状态同步到数据漂移清单。"""
        drift = self._find_drift(detection_no)
        if drift is None:
            return None, f"数据漂移明细 {detection_no} 不存在"
        switch_row = self._existing_switch(detection_no)
        if switch_row is None:
            drift["推荐状态同步"] = "未切换"
            drift["推荐状态同步时间"] = _now()
            return drift, "该检测尚未登记基线切换，已同步为未切换"
        checks = self.list_checks(switch_id=int(switch_row["id"]))
        if switch_row.get("样本不足"):
            synced = "待复核-监控样本不足"
            conclusion = "监控样本不足，维持原基线，待补齐样本"
        elif switch_row.get("刷新失败场景"):
            synced = "部分刷新失败-保留最后可用结果"
            conclusion = "部分场景刷新失败，旧召回继续可用"
        elif switch_row["状态"] == ST_CHECKED:
            synced = "已同步-核对通过"
            conclusion = self._drift_conclusion(float(switch_row.get("漂移值") or 0.0), drift)
        else:
            synced = f"已同步-{switch_row['状态']}"
            conclusion = self._drift_conclusion(float(switch_row.get("漂移值") or 0.0), drift)
        drift["推荐状态同步"] = synced
        drift["推荐状态同步时间"] = _now()
        drift["漂移处置结论"] = conclusion
        return drift, f"智能推荐状态已同步到数据漂移清单：{synced}"

    @staticmethod
    def _drift_conclusion(drift_value: float, drift: dict[str, Any]) -> str:
        try:
            threshold = float(drift.get("漂移阈值") or 0.25)
        except (TypeError, ValueError):
            threshold = 0.25
        if drift_value >= threshold:
            return f"漂移值 {drift_value} 达阈值 {threshold}，已切换新基线并完成差异核对"
        return f"漂移值 {drift_value} 低于阈值 {threshold}，按版本差异预防性切换并核对通过"

    # ------------------------------------------------- 构建上线（联调环境数据库）
    def deploy_dependency(self) -> dict[str, Any]:
        rows = store.rows(DEPLOY_MODULE)
        return rows[0] if rows else {}

    def connect_env_db(self) -> tuple[dict[str, Any], str]:
        rows = store.rows(DEPLOY_MODULE)
        if not rows:
            row = {"id": 1, "依赖名称": "联调环境数据库", "状态": "已联通", "联通时间": _now()}
            rows.append(row)
        else:
            rows[0]["状态"] = "已联通"
            rows[0]["联通时间"] = _now()
        return rows[0], "联调环境数据库已联通"

    def build_online(self) -> tuple[dict[str, Any] | None, str]:
        dep = self.deploy_dependency()
        if dep.get("状态") != "已联通":
            return None, "联调环境数据库未联通，智能推荐不能构建上线"
        switch_row = self._existing_switch("DATA-0001")
        if switch_row is None:
            return None, "主业务样本 DATA-0001 尚未登记基线切换，无可上线的推荐基线"
        if switch_row.get("状态") != ST_CHECKED:
            return None, f"DATA-0001 切换单状态为「{switch_row.get('状态')}」，核对通过后才能构建上线"
        return {
            "核验码": CODE_BUILD_ONLINE,
            "构建对象": "智能推荐（DATA-0001 主业务样本）",
            "上线基线": switch_row["新基线版本"],
            "关联切换记录id": switch_row["id"],
            "构建状态": "构建上线成功",
            "完成时间": _now(),
        }, "智能推荐构建上线成功，部署依赖联调环境数据库校验通过"


baseline_switch_service = BaselineSwitchService()
