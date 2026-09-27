"""约定清单与交付清单核对（纯内存，不访问文件系统，不读取文件内容）。

两侧条目均为 ``file`` / ``directory``，文件条目的 SHA-256 由调用方随请求
**声明**，本模块只比较声明摘要，绝不读取磁盘上的文件。

核对分类：

* :attr:`AuditDifference.MATCH`：两侧同键唯一条目，类型（文件还包括摘要）一致；
* :attr:`AuditDifference.TYPE_MISMATCH`：同键唯一条目类型不符（file ↔ directory）；
* :attr:`AuditDifference.DIGEST_MISMATCH`：同键唯一文件条目声明的 SHA-256 不符；
* 仅约定侧存在：交付侧有文件占据其祖先 → 层级阻挡，否则为缺失（MISSING）；
* 仅交付侧存在：约定侧有文件占据其祖先 → 层级阻挡，否则为额外（EXTRA）；
* 任一侧同键出现多条：整键列为歧义（ambiguous），不任意配对，成员全部列出。

层级阻挡中的“阻挡文件”取另一侧占据该条目祖先路径的文件；存在多个时
（近祖与远祖同时被另一侧列为文件），取**最深层（最近）**的为首要阻挡，
其余按由近及远列入 ``additional_blocked_by``。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .normalize import IssueSeverity, NormalizedEntry

FILE_TYPE = "file"

_SIDE_LABEL = {
    "agreed": "约定清单",
    "delivered": "交付清单",
}


class ManifestSide(str, Enum):
    AGREED = "agreed"  # 约定清单
    DELIVERED = "delivered"  # 交付清单


def validate_manifest_side(
    entries: list[NormalizedEntry], side: ManifestSide
) -> None:
    """拒绝不安全路径；发现问题时抛出 ``ValueError``（API 层映射为 422）。

    同键重复**不**在此拒绝：单侧同键重复属于核对结果中的“歧义键”，
    需要返回 200 并列出全部成员，而非整体拒绝请求。
    """
    label = _SIDE_LABEL[side.value]
    for entry in entries:
        if entry.unsafe:
            codes = ", ".join(
                i.code.value
                for i in entry.issues
                if i.severity == IssueSeverity.ERROR
            )
            raise ValueError(
                f"{label}条目 #{entry.index}（{entry.original_path!r}）"
                f"路径不安全：{codes}"
            )


class AuditDifference(str, Enum):
    MATCH = "MATCH"  # 一致
    MISSING = "MISSING"  # 约定有、交付无
    EXTRA = "EXTRA"  # 交付有、约定无
    TYPE_MISMATCH = "TYPE_MISMATCH"  # 同键类型不符
    DIGEST_MISMATCH = "DIGEST_MISMATCH"  # 同键文件摘要不符
    HIERARCHY_BLOCKED = "HIERARCHY_BLOCKED"  # 被另一侧文件祖先阻挡
    AMBIGUOUS = "AMBIGUOUS"  # 单侧同键重复，整键歧义


@dataclass(frozen=True)
class AuditEntry:
    """参与核对的单条条目（下标与原始清单一一对应，从 1 开始）。"""

    side: ManifestSide
    index: int
    entry_type: str  # "file" | "directory"
    original_path: str
    canonical_path: str
    comparison_key: str
    sha256: str | None  # 声明摘要（已归一为小写十六进制）；目录为 None

    @property
    def depth(self) -> int:
        return self.comparison_key.count("/") + 1


@dataclass(frozen=True)
class AuditPair:
    """同键唯一成对条目的核对结果。"""

    status: AuditDifference  # MATCH / TYPE_MISMATCH / DIGEST_MISMATCH
    comparison_key: str
    agreed: AuditEntry
    delivered: AuditEntry


@dataclass(frozen=True)
class HierarchyBlocked:
    """被另一侧文件祖先阻挡的单侧条目（不计普通缺失/额外）。"""

    entry: AuditEntry
    blocked_by: AuditEntry  # 最近（最深层）的阻挡文件
    additional_blocked_by: tuple[AuditEntry, ...] = ()  # 其余阻挡文件，由近及远


@dataclass(frozen=True)
class AmbiguousKey:
    """单侧（或两侧）同键重复的歧义键：成员全部列出，不任意配对。"""

    comparison_key: str
    canonical_path: str  # 展示用规范路径（优先取约定侧首个成员）
    agreed: tuple[AuditEntry, ...]
    delivered: tuple[AuditEntry, ...]


@dataclass
class AuditResult:
    matched: list[AuditPair]
    type_mismatches: list[AuditPair]
    digest_mismatches: list[AuditPair]
    missing: list[AuditEntry]  # 约定有、交付无且未被阻挡
    extra: list[AuditEntry]  # 交付有、约定无且未被阻挡
    hierarchy_blocked: list[HierarchyBlocked]
    ambiguous_keys: list[AmbiguousKey]


def _to_entry(
    side: ManifestSide, normalized: NormalizedEntry, sha256: str | None
) -> AuditEntry:
    # 调用方已拒绝不安全路径，故 comparison_key 必非 None。
    assert normalized.comparison_key is not None
    return AuditEntry(
        side=side,
        index=normalized.index,
        entry_type=normalized.entry_type,
        original_path=normalized.original_path,
        canonical_path=normalized.canonical_path,
        comparison_key=normalized.comparison_key,
        sha256=sha256,
    )


def _group_by_key(entries: list[AuditEntry]) -> dict[str, list[AuditEntry]]:
    groups: dict[str, list[AuditEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.comparison_key, []).append(entry)
    return groups


def _file_index(
    groups: dict[str, list[AuditEntry]],
) -> dict[str, list[AuditEntry]]:
    """比较键 → 该键上的文件条目（仅保留文件所在键）。"""
    return {
        key: [e for e in members if e.entry_type == FILE_TYPE]
        for key, members in groups.items()
        if any(e.entry_type == FILE_TYPE for e in members)
    }


def _file_ancestor_blockers(
    entry: AuditEntry, other_files: dict[str, list[AuditEntry]]
) -> list[AuditEntry]:
    """另一侧占据 ``entry`` 祖先路径（不含自身）的全部文件，由近及远排序。"""
    parts = entry.comparison_key.split("/")
    blockers: list[AuditEntry] = []
    for depth in range(1, len(parts)):  # 不含自身
        blockers.extend(other_files.get("/".join(parts[:depth]), ()))
    # 近祖（深层）优先，同深度按下标稳定排序。
    blockers.sort(key=lambda b: (-b.depth, b.index))
    return blockers


def audit_manifests(
    agreed: list[tuple[NormalizedEntry, str | None]],
    delivered: list[tuple[NormalizedEntry, str | None]],
) -> AuditResult:
    """核对约定清单与交付清单。

    参数为 ``(规范化条目, 声明 SHA-256（小写）或 None)`` 二元组列表，下标从 1
    开始；调用方须保证两侧条目路径均安全（不安全路径应在 API 层拒绝）。
    """
    agreed_entries = [
        _to_entry(ManifestSide.AGREED, norm, sha) for norm, sha in agreed
    ]
    delivered_entries = [
        _to_entry(ManifestSide.DELIVERED, norm, sha) for norm, sha in delivered
    ]

    agreed_groups = _group_by_key(agreed_entries)
    delivered_groups = _group_by_key(delivered_entries)
    agreed_files = _file_index(agreed_groups)
    delivered_files = _file_index(delivered_groups)

    matched: list[AuditPair] = []
    type_mismatches: list[AuditPair] = []
    digest_mismatches: list[AuditPair] = []
    missing: list[AuditEntry] = []
    extra: list[AuditEntry] = []
    hierarchy_blocked: list[HierarchyBlocked] = []
    ambiguous_keys: list[AmbiguousKey] = []

    def _classify_unpaired(
        entry: AuditEntry, other_files: dict[str, list[AuditEntry]]
    ) -> None:
        blockers = _file_ancestor_blockers(entry, other_files)
        if blockers:
            hierarchy_blocked.append(
                HierarchyBlocked(
                    entry=entry,
                    blocked_by=blockers[0],
                    additional_blocked_by=tuple(blockers[1:]),
                )
            )
        elif entry.side is ManifestSide.AGREED:
            missing.append(entry)
        else:
            extra.append(entry)

    for key in sorted(set(agreed_groups) | set(delivered_groups)):
        am = agreed_groups.get(key, [])
        dm = delivered_groups.get(key, [])

        # 任一侧同键多条 → 整键歧义，不做任何配对。
        if len(am) > 1 or len(dm) > 1:
            display_members = am if am else dm
            ambiguous_keys.append(
                AmbiguousKey(
                    comparison_key=key,
                    canonical_path=display_members[0].canonical_path,
                    agreed=tuple(sorted(am, key=lambda e: e.index)),
                    delivered=tuple(sorted(dm, key=lambda e: e.index)),
                )
            )
            continue

        if not am or not dm:
            # 仅单侧存在：检查另一侧文件祖先阻挡，否则记缺失/额外。
            only = (am or dm)[0]
            _classify_unpaired(
                only, delivered_files if not dm else agreed_files
            )
            continue

        a, d = am[0], dm[0]
        if a.entry_type != d.entry_type:
            type_mismatches.append(
                AuditPair(AuditDifference.TYPE_MISMATCH, key, a, d)
            )
        elif a.entry_type == FILE_TYPE and a.sha256 != d.sha256:
            digest_mismatches.append(
                AuditPair(AuditDifference.DIGEST_MISMATCH, key, a, d)
            )
        else:
            matched.append(AuditPair(AuditDifference.MATCH, key, a, d))

    # 全部结果稳定排序（同键下标兜底，保证输出与输入顺序无关地确定）。
    matched.sort(key=lambda p: p.comparison_key)
    type_mismatches.sort(key=lambda p: p.comparison_key)
    digest_mismatches.sort(key=lambda p: p.comparison_key)
    missing.sort(key=lambda e: (e.comparison_key, e.index))
    extra.sort(key=lambda e: (e.comparison_key, e.index))
    hierarchy_blocked.sort(key=lambda h: (h.entry.comparison_key, h.entry.index))

    return AuditResult(
        matched=matched,
        type_mismatches=type_mismatches,
        digest_mismatches=digest_mismatches,
        missing=missing,
        extra=extra,
        hierarchy_blocked=hierarchy_blocked,
        ambiguous_keys=ambiguous_keys,
    )
