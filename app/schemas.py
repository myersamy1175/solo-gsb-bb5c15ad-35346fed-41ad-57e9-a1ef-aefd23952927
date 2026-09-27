"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from .normalize import IssueCode, IssueSeverity, normalize_entry
from .mirror import MirrorOperation
from .overlay import (
    OverlayBlockReason,
    OverlayNodeSource,
    OverlayStatus,
    validate_existing_entries,
)
from .selection import SelectionAction, apply_selection_rules, validate_pattern


class EntryType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


class SelectionRuleIn(BaseModel):
    action: SelectionAction = Field(
        ..., description="命中后的筛选动作：include（实际解包）或 exclude（排除）"
    )
    pattern: str = Field(
        ...,
        description=(
            "按路径段解释的 glob 模式，匹配规范化后的归档路径："
            "* 不跨 /，? 匹配单段内一个字符，** 必须独占一段且可跨多级"
        ),
    )

    @model_validator(mode="after")
    def _validate_pattern(self) -> "SelectionRuleIn":
        validate_pattern(self.pattern)
        return self


class PreflightEntryIn(BaseModel):
    path: str = Field(..., description="待解包条目的原始路径")
    type: EntryType = Field(..., description="条目类型：file、directory 或 symlink")
    link_target: str | None = Field(
        default=None,
        description="符号链接目标（type 为 symlink 时必填；file/directory 不得携带）",
    )
    compressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description="声明的压缩大小（字节，非负；仅 file 条目可携带）",
    )
    uncompressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description="声明的解压大小（字节，非负；仅 file 条目可携带）",
    )

    @model_validator(mode="after")
    def _validate_link_target(self) -> "PreflightEntryIn":
        if self.type is EntryType.SYMLINK:
            if self.link_target is None:
                raise ValueError("type 为 symlink 时必须提供 link_target")
        elif self.link_target is not None:
            raise ValueError("仅 symlink 条目可携带 link_target")
        return self

    @model_validator(mode="after")
    def _validate_declared_sizes(self) -> "PreflightEntryIn":
        if self.type is not EntryType.FILE and (
            self.compressed_bytes is not None
            or self.uncompressed_bytes is not None
        ):
            raise ValueError(
                "仅 file 条目可携带 compressed_bytes/uncompressed_bytes"
            )
        return self


class ResourceLimits(BaseModel):
    """可选的解压资源预算；任一字段为 null 表示不启用该项限制。"""

    max_file_uncompressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description="单文件解压大小上限（字节）；启用时每个 file 条目必须提供 uncompressed_bytes",
    )
    max_total_uncompressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description="整批解压总量上限（字节）；启用时每个 file 条目必须提供 uncompressed_bytes",
    )
    max_compression_ratio: int | None = Field(
        default=None,
        ge=1,
        description=(
            "最大压缩比（解压大小/压缩大小，整数）；启用时每个 file 条目必须同时提供 "
            "compressed_bytes 与 uncompressed_bytes"
        ),
    )


class PreflightRequest(BaseModel):
    entries: list[PreflightEntryIn] = Field(
        ..., min_length=1, description="待预检的条目列表（至少 1 条）"
    )
    repair: bool = Field(
        default=False,
        description="为 true 时生成确定性 repair_plan（同名碰撞与文件祖先冲突修复建议）",
    )
    resource_limits: ResourceLimits | None = Field(
        default=None,
        description="可选的解压资源预算校验；省略时行为与旧版完全一致",
    )
    selection_rules: list[SelectionRuleIn] | None = Field(
        default=None,
        description=(
            "可选的解包筛选规则，按规范化后的归档路径匹配；"
            "按输入顺序依次匹配，最后一条命中规则生效，未命中默认 include；"
            "省略时全部条目参与预检，行为与旧版完全一致"
        ),
    )

    @model_validator(mode="after")
    def _validate_required_sizes(self) -> "PreflightRequest":
        limits = self.resource_limits
        if limits is None:
            return self
        need_uncompressed = (
            limits.max_file_uncompressed_bytes is not None
            or limits.max_total_uncompressed_bytes is not None
        )
        need_ratio = limits.max_compression_ratio is not None
        if not need_uncompressed and not need_ratio:
            return self
        # 被 selection_rules 排除的文件不参与资源预算，无需声明大小。
        normalized = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.entries, start=1)
        ]
        decisions = apply_selection_rules(
            [n.canonical_parts for n in normalized],
            None
            if self.selection_rules is None
            else [(r.action.value, r.pattern) for r in self.selection_rules],
        )
        for index, entry in enumerate(self.entries, start=1):
            if entry.type is not EntryType.FILE or not decisions[index - 1].selected:
                continue
            if need_uncompressed and entry.uncompressed_bytes is None:
                raise ValueError(
                    f"启用单文件/整批解压大小限制时，文件条目 #{index} "
                    "必须提供 uncompressed_bytes"
                )
            if need_ratio and (
                entry.compressed_bytes is None
                or entry.uncompressed_bytes is None
            ):
                raise ValueError(
                    f"启用最大压缩比限制时，文件条目 #{index} 必须同时提供 "
                    "compressed_bytes 与 uncompressed_bytes"
                )
        return self


