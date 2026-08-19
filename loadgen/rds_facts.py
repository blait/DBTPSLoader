"""Live RDS instance facts, keyed by DB identifier.

Used by both the report endpoint and the CLI so the two produce the same
document. The report must not take vCPU on faith: `analysis.VCPU` is a static
map, but the whole argument turns on the HT-off side really having half the
vCPU, so when the RDS API is reachable the measured `ProcessorFeatures` is what
gets printed. Provisioned IOPS comes from the same call, which is what makes
"IOPS was the ceiling" checkable rather than asserted.

Region is derived from the run's own endpoint in meta.json, so no configuration
is needed and the CLI works from a bare checkout of runs/.
"""
from __future__ import annotations

import logging

log = logging.getLogger("loadgen.rds_facts")

# 인스턴스 클래스 이름에서 vCPU/메모리를 추정한다. `ProcessorFeatures`가 있으면
# 그쪽이 우선이다 (coreCount / threadsPerCore) — vCPU를 줄여 띄운 인스턴스는
# 클래스 이름만으로는 알 수 없기 때문이다.
#
# 클래스 표를 코드에 나열하지 않는 이유: 어떤 인스턴스로 쓸지 모르는 범용 도구에서
# 목록은 늘 부족하다. 이름 규칙(`db.<family>.<size>`)으로 추정하고, 추정임을
# 밝힌다. 정확한 값은 comparisons.yaml의 vcpu가 정한다.
_SIZE_VCPU = {
    "large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16, "8xlarge": 32,
    "12xlarge": 48, "16xlarge": 64, "24xlarge": 96, "32xlarge": 128,
}
# 패밀리별 vCPU당 GiB (m=4, r=8, x=16 계열)
_FAMILY_MEM_PER_VCPU = {"t": 2, "m": 4, "c": 2, "r": 8, "x": 16, "z": 12}


def _parse_class(cls: str) -> tuple[int | None, int | None]:
    """'db.r6i.8xlarge' -> (32 vCPU, 256 GiB). 모르면 (None, None)."""
    parts = (cls or "").split(".")
    if len(parts) < 3:
        return None, None
    family, size = parts[1], ".".join(parts[2:])
    vcpu = _SIZE_VCPU.get(size)
    if vcpu is None:
        return None, None
    per = _FAMILY_MEM_PER_VCPU.get(family[:1], 4)
    return vcpu, vcpu * per

_cache: dict = {}    # db_id -> facts dict (or None when unreachable)
_errors: dict = {}   # db_id -> why it is None, so the report can say so


def region_of(host: str) -> str | None:
    """'p7-m6i.abc123.ap-northeast-2.rds.amazonaws.com' -> 'ap-northeast-2'"""
    if not host or ".rds.amazonaws.com" not in host:
        return None
    return host.split(".rds.amazonaws.com")[0].split(".")[-1]


def _describe(db_id: str, host: str) -> dict | None:
    import boto3

    region = region_of(host)
    if not region:
        return None
    i = boto3.client("rds", region_name=region).describe_db_instances(
        DBInstanceIdentifier=db_id)["DBInstances"][0]
    pf = {p["Name"]: p["Value"] for p in i.get("ProcessorFeatures", [])}
    cls = i["DBInstanceClass"]
    class_vcpu, class_mem = _parse_class(cls)
    if "coreCount" in pf:
        # 실측값. vCPU를 줄여 띄운 인스턴스는 이것만이 진실이다.
        threads = int(pf.get("threadsPerCore", 2))
        vcpu = int(pf["coreCount"]) * threads
        proc = f"coreCount={pf['coreCount']}, threadsPerCore={threads}"
    else:
        vcpu, proc = class_vcpu, None
    facts = {
        "db_id": db_id, "instance_class": cls, "vcpu": vcpu, "processor": proc,
        "vcpu_is_measured": "coreCount" in pf,
        "memory_gb": class_mem,
        "storage_type": i.get("StorageType"),
        "storage_gb": i.get("AllocatedStorage"),
        "iops": i.get("Iops"),
        "storage_mbps": i.get("StorageThroughput"),
        "engine_version": i.get("EngineVersion"),
        "engine_desc": f"{i.get('Engine')} {i.get('EngineVersion')}",
        "multi_az": i.get("MultiAZ"),
        "status": i.get("DBInstanceStatus"),
    }
    facts["storage_desc"] = (
        f"{facts['storage_type']} {facts['storage_gb']}GB / {facts['iops']:,} IOPS"
        + (f" / {facts['storage_mbps']} MB/s" if facts["storage_mbps"] else ""))
    return facts


def instance_facts(hosts: dict[str, str]) -> dict:
    """{db_id: host} -> {db_id: facts or None}. Never raises: the report
    renders without these, just with fewer verified columns."""
    out = {}
    for db_id, host in hosts.items():
        if db_id not in _cache:
            try:
                _cache[db_id] = _describe(db_id, host)
                _errors.pop(db_id, None)
            except Exception as e:  # noqa: BLE001
                log.info("instance facts unavailable for %s: %s", db_id, e)
                _cache[db_id], _errors[db_id] = None, f"{type(e).__name__}: {e}"
        out[db_id] = _cache[db_id]
    return out


def facts_for_rows(rows: list[dict]) -> dict:
    """Facts for every instance appearing in a set of analysis rows."""
    hosts = {r["db_id"]: r["host"] for r in rows if r.get("db_id") and r.get("host")}
    return instance_facts(hosts)


def vcpu_warnings(rows: list[dict], facts: dict) -> list[str]:
    """A vCPU map disagreeing with the instance would invalidate every
    normalized figure in the report, so it is surfaced, not absorbed."""
    out = [
        f"{r['inst']} ({r['db_id']}): 표 계산에 쓴 vCPU {r['vcpu']}가 실제 "
        f"인스턴스 값 {facts[r['db_id']]['vcpu']}와 다르다 — 정규화 지표 전부 재확인 필요"
        for r in rows
        if r.get("db_id") and facts.get(r["db_id"]) and facts[r["db_id"]].get("vcpu")
        and facts[r["db_id"]]["vcpu"] != r.get("vcpu")
    ]
    # An unreachable RDS API is not a cosmetic loss: vCPU then comes from the
    # static map instead of measured ProcessorFeatures, and vCPU is what every
    # normalized figure divides by. Reported once per distinct cause — when the
    # whole lookup is down (no credentials, no boto3) all four fail identically.
    missing = {db_id: _errors.get(db_id) for db_id in facts if facts[db_id] is None}
    for cause in dict.fromkeys(v for v in missing.values() if v):
        ids = ", ".join(k for k, v in missing.items() if v == cause)
        out.append(f"RDS 조회 실패 ({ids}): {cause} — vCPU·IOPS가 실측이 아니라 "
                   f"정적 맵 기본값이다. 클래스·스토리지·ProcessorFeatures 열은 비고, "
                   f"HT-off의 vCPU 절감은 이 문서로 검증되지 않는다")
    return out