class IssueOut(BaseModel):
    code: IssueCode
    severity: IssueSeverity
    message: str
    segment: int | None = None
    related_index: int | None = None


class CollisionMember(BaseModel):
    index: int
    canonical_path: str


class CollisionGroup(BaseModel):
    group: int = Field(..., description="碰撞组编号（按首次出现顺序分配）")
    canonical_path: str = Field(..., description="该组对应的规范路径（展示用）")
    members: list[CollisionMember]


class HierarchyConflict(BaseModel):
    file_index: int = Field(..., description="作为祖先路径的文件条目下标")
    file_canonical_path: str
    descendant_index: int = Field(..., description="被阻挡的后代条目下标")
    descendant_canonical_path: str


class PreflightEntryOut(BaseModel):
    index: int = Field(..., description="条目下标（从 1 开始，与输入顺序一致）")
    type: EntryType
    original_path: str
    link_target: str | None = Field(
        default=None, description="符号链接目标（仅 symlink 条目有值）"
    )
    canonical_path: str = Field(..., description="规范化路径（NFC、段尾已裁剪，正斜杠分隔）")
    comparison_key: str | None = Field(
        ..., description="Windows 等价比较键（小写折叠，仅安全条目可用）"
    )
    safe: bool
    collision_group: int | None = Field(
        ..., description="所属同名碰撞组编号；无碰撞或不安全时为 null"
    )
    blocked_by_file_ancestors: list[int] = Field(
        default_factory=list, description="阻挡本条目的文件祖先下标列表"
    )
    blocks_descendants: list[int] = Field(
        default_factory=list, description="被本文件条目阻挡的后代下标列表（仅文件条目）"
    )
    selected: bool = Field(
        default=True,
        description=(
            "selection_rules 筛选结果：true 为实际解包条目；省略 "
            "selection_rules 时恒为 true"
        ),
    )
    matched_rule_index: int | None = Field(
        default=None,
        description=(
            "最后一条命中的 selection_rules 下标（从 0 开始，与请求下标一致）；"
            "未命中任何规则或省略 selection_rules 时为 null"
        ),
    )
    issues: list[IssueOut] = Field(default_factory=list)


class SymlinkChainStep(BaseModel):
    index: int | None = Field(
        ..., description="该步对应的符号链接条目下标；解析终点（非链接路径）为 null"
    )
    path: str = Field(..., description="该步的规范路径（正斜杠分隔）")


class SymlinkLinkOut(BaseModel):
    index: int = Field(..., description="符号链接条目下标（与输入顺序一致）")
    original_path: str
    link_target: str = Field(..., description="原始链接目标")
    resolved_target: str | None = Field(
        ...,
        description="自身目标相对链接所在目录解析后的根内路径；越界为 null",
    )
    final_path: str | None = Field(
        ...,
        description="沿链接链解析到的最终根内路径；目标越界或链存在循环时为 null",
    )
    chain: list[SymlinkChainStep] = Field(
        ...,
        description="解析链：依次为自身、途经的符号链接，以及最终解析终点（若有）",
    )
    risk_codes: list[IssueCode] = Field(
        default_factory=list,
        description=(
            "链接风险代码：LINK_TARGET_ESCAPE（目标或链越出目标根）、"
            "LINK_TARGET_INVALID（目标非法）、LINK_CYCLE（链存在循环）"
        ),
    )
    safe: bool = Field(..., description="无任何 error 级风险时为 true")


class SymlinkCycleOut(BaseModel):
    members: list[int] = Field(
        ..., description="构成循环的符号链接下标（按链顺序，从最小下标开始）"
    )


class SymlinkHijackOut(BaseModel):
    entry_index: int = Field(..., description="被劫持的文件/目录条目下标")
    canonical_path: str
    symlink_ancestors: list[int] = Field(
        ..., description="占据该条目祖先路径的符号链接下标（由近及远按深度升序）"
    )


class SymlinkAnalysisOut(BaseModel):
    links: list[SymlinkLinkOut] = Field(
        ..., description="逐链接解析链与风险代码（按输入顺序）"
    )
    cycles: list[SymlinkCycleOut] = Field(..., description="检测到的链接循环组")
    hijacked_entries: list[SymlinkHijackOut] = Field(
        ..., description="祖先路径被符号链接占据的文件/目录条目"
    )


class Summary(BaseModel):
    total: int
    safe: int
    unsafe: int
    selected: int = Field(
        default=0,
        description=(
            "selection_rules 判定为实际解包（include）的条目数量；"
            "省略 selection_rules 时等于 total"
        ),
    )
    excluded: int = Field(
        default=0,
        description="selection_rules 判定为排除（exclude）的条目数量"
    )
    collisions: int = Field(..., description="同名碰撞组数量")
    collision_entries: int
    hierarchy_conflicts: int
    symlinks: int = Field(..., description="符号链接条目数量")
    symlink_cycles: int = Field(..., description="检测到的链接循环组数")
    symlink_hijacked: int = Field(
        ..., description="被符号链接祖先劫持的文件/目录条目数量"
    )
    declared_compressed_bytes: int = Field(
        default=0, description="批次文件条目声明的压缩字节总数"
    )
    declared_uncompressed_bytes: int = Field(
        default=0, description="批次文件条目声明的解压字节总数"
    )
    resource_limits_exceeded: bool = Field(
        default=False,
        description="任一启用的资源限制（单文件解压大小、整批解压总量、压缩比）被超出时为 true",
    )


class RepairReason(str, Enum):
    COLLISION = "COLLISION"
    BLOCKS_DESCENDANT = "BLOCKS_DESCENDANT"
    UNSAFE = "UNSAFE"
    SYMLINK = "SYMLINK"


class RepairEntry(BaseModel):
    index: int = Field(..., description="条目下标（与输入顺序一致，从 1 开始）")
    original_path: str = Field(..., description="原始输入路径")
    repaired_path: str | None = Field(
        ...,
        description=(
            "修复后路径（正斜杠分隔）。安全且未改名的条目等于 canonical_path；"
            "skipped 条目为 null"
        ),
    )
    reasons: list[RepairReason] = Field(
        default_factory=list,
        description=(
            "修复原因：COLLISION（同名碰撞改让）、"
            "BLOCKS_DESCENDANT（文件阻挡后代而改名）；skipped 条目为 "
            "[UNSAFE]（不安全，含被符号链接祖先劫持）或 [SYMLINK]（符号链接）"
        ),
    )
    skipped: bool = Field(
        ..., description="为 true 表示不参与改名与复检（不安全条目与全部符号链接）"
    )


class RepairPlan(BaseModel):
    entries: list[RepairEntry] = Field(
        ..., description="按输入顺序排列的逐条修复计划"
    )
    renamed_count: int = Field(..., description="实际改名的安全条目数量")
    skipped_count: int = Field(
        ..., description="被跳过的条目数量（不安全条目与全部符号链接）"
    )
    collisions_remaining: int = Field(
        ..., description="按 repaired_path 复检后的同名碰撞组数（恒为 0）"
    )
    hierarchy_conflicts_remaining: int = Field(
        ..., description="按 repaired_path 复检后的文件祖先冲突对数（恒为 0）"
    )
    collisions_resolved: int = Field(
        ..., description="修复消除的同名碰撞组数（等于原 summary.collisions）"
    )
    hierarchy_conflicts_resolved: int = Field(
        ..., description="修复消除的文件祖先冲突对数（等于原 summary.hierarchy_conflicts）"
    )


class PreflightResponse(BaseModel):
    target: str = "windows"
    entries: list[PreflightEntryOut]
    collision_groups: list[CollisionGroup]
    hierarchy_conflicts: list[HierarchyConflict]
    symlink_analysis: SymlinkAnalysisOut
    summary: Summary


class PreflightRepairResponse(PreflightResponse):
    repair_plan: RepairPlan


class OverlayEntryType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


class OverlayEntryIn(BaseModel):
    path: str = Field(..., description="目标目录内的相对路径（原始形式）")
    type: OverlayEntryType = Field(..., description="条目类型：file 或 directory")


class OverlayPreviewRequest(BaseModel):
    """目标目录覆盖预演请求：现存清单 + 按顺序解包清单（纯内存干跑）。"""

    existing: list[OverlayEntryIn] = Field(
        default_factory=list,
        description=(
            "目标目录现存的 file/directory 清单（可为空，表示空目录）；"
            "含不安全路径、同键重复或文件占据其他条目祖先路径时返回 422"
        ),
    )
    incoming: list[OverlayEntryIn] = Field(
        ..., min_length=1, description="按顺序解包的 file/directory 清单（至少 1 条）"
    )

    @model_validator(mode="after")
    def _validate_existing_list(self) -> "OverlayPreviewRequest":
        normalized = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.existing, start=1)
        ]
        validate_existing_entries(normalized)
        return self


class OverlayBlocker(BaseModel):
    source: OverlayNodeSource = Field(
        ...,
        description=(
            "阻挡来源：existing（现存清单条目）、incoming（解包清单条目）"
            "或 implicit（未显式列出的隐含目录）"
        ),
    )
    index: int | None = Field(
        ..., description="阻挡条目在来源清单中的下标（从 1 开始）；隐含目录为 null"
    )
    canonical_path: str = Field(..., description="阻挡节点的规范路径（正斜杠分隔）")


class OverlayEntryOut(BaseModel):
    index: int = Field(..., description="条目下标（从 1 开始，与 incoming 输入顺序一致）")
    type: OverlayEntryType
    original_path: str
    canonical_path: str = Field(..., description="规范化路径（NFC、段尾已裁剪，正斜杠分隔）")
    comparison_key: str | None = Field(
        ..., description="Windows 等价比较键（小写折叠，仅安全条目可用）"
    )
    safe: bool = Field(..., description="路径自身是否安全（与是否被阻挡无关）")
    status: OverlayStatus = Field(
        ...,
        description=(
            "干跑结果：CREATE（新建）、OVERWRITE（覆盖同键文件）、"
            "REUSE_DIRECTORY（复用同键目录）或 BLOCKED（被阻挡）"
        ),
    )
    blocked_reason: OverlayBlockReason | None = Field(
        ...,
        description=(
            "BLOCKED 原因：UNSAFE_PATH（路径不安全）、TYPE_CONFLICT（同键类型冲突）、"
            "FILE_ANCESTOR（祖先路径被文件占据）；非 BLOCKED 为 null"
        ),
    )
    blocked_by: OverlayBlocker | None = Field(
        ..., description="阻挡来源及下标（仅冲突/阻挡类 BLOCKED 有值）"
    )
    issues: list[IssueOut] = Field(default_factory=list)


class OverlaySummary(BaseModel):
    total: int
    create: int
    overwrite: int
    reuse_directory: int
    blocked: int


class OverlayPreviewResponse(BaseModel):
    target: str = "windows"
    entries: list[OverlayEntryOut]
    summary: OverlaySummary


class MirrorPreviewRequest(BaseModel):
    """目录镜像同步预演请求：现存清单 + 期望归档清单（纯内存生成操作序列）。"""

    existing: list[OverlayEntryIn] = Field(
        default_factory=list,
        description=(
            "目标目录现存的 file/directory 清单（可为空，表示空目录）；"
            "沿用 overlay-preview 的现存清单校验，含不安全路径、同键重复或"
            "文件祖先冲突时返回 422"
        ),
    )
    incoming: list[OverlayEntryIn] = Field(
        default_factory=list,
        description=(
            "期望归档的 file/directory 清单（可为空，表示清空目标目录）；"
            "含不安全路径、同键（Windows 比较键）重复或文件占据其他条目"
            "祖先路径时返回 422。未列出的父目录视为需要保留的隐含目录"
        ),
    )

    @model_validator(mode="after")
    def _validate_manifests(self) -> "MirrorPreviewRequest":
        existing = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.existing, start=1)
        ]
        validate_existing_entries(existing, label="现存清单")
        incoming = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.incoming, start=1)
        ]
        validate_existing_entries(incoming, label="期望清单")
        return self


class MirrorStepOut(BaseModel):
    order: int = Field(..., description="执行顺序（从 1 开始）")
    operation: MirrorOperation = Field(
        ...,
        description=(
            "操作类型：DELETE（删除期望布局外或类型互换处的旧节点）、"
            "KEEP_DIRECTORY（保留现存目录）、CREATE_DIRECTORY（新建目录）、"
            "OVERWRITE_FILE（覆盖同键文件）、CREATE_FILE（新建文件）"
        ),
    )
    node_type: OverlayEntryType = Field(
        ..., description="节点类型：file 或 directory"
    )
    source: OverlayNodeSource = Field(
        ...,
        description=(
            "操作节点来源：existing（现存清单显式条目）、incoming（期望清单"
            "条目）或 implicit（未显式列出的隐含目录）"
        ),
    )
    index: int | None = Field(
        ...,
        description=(
            "原清单下标（从 1 开始）：DELETE/KEEP_DIRECTORY 指向 existing，"
            "CREATE_*/OVERWRITE_FILE 指向 incoming；隐含目录为 null"
        ),
    )
    canonical_path: str = Field(..., description="规范路径（正斜杠分隔）")
    comparison_key: str = Field(
        ..., description="Windows 等价比较键（小写折叠）"
    )


class MirrorSummary(BaseModel):
    total: int
    delete: int
    keep_directory: int
    create_directory: int
    create_file: int
    overwrite_file: int


class MirrorPreviewResponse(BaseModel):
    target: str = "windows"
    steps: list[MirrorStepOut]
    summary: MirrorSummary


class HealthResponse(BaseModel):
    status: str
    service: str
